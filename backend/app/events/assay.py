"""化验领域事件、投影器与存量迁移。

事件（append-only，同一事件重放任意次结果一致）：

- ``assay.version_created``  登记/录入/复检产生一个新版本行（历史版本原样保留）
- ``assay.version_returned`` 未确认版本被退回修改（只影响草稿态当前值）
- ``assay.confirmed``        确认结论：以指定版本为准，刷新台账/追溯/面板三处投影

投影表：

- ``assay``               化验台账，一个化验编号每个版本一行，确认结论不可变
- ``assay_current``       列表/详情当前态，每个化验编号一行（最近确认版本优先）
- ``sample_trace``        样品追溯清单，按样品编号聚合
- ``report_panel_assay``  报告数据面板，按化验编号汇总确认结论
- ``sample_registry``     送检样品台账回写最近确认的化验结论

所有投影函数第一个参数都是 ``Store`` 实例，注册时绑定，避免依赖尚未构造完的
全局单例；投影全部按自然键（化验编号 + 版本号）幂等 upsert，支持断连复位重放。
"""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from app.store import Store

MODULE = "assay"

VERSION_FIELD = "version"
CONFIRMED_STATUS = "已确认"
PENDING_CONFIRM_STATUS = "待确认"
RETURNED_STATUS = "已退回"

# 旧版状态 -> 现行口径的兼容映射
STATUS_ALIAS = {"已审核": CONFIRMED_STATUS}

ELEMENT_FIELDS = ["元素名称", "化验值", "单位", "化验方法", "化验日期"]

# 工作流状态权威字段是 status；结果状态在投影里镜像 status 供列表列展示。
WORKFLOW_STATUSES = {"待录入", "已录入", "待确认", "已确认", "已退回"}

# 事件类型常量，供服务与投影注册共用
EVT_VERSION_CREATED = "assay.version_created"
EVT_VERSION_RETURNED = "assay.version_returned"
EVT_CONFIRMED = "assay.confirmed"


# =========================== 工具函数 ===========================

def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _versions_of(store: "Store", assay_no: str) -> list[dict[str, Any]]:
    return [row for row in store.rows(MODULE) if str(row.get("化验编号")) == assay_no]


def latest_version(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    return max(rows, key=lambda row: int(row.get(VERSION_FIELD, 1)))


def confirmed_versions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if str(row.get("status")) == CONFIRMED_STATUS]


def current_head(store: "Store", assay_no: str) -> dict[str, Any] | None:
    """同一化验编号的当前结论：最近确认复检为准，没有确认结果时取最高版本。"""
    rows = _versions_of(store, assay_no)
    confirmed = confirmed_versions(rows)
    if confirmed:
        return latest_version(confirmed)
    return latest_version(rows)


def _upsert(store: "Store", table: str, key_field: str, key_value: str) -> dict[str, Any]:
    for row in store.rows(table):
        if str(row.get(key_field)) == str(key_value):
            return row
    row = {"id": _next_id(store.rows(table)), key_field: key_value}
    store.rows(table).append(row)
    return row


# =========================== 投影：化验台账当前态 ===========================

def _refresh_current(store: "Store", assay_no: str) -> None:
    rows = _versions_of(store, assay_no)
    head = current_head(store, assay_no)
    table = store.rows("assay_current")
    target = next((row for row in table if str(row.get("化验编号")) == assay_no), None)
    if target is None:
        target = {"id": _next_id(table), "化验编号": assay_no}
        table.append(target)

    if head is not None:
        confirmed = confirmed_versions(rows)
        latest_confirmed = latest_version(confirmed) if confirmed else None
        result_status = str(head.get("status") or "")
        is_confirmed = result_status == CONFIRMED_STATUS
        target.update({
            "样品编号": head.get("样品编号"),
            "元素名称": head.get("元素名称"),
            "化验值": head.get("化验值"),
            "单位": head.get("单位"),
            "化验方法": head.get("化验方法"),
            "化验日期": head.get("化验日期"),
            "结果状态": result_status,
            "status": result_status,
            VERSION_FIELD: int(head.get(VERSION_FIELD, 1)),
            "latest_version": int(latest_version(rows).get(VERSION_FIELD, 1)),
            "confirmed_version": int(latest_confirmed[VERSION_FIELD]) if latest_confirmed else None,
            "history_count": len(rows),
            "pending": not is_confirmed,
            "abnormal": result_status == RETURNED_STATUS,
            "confirmed_at": head.get("confirmed_at"),
            "confirmed_by": head.get("confirmed_by"),
        })


