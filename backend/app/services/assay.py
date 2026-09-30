"""化验数据业务规则：版本化复检、确认结论、并发与幂等控制都收在这里。

写入统一走事件事务：

- 同一化验编号每次复检都产生新版本行，历史版本按原版本保留；
- 「确认结论」以指定版本为准（默认最新版本），只有一次确认能成功，
  已确认版本的元素值不可被旧结果覆盖；
- request_id 相同的重复请求（断连重发/复位重放）直接返回首次结果；
- 台账写入、样品追溯清单、报告数据面板、出站事件在同一事务内保持一致。
"""
from __future__ import annotations

from typing import Any

from app import store as store_module
from app.events.assay import (
    CONFIRMED_STATUS,
    ELEMENT_FIELDS,
    EVT_CONFIRMED,
    EVT_VERSION_CREATED,
    EVT_VERSION_RETURNED,
    MODULE,
    PENDING_CONFIRM_STATUS,
    RETURNED_STATUS,
    VERSION_FIELD,
    current_head,
    latest_version,
)
from app.store import CONFLICT_HTTP_STATUS, ConflictError

store = store_module.store
ASSAY_CURRENT_TABLE = store_module.ASSAY_CURRENT_TABLE
SAMPLE_TRACE_TABLE = store_module.SAMPLE_TRACE_TABLE
REPORT_PANEL_TABLE = store_module.REPORT_PANEL_TABLE

REQUIRED_FIELDS = ["化验编号", "样品编号", "元素名称"]
# 录入/复检时允许携带的业务字段
WRITABLE_FIELDS = ["化验值", "单位", "化验方法", "化验日期"]

# 兼容旧前端/旧客户端的动作名
ACTION_ALIAS = {
    "录入结果": "submit_result",
    "审核通过": "confirm",
    "退回修改": "return",
}
STATUS_BY_ACTION = {
    "submit_result": "已录入",
    "confirm": CONFIRMED_STATUS,
    "return": RETURNED_STATUS,
}


