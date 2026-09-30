"""化验数据接口：版本化复检、确认结论，以及台账/样品追溯/报告面板三处投影的读取。

确认、出站事件与页面投影在服务层同一事务内提交；冲突（并发确认）返回 409。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.assay import AssayService
from app.store import CONFLICT_HTTP_STATUS, ConflictError

router = APIRouter(prefix="/api/assay", tags=["化验数据"])

service = AssayService()

LIST_FIELDS = ["化验编号", "样品编号", "元素名称", "化验值", "单位", "化验方法", "化验日期", "结果状态"]
STATUSES = ["待录入", "已录入", "待确认", "已确认", "已退回"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按化验编号检索"),
    status: str | None = Query(default=None, description="待录入、已录入、待确认、已确认、已退回"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按化验编号与状态过滤化验台账当前投影；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/trace", response_model=list[dict])
def sample_trace(
    sample_no: str | None = Query(default=None, description="按样品编号过滤追溯清单"),
) -> list[dict[str, Any]]:
    """样品追溯清单投影：只包含最近确认复检结论。"""
    return service.trace_list(sample_no=sample_no)


@router.get("/report-panel", response_model=list[dict])
def report_panel() -> list[dict[str, Any]]:
    """报告数据面板投影：每个化验编号一条已确认结论。"""
    return service.report_panel()


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出化验台账当前清单：返回全量当前投影数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "assay", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict[str, Any]:
    """读取单条化验结果详情（当前结论 + 全部历史版本）；不存在时给出可读错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"化验结果 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/versions", response_model=list[dict])
def list_versions(entry_id: int) -> list[dict[str, Any]]:
    """读取同一化验编号的全部历史版本（历史结果按原版本保留）。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"化验结果 {entry_id} 不存在或已归档")
    return list(entry.get("history", []))


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条化验结果（v1，待录入），缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="化验结果已登记", entry=entry)


@router.post("/{entry_id}/results", response_model=ActionResult)
def submit_result(entry_id: int, payload: EntryPayload) -> ActionResult:
    """录入结果；对已确认编号调用即产生复检新版本，绝不覆盖已确认版本。"""
    try:
        entry, message, code = service.submit_result(entry_id, payload.values)
    except ConflictError as exc:
        raise HTTPException(status_code=CONFLICT_HTTP_STATUS, detail=str(exc)) from exc
    if entry is None:
        raise HTTPException(status_code=code, detail=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/confirm", response_model=ActionResult)
def confirm_result(entry_id: int, payload: EntryPayload) -> ActionResult:
    """确认结论：并发确认只允许一个成功，重复/重放请求幂等返回首次结果。"""
    try:
        entry, message, code = service.confirm(entry_id, payload.values)
    except ConflictError as exc:
        raise HTTPException(status_code=CONFLICT_HTTP_STATUS, detail=str(exc)) from exc
    if entry is None:
        raise HTTPException(status_code=code, detail=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/return", response_model=ActionResult)
def return_result(entry_id: int, payload: EntryPayload) -> ActionResult:
    """退回修改：已确认结论不可退回，需发起复检生成新版本。"""
    try:
        entry, message, code = service.return_for_revision(entry_id, payload.values)
    except ConflictError as exc:
        raise HTTPException(status_code=CONFLICT_HTTP_STATUS, detail=str(exc)) from exc
    if entry is None:
        raise HTTPException(status_code=code, detail=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """兼容旧客户端的统一动作入口（录入结果/审核通过/退回修改）。"""
    action = str(payload.values.get("action") or "").strip()
    try:
        entry, message, code = service.run_action(entry_id, action, payload.values)
    except ConflictError as exc:
        raise HTTPException(status_code=CONFLICT_HTTP_STATUS, detail=str(exc)) from exc
    if entry is None:
        if code == 404:
            return ActionResult(ok=False, message=message)
        raise HTTPException(status_code=code, detail=message)
    return ActionResult(ok=True, message=message, entry=entry)
