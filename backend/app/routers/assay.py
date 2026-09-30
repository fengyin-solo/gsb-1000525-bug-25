"""化验数据接口：维护化验结果，覆盖录入结果、提交复检、确认结论、退回修改等动作。

一致性口径：
- 列表/详情/样品追溯/报告面板全部来自同一内核投影，刷新后不会对不上；
- 确认结论在一个事务里回写化验台账、样品追溯清单、报告数据面板并登记出站事件；
- 确认动作支持 request_id 幂等，并发确认只允许一个成功（409 表示未覆盖）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.assay import CONFLICT_ACTIONS, AssayService

router = APIRouter(prefix="/api/assay", tags=["化验数据"])

service = AssayService()

LIST_FIELDS = ["化验编号", "样品编号", "元素名称", "化验值", "单位", "化验方法", "化验日期", "结果状态"]
STATUSES = ["待录入", "已录入", "已确认", "已退回"]
CONFIRM_ACTIONS = {"确认结论", "审核通过", "confirm"}


def _resolve_request_id(payload: EntryPayload, request: Request) -> str | None:
    values = payload.values or {}
    rid = values.get("request_id") or values.get("请求ID") or request.headers.get("Idempotency-Key")
    return str(rid).strip() if rid else None


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按化验编号检索"),
    sample_no: str | None = Query(default=None, description="按样品编号检索"),
    status: str | None = Query(default=None, description="待录入、已录入、已确认、已退回"),
    confirmed: bool | None = Query(default=None, description="是否只看已确认结论"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按化验编号、样品编号与状态过滤化验台账；台账只投影生效版本，不会重复显示。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, sample_no=sample_no, status=status, confirmed=confirmed, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/report_panel")
def report_panel() -> dict[str, Any]:
    """报告数据面板：已确认结论、待确认、退回待复检及确认明细，确认后即时反映。"""
    return service.report_panel()


@router.post("/outbound/replay")
def replay_outbound() -> dict[str, Any]:
    """连接断开复位后重放未投递的出站确认事件（按 event_id 幂等，不重复通知）。"""
    return {"ok": True, **service.replay_outbound()}


@router.get("/trace")
def trace_entries(
    sample_no: str | None = Query(default=None, description="按样品编号过滤"),
    keyword: str | None = Query(default=None, description="样品编号或化验编号关键字"),
) -> dict[str, Any]:
    """化验侧样品追溯清单：只包含已确认结论，同一化验编号只显示最近确认版本。"""
    items = service.traceability(sample_no=sample_no, keyword=keyword)
    return {"module": "assay", "total": len(items), "items": items}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出化验数据台账：当前生效版本的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "assay", "total": total, "items": items}


@router.get("/{entry_key}", response_model=dict)
def get_entry(entry_key: str) -> dict:
    """读取单条化验结果明细，含按版本保留的历史复检结果。"""
    entry = service.get_entry(entry_key)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"化验结果 {entry_key} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条化验结果（v1 待录入），缺字段或重复编号时说明原因。"""
    entry, problem = service.create_entry(payload.values)
    if entry is None:
        if isinstance(problem, list):
            return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(problem)}")
        return ActionResult(ok=False, message=str(problem))
    return ActionResult(ok=True, message="化验结果已登记", entry=entry)


@router.post("/{entry_key}/actions", response_model=ActionResult)
def run_action(entry_key: str, payload: EntryPayload, request: Request) -> ActionResult:
    """对单个化验编号执行录入结果、提交复检、确认结论、退回修改。

    - 确认/复检动作与已确认状态冲突时返回 409，未成功请求不会覆盖已确认结果；
    - 携带 request_id 或 Idempotency-Key 的请求重连重放时返回首次结论。
    """
    action = str(payload.values.get("action") or "").strip()
    request_id = _resolve_request_id(payload, request)
    entry, message = service.run_action(entry_key, action, payload.values, request_id=request_id)
    if entry is None:
        status_code = 409 if action in CONFLICT_ACTIONS else 400
        raise HTTPException(status_code=status_code, detail=message)
    return ActionResult(ok=True, message=message, entry=entry)
