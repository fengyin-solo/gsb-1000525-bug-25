"""化验领域：事件规约（reducer）、读模型投影与存量迁移。

核心约定：
- 聚合根是「化验编号」，同一化验编号的每次复检都是一个新版本，历史版本永不删除；
- 投影只认「已确认」版本：新复检未确认前，台账/追溯/报告继续展示上一确认版本，
  因此旧结果不可能覆盖或重复显示在投影里；
- reduce() 是纯函数：给定状态和事件必然得到相同结果，重放 journal 即可完整恢复。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# ---- 状态流转 ----------------------------------------------------------------
STATUS_DRAFT = "待录入"
STATUS_ENTERED = "已录入"
STATUS_CONFIRMED = "已确认"
STATUS_RETURNED = "已退回"

# 兼容旧页面的状态名称
STATUS_ALIAS = {
    "已审核": STATUS_CONFIRMED,
    "待审核": STATUS_ENTERED,
}

# 领域事件
EV_CREATED = "assay.created"
EV_RESULT_ENTERED = "assay.result_entered"
EV_RESUBMITTED = "assay.resubmitted"
EV_CONFIRMED = "assay.confirmed"
EV_RETURNED = "assay.returned"
EV_MIGRATED = "assay.migrated"

VALUE_FIELDS = ("元素名称", "化验值", "单位", "化验方法", "化验日期")


class DomainConflict(Exception):
    """与当前聚合状态冲突的命令（例如重复确认同一版本）。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_status(status: Any) -> str:
    text = str(status or "").strip()
    return STATUS_ALIAS.get(text, text)


def initial_state() -> dict[str, Any]:
    return {"orders": {}, "requests": {}}


# ---- 版本与聚合 ---------------------------------------------------------------

def _new_version(version: int, *, source: str) -> dict[str, Any]:
    return {
        "version": version,
        "source": source,
        "status": STATUS_DRAFT,
        "confirmed": False,
        "化验值": None,
        "单位": None,
        "化验方法": None,
        "化验日期": None,
        "元素名称": None,
        "entered_at": None,
        "confirmed_at": None,
        "returned_at": None,
    }


def _get_order(state: dict[str, Any], assay_no: str) -> dict[str, Any]:
    order = state["orders"].get(assay_no)
    if order is None:
        raise DomainConflict(f"化验编号 {assay_no} 不存在或已归档")
    return order


def _current(order: dict[str, Any]) -> dict[str, Any]:
    return order["versions"][-1]


