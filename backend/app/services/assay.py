"""化验数据应用服务：路由与领域内核之间的薄封装。

- 同一化验编号以最近确认复检为准（投影由 kernel 统一构建，列表/详情/追溯一致）；
- 历史版本只读保留，缺版本号存量记录在启动时迁移为 v1；
- 确认命令携带 request_id：重试幂等回放，并发确认只有一个成功。
"""
from __future__ import annotations

from typing import Any

from app import assay_kernel
from app.domain import assay as domain

MODULE = "assay"
REQUIRED_FIELDS = ["化验编号", "样品编号", "元素名称"]
STATUS_ORDER = ["待录入", "已录入", "已确认", "已退回"]
ACTION_RULES = {"录入结果": "已录入", "确认结论": "已确认", "审核通过": "已确认", "提交复检": "已录入", "退回修改": "已退回"}
NEGATIVE_ACTIONS: list[str] = []

CONFLICT_ACTIONS = {"确认结论", "审核通过", "confirm", "提交复检", "resubmit"}


class AssayService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        sample_no: str | None = None,
        status: str | None = None,
        confirmed: bool | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        return assay_kernel.kernel.ledger(
            keyword=keyword,
            sample_no=sample_no,
            status=status,
            confirmed=confirmed,
            page=page,
            size=size,
        )

    def get_entry(self, entry_key: str | int) -> dict[str, Any] | None:
        try:
            return assay_kernel.kernel.get_order(str(entry_key))
        except domain.DomainConflict:
            return None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str] | str]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        try:
            result = assay_kernel.kernel.execute("create", str(values["化验编号"]), values,
                                                 request_id=_request_id(values))
        except domain.DomainConflict as exc:
            return None, str(exc)
        return result, []

    def run_action(
        self,
        entry_key: str | int,
        action: str,
        values: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        try:
            result = assay_kernel.kernel.execute(action, str(entry_key), values, request_id=request_id)
        except domain.DomainConflict as exc:
            return None, str(exc)
        replayed = result.get("replayed")
        suffix = "（重复请求，已幂等回放首次结论）" if replayed else ""
        return result, f"化验结果已{action}{suffix}"

    def traceability(self, *, sample_no: str | None = None, keyword: str | None = None) -> list[dict[str, Any]]:
        return assay_kernel.kernel.traceability(sample_no=sample_no, keyword=keyword)

    def report_panel(self) -> dict[str, Any]:
        return assay_kernel.kernel.report_panel()

    def replay_outbound(self) -> dict[str, int]:
        return assay_kernel.kernel.replay_outbound()


def _request_id(values: dict[str, Any]) -> str | None:
    rid = values.get("request_id") or values.get("请求ID")
    return str(rid) if rid else None
