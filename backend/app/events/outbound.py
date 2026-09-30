"""出站事件订阅器：把事务箱中投递的确认事件留痕。

真实部署里这里会调用 LIMS / 出站闸门等外部系统；当前以落内存表代替，
重点保证：随事务提交后投递、断开复位后自动补发、同一事件重复投递幂等。
"""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from app.store import Store


def record_outbound(store: "Store", event: dict[str, Any]) -> None:
    log = store.rows("assay_outbound")
    for row in log:
        if row.get("event_id") == event["id"]:
            # 同一事件已投递过：幂等跳过，不产生重复通知。
            return
    log.append({
        "event_id": event["id"],
        "event_type": event["type"],
        "aggregate": event["aggregate"],
        "request_id": event["request_id"],
        "delivered_at": event["ts"],
        "payload": event["payload"],
    })


def register_outbound(store: "Store") -> None:
    store.register_outbound_subscriber(lambda event: record_outbound(store, event))