def reduce(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """把单个事件应用到状态；返回状态本身（原地演进，调用前应已完成试算）。"""
    etype = event.get("type")
    data = event.get("data", {})
    at = event.get("at") or now_iso()

    if etype == EV_CREATED:
        assay_no = str(data["化验编号"])
        if assay_no in state["orders"]:
            raise DomainConflict(f"化验编号 {assay_no} 已存在")
        version = _new_version(1, source="录入")
        version["元素名称"] = data.get("元素名称")
        state["orders"][assay_no] = {
            "化验编号": assay_no,
            "样品编号": data.get("样品编号"),
            "current_version": 1,
            "confirmed_version": None,
            "status": STATUS_DRAFT,
            "created_at": at,
            "updated_at": at,
            "versions": [version],
        }
        return state

    order = _get_order(state, str(data.get("化验编号", "")))
    version = _current(order)

    if etype == EV_MIGRATED:
        # 存量记录回填：缺版本号一律按 v1 落账
        legacy = _new_version(int(data.get("version") or 1), source="迁移")
        legacy["version"] = int(data.get("version") or 1)
        for field in VALUE_FIELDS:
            legacy[field] = data.get(field)
        legacy["化验值"] = data.get("化验值")
        legacy_status = normalize_status(data.get("status"))
        legacy["status"] = legacy_status or STATUS_DRAFT
        legacy["entered_at"] = data.get("化验日期") or at
        order["versions"] = [legacy]
        order["current_version"] = legacy["version"]
        order["样品编号"] = data.get("样品编号") or order.get("样品编号")
        if legacy_status == STATUS_CONFIRMED:
            legacy["confirmed"] = True
            legacy["confirmed_at"] = at
            order["confirmed_version"] = legacy["version"]
        order["status"] = legacy["status"]
        order["updated_at"] = at
        return state

    if etype == EV_RESULT_ENTERED:
        if version["confirmed"]:
            raise DomainConflict("当前版本已确认，不能直接改写；请提交复检生成新版本")
        if version["status"] != STATUS_DRAFT:
            raise DomainConflict("当前版本已录入结果，如需修改请先退回或提交复检")
        for field in ("化验值", "单位", "化验方法", "化验日期", "元素名称"):
            if data.get(field) is not None:
                version[field] = data[field]
        version["status"] = STATUS_ENTERED
        version["entered_at"] = at
        order["status"] = STATUS_ENTERED
        order["updated_at"] = at
        return state

    if etype == EV_RESUBMITTED:
        if version["status"] not in (STATUS_RETURNED, STATUS_CONFIRMED):
            raise DomainConflict("只有已退回或已确认的结果可以提交复检")
        new_ver = _new_version(len(order["versions"]) + 1, source="复检")
        for field in ("化验值", "单位", "化验方法", "化验日期", "元素名称"):
            new_ver[field] = data.get(field, version.get(field))
        if data.get("化验值"):
            new_ver["status"] = STATUS_ENTERED
            new_ver["entered_at"] = at
        order["versions"].append(new_ver)
        order["current_version"] = new_ver["version"]
        # 投影口径仍是上一确认版本；当前状态反映有一版待确认复检
        order["status"] = new_ver["status"]
        order["updated_at"] = at
        return state

    if etype == EV_RETURNED:
        if version["confirmed"]:
            raise DomainConflict("已确认结论不能退回，如需更正请提交复检")
        if version["status"] != STATUS_ENTERED:
            raise DomainConflict("只有已录入待确认的结果可以退回")
        version["status"] = STATUS_RETURNED
        version["returned_at"] = at
        if data.get("退回原因"):
            version["退回原因"] = data["退回原因"]
        order["status"] = STATUS_RETURNED
        order["updated_at"] = at
        return state

    if etype == EV_CONFIRMED:
        if version["confirmed"]:
            # 并发下第二个确认会走到这里：已确认结果不允许被覆盖
            raise DomainConflict("该版本已确认，重复确认不会覆盖既有结论")
        if version["status"] != STATUS_ENTERED:
            raise DomainConflict("只有已录入的结果可以确认结论")
        version["confirmed"] = True
        version["status"] = STATUS_CONFIRMED
        version["confirmed_at"] = at
        if data.get("确认人"):
            version["确认人"] = data["确认人"]
        if data.get("确认结论"):
            version["确认结论"] = data["确认结论"]
        order["confirmed_version"] = version["version"]
        order["status"] = STATUS_CONFIRMED
        order["updated_at"] = at
        return state

    raise DomainConflict(f"未知事件类型：{etype}")


# ---- 命令 -> 事件（校验在此发生，落账由 kernel 统一提交） ------------------------

def command_events(
    state: dict[str, Any],
    command: str,
    assay_no: str,
    data: dict[str, Any],
    request_id: str,
) -> list[dict[str, Any]]:
    """根据当前状态把命令翻译成事件；非法命令直接抛 DomainConflict。"""
    base = {"request_id": request_id, "化验编号": assay_no}
    at = now_iso()

    if command == "create":
        assay_no = str(data.get("化验编号") or "").strip()
        if not assay_no:
            raise DomainConflict("缺少化验编号")
        if assay_no in state["orders"]:
            raise DomainConflict(f"化验编号 {assay_no} 已存在")
        return [{"type": EV_CREATED, "at": at, "data": {
            "化验编号": assay_no,
            "样品编号": data.get("样品编号"),
            "元素名称": data.get("元素名称"),
        }}]

    if command in ("录入结果", "enter_result"):
        payload = {**base, **{k: data.get(k) for k in ("化验值", "单位", "化验方法", "化验日期", "元素名称")}}
        return [{"type": EV_RESULT_ENTERED, "at": at, "data": payload}]

    if command in ("提交复检", "resubmit"):
        payload = {**base, **{k: data.get(k) for k in ("化验值", "单位", "化验方法", "化验日期", "元素名称", "复检原因")}}
        return [{"type": EV_RESUBMITTED, "at": at, "data": payload}]

    if command in ("退回修改", "return"):
        return [{"type": EV_RETURNED, "at": at, "data": {**base, "退回原因": data.get("退回原因")}}]

    if command in ("确认结论", "审核通过", "confirm"):
        return [{"type": EV_CONFIRMED, "at": at, "data": {
            **base,
            "确认人": data.get("确认人"),
            "确认结论": data.get("确认结论"),
        }}]

    raise DomainConflict(f"动作「{command}」不属于化验数据可执行范围")


# ---- 读模型投影 ---------------------------------------------------------------

def _effective_version(order: dict[str, Any]) -> dict[str, Any]:
    """投影取值版本：优先最近确认复检，否则取当前在途版本。"""
    target = order.get("confirmed_version")
    if target is not None:
        for ver in reversed(order["versions"]):
            if ver["version"] == target:
                return ver
    return _current(order)


def order_detail(order: dict[str, Any]) -> dict[str, Any]:
    eff = _effective_version(order)
    return {
        "化验编号": order["化验编号"],
        "样品编号": order["样品编号"],
        "元素名称": eff.get("元素名称"),
        "化验值": eff.get("化验值"),
        "单位": eff.get("单位"),
        "化验方法": eff.get("化验方法"),
        "化验日期": eff.get("化验日期"),
        "结果状态": order["status"],
        "当前版本": order["current_version"],
        "确认版本": order.get("confirmed_version"),
        "是否已确认": order.get("confirmed_version") is not None,
        "确认时间": eff.get("confirmed_at"),
        "更新时间": order.get("updated_at"),
        "历史版本": [dict(ver) for ver in order["versions"]],
    }


def ledger_row(order: dict[str, Any]) -> dict[str, Any]:
    detail = order_detail(order)
    return {
        "id": order["化验编号"],
        "化验编号": detail["化验编号"],
        "样品编号": detail["样品编号"],
        "元素名称": detail["元素名称"],
        "化验值": detail["化验值"],
        "单位": detail["单位"],
        "化验方法": detail["化验方法"],
        "化验日期": detail["化验日期"],
        "结果状态": detail["结果状态"],
        "当前版本": detail["当前版本"],
        "确认版本": detail["确认版本"],
        "是否已确认": detail["是否已确认"],
        "确认时间": detail["确认时间"],
    }


def build_projections(state: dict[str, Any]) -> dict[str, Any]:
    """在同一事务里由状态重建三个投影，天然与台账一致。"""
    ledger: list[dict[str, Any]] = []
    traceability: list[dict[str, Any]] = []
    confirmed_rows: list[dict[str, Any]] = []

    pending_enter = 0
    returned = 0
    samples: set[str] = set()

    for assay_no in sorted(state["orders"]):
        order = state["orders"][assay_no]
        ledger.append(ledger_row(order))
        if order["status"] == STATUS_ENTERED:
            pending_enter += 1
        if order["status"] == STATUS_RETURNED:
            returned += 1

        confirmed_no = order.get("confirmed_version")
        if confirmed_no is None:
            continue
        confirmed_ver = next(v for v in order["versions"] if v["version"] == confirmed_no)
        samples.add(str(order.get("样品编号") or ""))
        row = {
            "样品编号": order.get("样品编号"),
            "化验编号": assay_no,
            "元素名称": confirmed_ver.get("元素名称"),
            "化验值": confirmed_ver.get("化验值"),
            "单位": confirmed_ver.get("单位"),
            "化验方法": confirmed_ver.get("化验方法"),
            "化验日期": confirmed_ver.get("化验日期"),
            "确认版本": confirmed_no,
            "确认时间": confirmed_ver.get("confirmed_at"),
        }
        traceability.append(row)
        confirmed_rows.append(row)

    traceability.sort(key=lambda r: (str(r["样品编号"]), str(r["化验编号"])))
    confirmed_rows.sort(key=lambda r: str(r["确认时间"]), reverse=True)

    panel = {
        "已确认结论": len(confirmed_rows),
        "待确认结果": pending_enter,
        "退回待复检": returned,
        "涉及样品": len(samples),
        "items": confirmed_rows,
    }
    return {"ledger": ledger, "traceability": traceability, "report_panel": panel}


# ---- 存量迁移 -----------------------------------------------------------------

def migration_events(legacy_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把缺版本号的存量台账行迁移成 v1 事件；同化验编号只迁一次。"""
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in legacy_rows:
        assay_no = str(row.get("化验编号") or "").strip()
        if not assay_no or assay_no in seen:
            continue
        seen.add(assay_no)
        at = now_iso()
        events.append({"type": EV_CREATED, "at": at, "data": {
            "化验编号": assay_no,
            "样品编号": row.get("样品编号"),
            "元素名称": row.get("元素名称"),
        }})
        events.append({"type": EV_MIGRATED, "at": at, "data": {
            "化验编号": assay_no,
            "样品编号": row.get("样品编号"),
            "version": int(row.get("版本号") or row.get("version") or 1),
            "status": normalize_status(row.get("status") or row.get("结果状态")),
            **{field: row.get(field) for field in VALUE_FIELDS},
        }})
    return events
