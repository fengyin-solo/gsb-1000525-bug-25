"""化验内核装配：单例、存量迁移与出站事件 relay。

- 启动时把内存种子里缺版本号的存量记录迁移回填为 v1（只迁一次）；
- 出站确认事件随领域事务落 journal，relay 再投递到出站通道 dispatch.log.jsonl，
  通道按 event_id 去重，连接断开/进程复位后扫描重放，不会重复通知。
"""
from __future__ import annotations

import threading
from typing import Any

from app.config import settings
from app.kernel import AssayKernel
from app.seed import SEED_ROWS  # noqa: F401  # 保留依赖顺序：种子先于迁移装配
from app.store import store

kernel = AssayKernel(settings.data_dir)


def _adapter(event: dict[str, Any]) -> None:
    """出站适配器占位：真实系统里这里写消息总线；当前由内核追加到本地通道日志。"""
    return None


kernel.set_dispatcher(_adapter)


def bootstrap_legacy() -> int:
    """把内存仓库里的存量化验行（无版本号）迁移到事件内核，只迁一次。"""
    return kernel.migrate_legacy(store.rows("assay"))


migrated = bootstrap_legacy()

# 后台 relay：每 2 秒扫描未投递出站事件，断连恢复后幂等重放
_relay_stop = threading.Event()


def _relay_loop() -> None:
    while not _relay_stop.wait(2.0):
        try:
            kernel.replay_outbound()
        except Exception:
            pass


def start_relay() -> None:
    if getattr(start_relay, "_started", False):
        return
    start_relay._started = True  # type: ignore[attr-defined]
    thread = threading.Thread(target=_relay_loop, name="assay-outbox-relay", daemon=True)
    thread.start()


start_relay()
