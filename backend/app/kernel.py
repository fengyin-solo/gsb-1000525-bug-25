"""化验领域内核：事件提交、状态恢复（重放）、出站事件 outbox。

一致性边界（一次 commit）：
1. 领域事件 + 出站事件一起追加到 journal（单次加锁写入）；
2. 内存状态推进、三个投影（台账/追溯/报告面板）同步重建；
3. 出站事件由 relay 幂等投递，失败或进程复位后扫描重放。

并发：execute 全程持同一把可重入锁；确认命令靠 reducer 的版本条件实现
compare-and-set，第二个并发确认必然拿到 DomainConflict，已确认结果不会被覆盖。
"""
from __future__ import annotations

import copy
import threading
from pathlib import Path
from typing import Any, Callable

from app.domain import assay as domain
from app.persistence import Journal, Snapshot, OutboxLog

SNAPSHOT_EVERY = 10


class AssayKernel:
    def __init__(self, data_dir: str | Path) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.journal = Journal(self.dir / "journal.jsonl")
        self.snapshot = Snapshot(self.dir / "snapshot.json")
        # 出站通道（消息总线的本地占位）：文件本身即投递事实，按 event_id 去重
        self.sink = OutboxLog(self.dir / "dispatch.log.jsonl")
        self._lock = threading.RLock()
        self._dispatcher: Callable[[dict[str, Any]], None] | None = None

        self.state = domain.initial_state()
        self.projections = domain.build_projections(self.state)
        self._recover()

    # ---- 恢复 ----------------------------------------------------------------
    def _recover(self) -> None:
        """启动恢复：快照 + journal 续放；连接（进程）复位后状态不丢。"""
        records = self.journal.read_records()
        snap = self.snapshot.load()
        state = domain.initial_state()
        applied_domain_seq = 0

        if snap and isinstance(snap.get("state"), dict):
            try:
                state = copy.deepcopy(snap["state"])
                applied_domain_seq = int(snap.get("domain_seq", 0))
            except (TypeError, ValueError):
                state = domain.initial_state()
                applied_domain_seq = 0

        for record in records:
            kind = record.get("kind")
            if kind == "domain":
                if int(record.get("seq", 0)) <= applied_domain_seq:
                    continue
                try:
                    domain.reduce(state, record["event"])
                    applied_domain_seq = int(record["seq"])
                except domain.DomainConflict:
                    # 重放期间的冲突只可能来自损坏数据：跳过坏记录但保留整体可用
                    continue
            elif kind == "request":
                # 幂等索引随领域事件同批落盘，崩溃重放后重试依然会被拦下
                state["requests"][str(record["request_id"])] = {"化验编号": record["assay_no"]}

        self.state = state
        self.projections = domain.build_projections(state)
        self._domain_seq = applied_domain_seq
        self._outbound_seq = max(
            (int(r.get("seq", 0)) for r in records if r.get("kind") == "outbound"),
            default=0,
        )

    # ---- 提交 ----------------------------------------------------------------
    def _commit_events(
        self,
        events: list[dict[str, Any]],
        *,
        outbound: list[dict[str, Any]] | None = None,
        request_id: str | None = None,
        assay_no: str | None = None,
    ) -> bool:
        """校验通过后原子追加；返回是否发生了真实提交。"""
        with self._lock:
            # 试算：先在副本上应用，任何冲突都不会污染已确认状态
            scratch = copy.deepcopy(self.state)
            for event in events:
                domain.reduce(scratch, event)

            records: list[dict[str, Any]] = []
            for event in events:
                self._domain_seq += 1
                records.append({"kind": "domain", "seq": self._domain_seq, "event": event})
            if request_id:
                # 幂等索引与领域事件同提交点落盘
                records.append({"kind": "request", "request_id": request_id, "assay_no": assay_no})
            for payload in outbound or []:
                self._outbound_seq += 1
                records.append({"kind": "outbound", "seq": self._outbound_seq, "event": payload})

            self.journal.commit(records)

            # journal 落盘成功后才推进内存与投影（同事务视图）
            for event in events:
                domain.reduce(self.state, event)
            self.projections = domain.build_projections(self.state)
            if request_id:
                self.state["requests"][request_id] = {"化验编号": assay_no}

            if self._domain_seq % SNAPSHOT_EVERY == 0:
                self.save_snapshot()
            return True

    def execute(
        self,
        command: str,
        assay_no: str,
        data: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """执行一个命令。

        request_id 相同的重试直接回放首次结果（幂等）；
        与已确认状态冲突的命令抛 DomainConflict，由上层决定 409（不会覆盖结论）。
        """
        data = data or {}
        with self._lock:
            if request_id and request_id in self.state["requests"]:
                seen = self.state["requests"][request_id]
                return {"replayed": True, **self.get_order(seen["化验编号"])}

            try:
                events = domain.command_events(self.state, command, assay_no, data, request_id or "")
                # 校验发生在翻译阶段（如编号不存在）；状态冲突由试算阶段兜底
                outbound: list[dict[str, Any]] = []
                if events and events[0]["type"] == domain.EV_CONFIRMED:
                    # 出站事件在同一提交里落盘，确认写台账/投影与对外通知不会各做一半
                    outbound.append(self._confirmation_outbound_preview(events[0]))

                final_no = str(events[0]["data"].get("化验编号") or assay_no)
                self._commit_events(
                    events,
                    outbound=outbound,
                    request_id=request_id,
                    assay_no=final_no,
                )
            except domain.DomainConflict:
                # 未成功的请求不落 request 索引；重试仍拿到同样的冲突，不会覆盖已确认结果
                raise

            # 提交后立即尝试投递（outbox 去重保证幂等）
            self._dispatch_pending()
            return {"replayed": False, **self.get_order(final_no)}

    def _confirmation_outbound_preview(self, event: dict[str, Any]) -> dict[str, Any]:
        """构造出站事件。确认提交后 current_version 即被确认版本。"""
        assay_no = str(event["data"]["化验编号"])
        order = self.state["orders"][assay_no]
        ver_no = order["current_version"]
        return {
            "event_type": "assay.confirmed.v1",
            "event_id": f"assay-confirmed:{assay_no}:v{ver_no}",
            "at": event.get("at") or domain.now_iso(),
            "payload": {
                "化验编号": assay_no,
                "样品编号": order.get("样品编号"),
                "确认版本": ver_no,
            },
        }

    def migrate_legacy(self, legacy_rows: list[dict[str, Any]]) -> int:
        """存量缺版本号记录回填为 v1；只处理 journal 中尚不存在的化验编号。"""
        with self._lock:
            existing = set(self.state["orders"])
            fresh = [row for row in legacy_rows if str(row.get("化验编号") or "").strip() not in existing]
            if not fresh:
                return 0
            events = domain.migration_events(fresh)
            self._commit_events(events)
            self.save_snapshot()
            return len(fresh)

    def save_snapshot(self) -> None:
        with self._lock:
            self.snapshot.save({
                "domain_seq": self._domain_seq,
                "state": self.state,
            })

    # ---- 读模型 ---------------------------------------------------------------
    def get_order(self, assay_no: str) -> dict[str, Any]:
        with self._lock:
            order = self.state["orders"].get(assay_no)
            if order is None:
                raise domain.DomainConflict(f"化验编号 {assay_no} 不存在或已归档")
            return domain.order_detail(order)

    def ledger(
        self,
        *,
        keyword: str | None = None,
        sample_no: str | None = None,
        status: str | None = None,
        confirmed: bool | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = list(self.projections["ledger"])
        if keyword:
            rows = [r for r in rows if keyword in str(r.get("化验编号", ""))]
        if sample_no:
            rows = [r for r in rows if sample_no in str(r.get("样品编号", ""))]
        if status:
            want = domain.normalize_status(status)
            rows = [r for r in rows if domain.normalize_status(r.get("结果状态")) == want]
        if confirmed is not None:
            rows = [r for r in rows if bool(r.get("是否已确认")) == confirmed]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def traceability(self, *, sample_no: str | None = None, keyword: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self.projections["traceability"])
        if sample_no:
            rows = [r for r in rows if sample_no in str(r.get("样品编号", ""))]
        if keyword:
            rows = [r for r in rows if keyword in str(r.get("样品编号", "")) or keyword in str(r.get("化验编号", ""))]
        return rows

    def report_panel(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.projections["report_panel"])

    # ---- outbox 投递 ----------------------------------------------------------
    def set_dispatcher(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """注入出站适配器（写外部消息总线等）；成功返回即视为投递完成。"""
        self._dispatcher = fn

    def pending_outbound(self) -> list[dict[str, Any]]:
        """journal 中已提交、但出站通道还没有 event_id 的事件。"""
        delivered = self.sink.delivered_ids()
        return [
            rec["event"]
            for rec in self.journal.read_records()
            if rec.get("kind") == "outbound" and str(rec["event"].get("event_id")) not in delivered
        ]

    def _dispatch_pending(self) -> dict[str, int]:
        delivered = skipped = 0
        for event in self.pending_outbound():
            try:
                if self._dispatcher is not None:
                    self._dispatcher(event)
                # 适配器成功后追加到出站通道；event_id 去重，重放绝不重复
                if self.sink.deliver(event):
                    delivered += 1
                else:
                    skipped += 1
            except Exception:
                # 投递失败不能影响主事务；下次扫描会重放（event_id 幂等）
                continue
        return {"delivered": delivered, "skipped": skipped}

    def replay_outbound(self) -> dict[str, int]:
        """连接断开复位后显式触发：扫描未确认投递的出站事件并重放。"""
        with self._lock:
            return self._dispatch_pending()