def _project_version_created(store: "Store", event: dict[str, Any]) -> None:
    payload = event["payload"]
    rows = store.rows(MODULE)
    assay_no = str(payload["化验编号"])
    version = int(payload[VERSION_FIELD])

    existing = next(
        (row for row in rows
         if str(row.get("化验编号")) == assay_no and int(row.get(VERSION_FIELD, 1)) == version),
        None,
    )
    if existing is None:
        entry: dict[str, Any] = {"id": int(payload["id"])}
        entry.update({field: payload.get(field) for field in ["化验编号", "样品编号", *ELEMENT_FIELDS]})
        entry[VERSION_FIELD] = version
        entry["status"] = payload.get("status") or "待录入"
        # 新台账行的结果状态列镜像工作流状态；存量旧文本由迁移分支原样保留。
        entry["结果状态"] = payload.get("结果状态") or entry["status"]
        entry.setdefault("pending", entry["status"] != CONFIRMED_STATUS)
        entry.setdefault("abnormal", entry["status"] == RETURNED_STATUS)
        rows.append(entry)
    else:
        # 同版本重放：草稿值可以刷新（录入结果），确认后的字段保持不可变。
        if str(existing.get("status")) != CONFIRMED_STATUS:
            for field in ELEMENT_FIELDS:
                if payload.get(field) is not None:
                    existing[field] = payload[field]
            if payload.get("status"):
                existing["status"] = payload["status"]
                existing["结果状态"] = payload.get("结果状态") or payload["status"]
    _refresh_current(store, assay_no)


def _project_version_returned(store: "Store", event: dict[str, Any]) -> None:
    payload = event["payload"]
    assay_no = str(payload["化验编号"])
    version = int(payload[VERSION_FIELD])
    for row in _versions_of(store, assay_no):
        if int(row.get(VERSION_FIELD, 1)) != version:
            continue
        if str(row.get("status")) != CONFIRMED_STATUS:
            row["status"] = RETURNED_STATUS
            row["结果状态"] = RETURNED_STATUS
            row["pending"] = True
            row["abnormal"] = True
    _refresh_current(store, assay_no)


# =========================== 投影：确认结论（台账/追溯/面板/送检回写） ===========================

def _write_sample_trace(
    store: "Store", head: dict[str, Any], payload: dict[str, Any]
) -> None:
    sample_no = str(head.get("样品编号") or "")
    trace = _upsert(store, "sample_trace", "样品编号", sample_no)
    trace.update({
        "送检编号": sample_no,
        "化验编号": head.get("化验编号"),
        "元素名称": head.get("元素名称"),
        "化验值": head.get("化验值"),
        "单位": head.get("单位"),
        "化验方法": head.get("化验方法"),
        "化验日期": head.get("化验日期"),
        "确认版本": int(head[VERSION_FIELD]),
        "结论": CONFIRMED_STATUS,
        "确认时间": head.get("confirmed_at"),
        "追溯状态": "化验结论已确认",
        "pending": False,
        "abnormal": False,
        "remark": payload.get("remark"),
    })
    # 回写样品登记台账（送检样品）：样品追溯清单以最近确认结论为准。
    for sample in store.rows("sample_registry"):
        if str(sample.get("送检编号")) == sample_no:
            sample["送检状态"] = "化验结论已确认"
            sample["status"] = "已出报告"
            sample["pending"] = False
            sample["最近化验编号"] = head.get("化验编号")
            sample["最近确认版本"] = int(head[VERSION_FIELD])
            sample["最近化验结论"] = (
                f"{head.get('元素名称', '')} {head.get('化验值', '')}{head.get('单位', '')}"
            ).strip()
            break


def _write_report_panel(
    store: "Store", head: dict[str, Any], payload: dict[str, Any]
) -> None:
    assay_no = str(head.get("化验编号"))
    panel = _upsert(store, "report_panel_assay", "化验编号", assay_no)
    panel.update({
        "样品编号": head.get("样品编号"),
        "元素名称": head.get("元素名称"),
        "化验值": head.get("化验值"),
        "单位": head.get("单位"),
        "化验方法": head.get("化验方法"),
        "化验日期": head.get("化验日期"),
        "确认版本": int(head[VERSION_FIELD]),
        "结论": CONFIRMED_STATUS,
        "确认时间": head.get("confirmed_at"),
        "确认人": payload.get("confirmed_by"),
        "remark": payload.get("remark"),
        "pending": False,
        "abnormal": False,
    })