class AssayService:
    # ---------------- 查询（走投影，保证列表/详情/追溯/面板口径一致） ----------------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(ASSAY_CURRENT_TABLE))
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("化验编号", ""))]
        if status:
            rows = [row for row in rows if row.get("结果状态") == status or row.get("status") == status]
        rows.sort(key=lambda row: str(row.get("化验编号", "")))
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        # 详情按化验编号（投影行 id）返回当前结论，并附带全部历史版本。
        head = store.find(ASSAY_CURRENT_TABLE, entry_id)
        if head is None:
            return None
        return self.detail_by_no(str(head["化验编号"]))

    def detail_by_no(self, assay_no: str) -> dict[str, Any] | None:
        head = current_head(store, assay_no)
        if head is None:
            return None
        current_row = next(
            (row for row in store.rows(ASSAY_CURRENT_TABLE) if str(row.get("化验编号")) == assay_no),
            None,
        )
        history = sorted(
            (
                {field: row.get(field) for field in ["id", "化验编号", "样品编号", *ELEMENT_FIELDS,
                                                     "结果状态", "status", VERSION_FIELD,
                                                     "confirmed_at", "confirmed_by"]}
                for row in store.rows(MODULE) if str(row.get("化验编号")) == assay_no
            ),
            key=lambda row: int(row[VERSION_FIELD]),
            reverse=True,
        )
        detail: dict[str, Any] = dict(current_row or head)
        detail["history"] = history
        return detail

    def list_versions(self, assay_no: str) -> list[dict[str, Any]]:
        return sorted(
            (row for row in store.rows(MODULE) if str(row.get("化验编号")) == assay_no),
            key=lambda row: int(row.get(VERSION_FIELD, 1)),
            reverse=True,
        )

    def trace_list(self, *, sample_no: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(SAMPLE_TRACE_TABLE)
        if sample_no:
            rows = [row for row in rows if sample_no in str(row.get("样品编号", ""))]
        return sorted(rows, key=lambda row: str(row.get("样品编号", "")))

    def report_panel(self) -> list[dict[str, Any]]:
        return sorted(store.rows(REPORT_PANEL_TABLE), key=lambda row: str(row.get("化验编号", "")))

    # ---------------- 登记 ----------------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        assay_no = str(values["化验编号"]).strip()
        if current_head(store, assay_no) is not None:
            return None, [f"化验编号 {assay_no} 已存在，请使用复检产生新版本"]
        with store.transaction() as tx:
            ledger = store.rows(MODULE)
            entry_id = max((int(row.get("id", 0)) for row in ledger), default=0) + 1
            payload = self._build_version_payload(assay_no, values, version=1, entry_id=entry_id)
            payload["status"] = "待录入"
            payload["结果状态"] = "待录入"
            tx.emit(EVT_VERSION_CREATED, f"assay:{assay_no}", payload, request_id=self._request_id(values))
            tx.result_builder = lambda: self.detail_by_no(assay_no)
        return tx.result, []

    # ---------------- 动作：录入结果 / 复检 ----------------

    def submit_result(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, int]:
        current = store.find(ASSAY_CURRENT_TABLE, entry_id)
        if current is None:
            return None, f"化验结果 {entry_id} 不存在或已归档", 404
        return self._submit_by_no(str(current["化验编号"]), values)

    def _submit_by_no(
        self, assay_no: str, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, int]:
        request_id = self._request_id(values)
        cached = store.cached_result(request_id)
        if cached is not None:
            if cached.get("replayed"):
                return self.detail_by_no(assay_no), "化验结果已录入（断连复位后幂等重放）", 200
            return cached.get("entry"), cached.get("message", "化验结果已录入（重复请求已幂等跳过）"), 200

        with store.transaction() as tx:
            rows = self.list_versions(assay_no)
            if not rows:
                return None, f"化验编号 {assay_no} 不存在或已归档", 404
            head = latest_version(rows)
            assert head is not None
            head_status = str(head.get("status"))
            # 最近确认的复检是当前结论：再次录入视为复检，产生新版本，不覆盖已确认结果。
            retest = head_status == CONFIRMED_STATUS
            version = int(head.get(VERSION_FIELD, 1)) + 1 if retest else int(head.get(VERSION_FIELD, 1))
            if head_status in (RETURNED_STATUS, PENDING_CONFIRM_STATUS):
                # 退回修改 / 待确认复检：只修订当前草稿，沿用其版本号，不偷开新版本。
                version = int(head.get(VERSION_FIELD, 1))

            base = dict(head)
            base.update({field: values.get(field) for field in WRITABLE_FIELDS if values.get(field) is not None})
            if not str(base.get("化验值") or "").strip():
                return None, "化验值不能为空，请先录入结果", 400

            entry_id = (
                max((int(row.get("id", 0)) for row in store.rows(MODULE)), default=0) + 1
                if retest else int(head["id"])
            )
            if head_status == CONFIRMED_STATUS:
                new_status = PENDING_CONFIRM_STATUS
            elif head_status == PENDING_CONFIRM_STATUS:
                new_status = PENDING_CONFIRM_STATUS
            else:
                new_status = "已录入"
            payload = self._build_version_payload(assay_no, base, version=version, entry_id=entry_id)
            payload["status"] = new_status
            payload["结果状态"] = new_status
            tx.emit(EVT_VERSION_CREATED, f"assay:{assay_no}", payload, request_id=request_id)
            tx.result_builder = lambda: self.detail_by_no(assay_no)
        message = "复检结果已录入，待确认" if retest else "化验结果已录入"
        store.remember_result(request_id, {"entry": tx.result, "message": message})
        return tx.result, message, 200

    # ---------------- 动作：确认结论（乐观锁 + 并发只允许一个成功） ----------------

    def confirm(
        self,
        entry_id: int,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str, int]:
        current = store.find(ASSAY_CURRENT_TABLE, entry_id)
        if current is None:
            return None, f"化验结果 {entry_id} 不存在或已归档", 404
        assay_no = str(current["化验编号"])
        request_id = self._request_id(values)

        cached = store.cached_result(request_id)
        if cached is not None:
            if cached.get("replayed"):
                return self.detail_by_no(assay_no), "确认结论已生效（断连复位后幂等重放）", 200
            return cached.get("entry"), cached.get("message", "确认结论已生效（重复请求已幂等跳过）"), 200

        expect_version = values.get("expected_version")
        with store.transaction() as tx:
            rows = self.list_versions(assay_no)
            if not rows:
                return None, f"化验编号 {assay_no} 不存在或已归档", 404

            target_version = int(values["version"]) if values.get("version") not in (None, "") else int(
                latest_version(rows).get(VERSION_FIELD, 1)
            )
            target = next((row for row in rows if int(row.get(VERSION_FIELD, 1)) == target_version), None)
            if target is None:
                return None, f"化验编号 {assay_no} 不存在版本 v{target_version}", 404

            # 并发确认：已被确认过的版本只允许同一 request_id 重放，新请求一律失败，
            # 未成功的请求不能覆盖已确认结果。
            if str(target.get("status")) == CONFIRMED_STATUS:
                raise ConflictError(
                    f"化验编号 {assay_no} 版本 v{target_version} 已确认，并发请求只有一个能成功"
                )

            if expect_version not in (None, ""):
                current_confirmed = current.get("confirmed_version")
                if int(expect_version) != int(current_confirmed or 0):
                    raise ConflictError(
                        f"化验编号 {assay_no} 的结论版本已变化（当前确认 v{current_confirmed or 0}），请刷新后重试"
                    )

            import time
            payload = {
                "id": int(target["id"]),
                "化验编号": assay_no,
                "样品编号": target.get("样品编号"),
                VERSION_FIELD: target_version,
                **{field: target.get(field) for field in ELEMENT_FIELDS},
                "confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "confirmed_by": str(values.get("confirmed_by") or "当前值班"),
                "remark": values.get("remark"),
            }
            tx.emit(EVT_CONFIRMED, f"assay:{assay_no}", payload, request_id=request_id)
            tx.result_builder = lambda: self.detail_by_no(assay_no)

        message = f"化验编号 {assay_no} 版本 v{target_version} 已确认"
        store.remember_result(request_id, {"entry": tx.result, "message": message})
        return tx.result, message, 200

    # ---------------- 动作：退回修改（不允许退回已确认结论） ----------------

    def return_for_revision(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, int]:
        current = store.find(ASSAY_CURRENT_TABLE, entry_id)
        if current is None:
            return None, f"化验结果 {entry_id} 不存在或已归档", 404
        assay_no = str(current["化验编号"])
        request_id = self._request_id(values)
        cached = store.cached_result(request_id)
        if cached is not None:
            return cached.get("entry"), cached.get("message", "退回结果已生效（重复请求已幂等跳过）"), 200

        with store.transaction() as tx:
            rows = self.list_versions(assay_no)
            head = latest_version(rows)
            if head is None:
                return None, f"化验编号 {assay_no} 不存在或已归档", 404
            if str(head.get("status")) == CONFIRMED_STATUS:
                raise ConflictError(
                    f"化验编号 {assay_no} 版本 v{head.get(VERSION_FIELD)} 已确认，不能退回；如需修正请发起复检"
                )
            payload = {"化验编号": assay_no, VERSION_FIELD: int(head.get(VERSION_FIELD, 1)),
                       "remark": values.get("remark")}
            tx.emit(EVT_VERSION_RETURNED, f"assay:{assay_no}", payload, request_id=request_id)
            tx.result_builder = lambda: self.detail_by_no(assay_no)
        store.remember_result(request_id, {"entry": tx.result, "message": "化验结果已退回修改"})
        return tx.result, "化验结果已退回修改", 200

    # ---------------- 兼容旧动作分发 ----------------

    def run_action(self, entry_id: int, action: str, values: dict[str, Any] | None = None):
        values = values or {}
        canonical = ACTION_ALIAS.get(action, action)
        if canonical == "submit_result":
            return self.submit_result(entry_id, values)
        if canonical == "confirm":
            return self.confirm(entry_id, values)
        if canonical == "return":
            return self.return_for_revision(entry_id, values)
        return None, f"动作「{action}」不属于化验数据可执行范围", 400

    # ---------------- 工具 ----------------

    @staticmethod
    def _request_id(values: dict[str, Any]) -> str:
        request_id = str(values.get("request_id") or "").strip()
        if request_id:
            return request_id
        # 未显式携带时生成；调用方重试应沿用同一 request_id 才能幂等。
        import uuid
        return f"req:{uuid.uuid4().hex}"

    @staticmethod
    def _build_version_payload(
        assay_no: str, values: dict[str, Any], *, version: int, entry_id: int
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": entry_id,
            "化验编号": assay_no,
            "样品编号": values.get("样品编号"),
            VERSION_FIELD: version,
        }
        for field in ELEMENT_FIELDS:
            payload[field] = values.get(field)
        return payload
