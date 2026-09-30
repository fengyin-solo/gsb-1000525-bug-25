"""持久化基础设施：追加式事件日志、原子快照与出站事件日志。

设计目标：
- 所有领域变更先顺序追加到 journal（一个文件 = 提交点，append 由锁串行化）；
- 快照只是缓存，损坏或丢失都能靠 journal 完整重放；
- 出站事件与领域事件在同一次追加里落盘（事务一致），relay 再幂等投递；
- 全部只依赖标准库，保证克隆即可运行。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Iterator


def atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件 + os.replace，保证读到的永远是完整文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def atomic_append_text(path: Path, text: str) -> None:
    """串行化的追加写：调用方需要持锁，这里保证一次 write 整体落盘。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


class Journal:
    """JSONL 追加日志。

    每条记录形如 {"kind": "domain"|"outbound", "seq": int, ...}。
    domain 记录可驱动状态重放；outbound 记录是与领域变更同事务落盘的出站事件。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def read_records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    # 崩溃残留的半行直接跳过：它从未完成提交
                    continue
        return records

    def commit(self, entries: list[dict[str, Any]]) -> None:
        """把一批记录作为一个提交点追加；锁与单次 write 保证原子可见性。"""
        if not entries:
            return
        payload = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries)
        with self._lock:
            atomic_append_text(self.path, payload)

    def last_seq(self) -> int:
        seqs = [int(r.get("seq", 0)) for r in self.read_records()]
        return max(seqs, default=0)


class Snapshot:
    """原子写入的 JSON 快照，仅作重放加速。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None

    def save(self, data: dict[str, Any]) -> None:
        atomic_write_text(self.path, json.dumps(data, ensure_ascii=False))


class OutboxLog:
    """出站事件落地日志。

    relay 把已提交的出站事件投递到这里；按 event_id 去重，
    连接断开/进程复位后重放不会产生重复消息。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def delivered_ids(self) -> set[str]:
        ids: set[str] = set()
        if not self.path.exists():
            return ids
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ids.add(str(json.loads(line)["event_id"]))
                except (json.JSONDecodeError, KeyError):
                    continue
        return ids

    def read_events(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def deliver(self, event: dict[str, Any]) -> bool:
        """幂等投递：已存在的 event_id 直接跳过，返回 False。

        检查与追加在同一把锁内完成，relay 线程与请求线程并发重放也不会重复写。
        """
        event_id = str(event["event_id"])
        with self._lock:
            if event_id in self.delivered_ids():
                return False
            atomic_append_text(self.path, json.dumps(event, ensure_ascii=False) + "\n")
            return True
