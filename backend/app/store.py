"""内存数据仓库 + 事件溯源管线。

写入路径统一走 ``Store.transaction()``：

1. 业务服务在事务内校验并暂存领域事件（乐观锁/幂等检查也在这一步）；
2. 提交时先把事件以 append-only 方式落盘到 ``data/events.jsonl``（fsync），
   再把事件分发给各投影器（化验台账、样品追溯清单、报告数据面板、出站箱）；
3. 任一步失败都会用提交前快照整体回滚内存状态；
4. 进程重启（连接断开复位）时先执行存量迁移，再按日志顺序幂等重放事件，
   最后补发出站箱中未投递的事件。

同一 ``request_id`` 的重复请求会直接返回首次结果，不会再次落库，
从而保证断连重发/重放的幂等性。
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from app.seed import SEED_ROWS

DATA_DIR = Path(os.environ.get("APP_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
JOURNAL_FILE = DATA_DIR / "events.jsonl"

# 迁移/启动引导阶段使用的表名：投影与出站箱。
ASSAY_CURRENT_TABLE = "assay_current"        # 化验台账当前投影：每个化验编号一行
SAMPLE_TRACE_TABLE = "sample_trace"          # 样品追溯清单投影
REPORT_PANEL_TABLE = "report_panel_assay"    # 报告数据面板投影（化验结论）
OUTBOX_TABLE = "outbox"                      # 出站事件事务箱
OUTBOUND_LOG_TABLE = "assay_outbound"        # 已投递出站事件留痕（幂等去重）

PROJECTION_TABLES = [
    ASSAY_CURRENT_TABLE,
    SAMPLE_TRACE_TABLE,
    REPORT_PANEL_TABLE,
    OUTBOX_TABLE,
    OUTBOUND_LOG_TABLE,
]


class ConflictError(Exception):
    """并发冲突：化验编号已被其他确认请求推进，本次请求不得覆盖已确认结论。"""


CONFLICT_HTTP_STATUS = 409


class Transaction:
    """一次原子写入：暂存事件，提交时落盘并投影。"""

    def __init__(self, store: "Store") -> None:
        self._store = store
        self._events: list[dict[str, Any]] = []
        # 投影完成后再取结果，避免把投影前的旧快照返回给调用方。
        self.result_builder: Callable[[], Any] | None = None
        self.result: dict[str, Any] | None = None

    def emit(self, event_type: str, aggregate: str, payload: dict[str, Any], *, request_id: str) -> dict[str, Any]:
        event = {
            "id": f"{event_type}:{uuid.uuid4().hex}",
            "type": event_type,
            "aggregate": aggregate,
            "request_id": request_id,
            "ts": time.time(),
            "payload": payload,
        }
        self._events.append(event)
        return event

    def _commit(self) -> None:
        store = self._store
        if not self._events:
            return
        # 1) 事件先持久化（append-only + fsync），失败则整体回滚。
        store._append_journal(self._events)
        # 2) 事件在同一把锁内顺序投影，保证台账/追溯/面板/出站箱一致。
        for event in self._events:
            store._dispatch_projectors(event)
        # 3) 投影收敛后再生成返回结果（与页面投影口径完全一致）。
        if self.result_builder is not None:
            self.result = self.result_builder()
        # 4) 业务提交完成后再投递出站箱；投递失败不回滚业务，留待重启补发。
        for event in self._events:
            store._deliver_outbox_event(event)


class Store:
    def __init__(self, *, bootstrap: bool = True) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        for name in PROJECTION_TABLES:
            self._tables.setdefault(name, [])
        self._lock = threading.RLock()
        self._projectors: dict[str, list[Callable[[dict[str, Any]], None]]] = {}
        self._outbound_subscribers: list[Callable[[dict[str, Any]], None]] = []
        # request_id -> 首次请求结果（幂等缓存，重放时也会重建）
        self._request_results: dict[str, dict[str, Any]] = {}
        if bootstrap:
            self.bootstrap()

    # ---------- 基础读取 ----------

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in PROJECTION_TABLES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            if name == "assay":
                # 历史版本只占台账行，不计入业务条目数，按化验编号去重统计。
                heads = self.rows(ASSAY_CURRENT_TABLE)
                modules.append({
                    "name": name,
                    "created": len(heads),
                    "pending": sum(1 for row in heads if row.get("pending")),
                    "abnormal": sum(1 for row in heads if row.get("abnormal")),
                })
                continue
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ---------- 投影器 / 出站订阅注册 ----------

    def register_projector(self, event_type: str, projector: Callable[[dict[str, Any]], None]) -> None:
        self._projectors.setdefault(event_type, []).append(projector)

    def register_outbound_subscriber(self, subscriber: Callable[[dict[str, Any]], None]) -> None:
        self._outbound_subscribers.append(subscriber)

    def _dispatch_projectors(self, event: dict[str, Any]) -> None:
        for projector in self._projectors.get(event["type"], []):
            projector(event)

    # ---------- 事务与幂等 ----------

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        self._lock.acquire()
        snapshot = copy.deepcopy(self._tables)
        tx = Transaction(self)
        try:
            yield tx
            tx._commit()
        except BaseException:
            # 投影或落盘失败：内存状态整体回滚；已落盘事件会在重启重放时收敛。
            self._tables = snapshot
            raise
        finally:
            self._lock.release()

    def cached_result(self, request_id: str | None) -> dict[str, Any] | None:
        if not request_id:
            return None
        return self._request_results.get(request_id)

    def remember_result(self, request_id: str, result: dict[str, Any]) -> None:
        self._request_results[request_id] = result

    # ---------- 事件日志持久化与重放 ----------

    def _append_journal(self, events: list[dict[str, Any]]) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with JOURNAL_FILE.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _read_journal(self) -> list[dict[str, Any]]:
        if not JOURNAL_FILE.exists():
            return []
        events: list[dict[str, Any]] = []
        with JOURNAL_FILE.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    # 半行写入（落盘瞬间断电）直接跳过，不阻断重放。
                    continue
        return events

    def bootstrap(self) -> None:
        """启动引导：注册投影 → 存量迁移 → 事件幂等重放 → 补发挂起的出站事件。"""
        with self._lock:
            # 延迟导入，避免 store ↔ 领域事件模块的循环依赖。
            from app.events.assay import backfill_assay_versions, register_projectors
            from app.events.outbound import register_outbound

            register_projectors(self)
            register_outbound(self)
            backfill_assay_versions(self)
            for event in self._read_journal():
                if event.get("request_id") and event["request_id"] not in self._request_results:
                    self._request_results[event["request_id"]] = {"replayed": True, "event_id": event["id"]}
                # 重放只投影，不再重复写日志；投影全部按自然键幂等 upsert。
                self._dispatch_projectors(event)
            self.deliver_pending_outbox()

    # ---------- 出站箱（事务发件 + 断点补发） ----------

    def upsert_outbox(self, event: dict[str, Any]) -> None:
        outbox = self.rows(OUTBOX_TABLE)
        for row in outbox:
            if row.get("event_id") == event["id"]:
                return
        outbox.append({
            "event_id": event["id"],
            "event_type": event["type"],
            "aggregate": event["aggregate"],
            "request_id": event["request_id"],
            "payload": event["payload"],
            "delivered": False,
            "created_at": event["ts"],
        })

    def _deliver_outbox_event(self, event: dict[str, Any]) -> None:
        row = next(
            (r for r in self.rows(OUTBOX_TABLE)
             if r.get("event_id") == event["id"] and not r.get("delivered")),
            None,
        )
        if row is None:
            return
        failed = False
        for subscriber in self._outbound_subscribers:
            try:
                subscriber(event)
            except Exception:
                # 任一订阅方（外部系统）断连都保持未投递，重启/复位时幂等补发。
                failed = True
        if not failed:
            row["delivered"] = True

    def deliver_pending_outbox(self) -> None:
        for row in self.rows(OUTBOX_TABLE):
            if row.get("delivered"):
                continue
            event = {
                "id": row["event_id"],
                "type": row["event_type"],
                "aggregate": row["aggregate"],
                "request_id": row["request_id"],
                "ts": row["created_at"],
                "payload": row["payload"],
            }
            failed = False
            for subscriber in self._outbound_subscribers:
                try:
                    subscriber(event)
                except Exception:
                    failed = True
            if not failed:
                row["delivered"] = True


store = Store()