def _project_confirmed(store: "Store", event: dict[str, Any]) -> None:
    payload = event["payload"]
    assay_no = str(payload["化验编号"])
    version = int(payload[VERSION_FIELD])

    rows = _versions_of(store, assay_no)
    target = next((row for row in rows if int(row.get(VERSION_FIELD, 1)) == version), None)
    if target is None:
        # 仅当确认事件先于版本事件到达的极端情况：补建台账行。
        target = {"id": int(payload.get("id") or _next_id(rows)), "化验编号": assay_no}
        rows.append(target)
    # 确认结论不可变：已确认版本的元素值不允许任何后续事件覆盖。
    for field in ELEMENT_FIELDS:
        if payload.get(field) is not None:
            target[field] = payload[field]
    target[VERSION_FIELD] = version
    target["status"] = CONFIRMED_STATUS
    target["结果状态"] = CONFIRMED_STATUS
    target["pending"] = False
    target["abnormal"] = False
    target["confirmed_at"] = payload.get("confirmed_at")
    target["confirmed_by"] = payload.get("confirmed_by")

    # 同一化验编号：只有最近确认的版本是当前结论；历史确认版本按原版本保留。
    head = current_head(store, assay_no)
    assert head is not None
    _refresh_current(store, assay_no)
    _write_sample_trace(store, head, payload)
    _write_report_panel(store, head, payload)

    # 出站箱：确认结论需要通知外部系统（如 LIMS/出站闸门）。
    store.upsert_outbox(event)


# =========================== 存量迁移：回填版本号 ===========================

def backfill_assay_versions(store: "Store") -> None:
    """存量缺少版本号的化验记录统一回填：每个化验编号的既有记录按 v1 起序。

    - 历史结果按原版本保留，迁移后每个版本独立成行；
    - 状态为「已审核」的存量行按「已确认」建立初始投影；
    - 幂等：已有版本号且投影已建立的记录不会重复迁移。
    """
    ledger = store.rows(MODULE)
    if not ledger:
        return

    # 1) 回填版本号：同一化验编号按 id 顺序 1,2,3…
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in ledger:
        assay_no = str(row.get("化验编号"))
        grouped.setdefault(assay_no, []).append(row)

    migrated_any = False
    for assay_no, rows in grouped.items():
        rows.sort(key=lambda row: int(row.get("id", 0)))
        for index, row in enumerate(rows, start=1):
            if VERSION_FIELD not in row or row[VERSION_FIELD] in (None, ""):
                row[VERSION_FIELD] = index
                migrated_any = True
            else:
                row[VERSION_FIELD] = int(row[VERSION_FIELD])
            status = str(row.get("status") or "")
            if status != CONFIRMED_STATUS:
                status = STATUS_ALIAS.get(status, status)
            # status 是权威工作流状态；结果状态列在投影里镜像，台账旧文本保留。
            row["status"] = status
            row.setdefault("pending", status != CONFIRMED_STATUS)
            row.setdefault("abnormal", status == RETURNED_STATUS)

    if not migrated_any and store.rows("assay_current"):
        return

    # 2) 依据回填后的台账建立初始投影（与事件投影共用同一套口径，保证幂等）。
    for assay_no in grouped:
        _refresh_current(store, assay_no)
        head = current_head(store, assay_no)
        if head is not None and str(head.get("status")) == CONFIRMED_STATUS:
            payload = {
                "化验编号": assay_no,
                VERSION_FIELD: int(head[VERSION_FIELD]),
                "id": int(head["id"]),
                "样品编号": head.get("样品编号"),
                **{field: head.get(field) for field in ELEMENT_FIELDS},
                "confirmed_at": head.get("confirmed_at") or head.get("化验日期"),
                "confirmed_by": head.get("confirmed_by") or "存量迁移",
                "remark": "存量记录迁移回填",
            }
            synthetic = {
                "id": f"migration:{assay_no}:v{head[VERSION_FIELD]}",
                "type": EVT_CONFIRMED,
                "aggregate": f"assay:{assay_no}",
                "request_id": f"migration:{assay_no}",
                "ts": 0,
                "payload": payload,
            }
            _project_confirmed(store, synthetic)
            # 迁移产生的出站记录视为已投递，不在重启后重复外发。
            for out in store.rows("outbox"):
                if out.get("event_id") == synthetic["id"]:
                    out["delivered"] = True


def register_projectors(store: "Store") -> None:
    """把 (store, event) 形态的投影函数绑定成只接收 event 的回调。"""
    store.register_projector(
        EVT_VERSION_CREATED, lambda event: _project_version_created(store, event)
    )
    store.register_projector(
        EVT_VERSION_RETURNED, lambda event: _project_version_returned(store, event)
    )
    store.register_projector(
        EVT_CONFIRMED, lambda event: _project_confirmed(store, event)
    )
