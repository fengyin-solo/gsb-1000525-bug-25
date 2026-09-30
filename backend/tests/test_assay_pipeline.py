"""化验持久化与投影一致性回归测试。

覆盖：
- 存量缺版本号记录迁移回填，且迁移本身幂等；
- 同一化验编号以最近确认复检为准，历史版本保留，未确认复检不进投影；
- 确认同时回写台账、样品追溯、报告面板（事务一致）；
- 幂等重放（request_id）、并发确认只成功一个、失败请求不覆盖结论；
- 进程复位后 journal/snapshot 恢复，出站事件断连后按 event_id 幂等重放。

直接运行：python3 -m tests.test_assay_pipeline
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.domain import assay as domain  # noqa: E402
from app.kernel import AssayKernel  # noqa: E402


def seed_rows() -> list[dict]:
    return [
        {"id": 1, "status": "待录入", "化验编号": "AS-1", "样品编号": "S-1", "元素名称": "Au",
         "化验值": "老值", "单位": "g/t", "化验日期": "2026-09-01"},
        {"id": 2, "status": "已录入", "化验编号": "AS-2", "样品编号": "S-2", "元素名称": "Cu",
         "化验值": "0.5", "单位": "%", "化验日期": "2026-09-02"},
        {"id": 3, "status": "已审核", "化验编号": "AS-3", "样品编号": "S-3", "元素名称": "Ag",
         "化验值": "3.0", "单位": "g/t", "化验日期": "2026-09-03"},
    ]


class AssayPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="assay-test-")
        self.kernel = AssayKernel(self.tmp)
        self.kernel.set_dispatcher(lambda event: None)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 迁移 ----------------------------------------------------------------
    def test_legacy_rows_backfilled_with_v1_and_idempotent(self) -> None:
        migrated = self.kernel.migrate_legacy(seed_rows())
        self.assertEqual(migrated, 3)
        detail = self.kernel.get_order("AS-3")
        self.assertEqual(detail["确认版本"], 1)
        self.assertEqual(detail["历史版本"][0]["source"], "迁移")
        self.assertEqual(detail["化验值"], "3.0")
        # 再迁一次：journal 已存在的编号不重复
        self.assertEqual(self.kernel.migrate_legacy(seed_rows()), 0)

    # ---- 投影一致 -------------------------------------------------------------
    def test_confirmation_writes_three_projections_together(self) -> None:
        self.kernel.execute("create", "AS-9", {"化验编号": "AS-9", "样品编号": "S-9", "元素名称": "Au"})
        self.kernel.execute("录入结果", "AS-9", {"化验值": "1.0", "单位": "g/t"})
        # 确认前：追溯/面板没有它
        self.assertEqual(self.kernel.traceability(sample_no="S-9"), [])
        self.assertEqual(self.kernel.report_panel()["已确认结论"], 0)

        self.kernel.execute("确认结论", "AS-9", {"确认人": "甲"}, request_id="req-1")
        ledger = [r for r in self.kernel.ledger(page=1, size=100)[0] if r["化验编号"] == "AS-9"][0]
        trace = self.kernel.traceability(sample_no="S-9")
        panel = self.kernel.report_panel()
        self.assertTrue(ledger["是否已确认"])
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["化验值"], "1.0")
        self.assertIn("AS-9", [r["化验编号"] for r in panel["items"]])
        self.assertEqual(panel["已确认结论"], 1)

    def test_latest_confirmed_recheck_wins_history_preserved(self) -> None:
        self.kernel.migrate_legacy(seed_rows())
        # AS-3 已确认 v1，提交复检 v2 但不确认：投影必须仍是 v1
        self.kernel.execute("提交复检", "AS-3", {"化验值": "9.9"})
        self.assertEqual(self.kernel.get_order("AS-3")["化验值"], "3.0")
        self.assertEqual(
            [t for t in self.kernel.traceability(keyword="AS-3")][0]["化验值"], "3.0")
        ledger_row = [r for r in self.kernel.ledger(page=1, size=100)[0] if r["化验编号"] == "AS-3"][0]
        self.assertEqual(ledger_row["化验值"], "3.0")
        self.assertEqual(ledger_row["结果状态"], "已录入")  # 在途复检

        # 确认 v2 后：三处全部切到新值，历史仍可读到 v1
        self.kernel.execute("确认结论", "AS-3", {})
        detail = self.kernel.get_order("AS-3")
        self.assertEqual(detail["化验值"], "9.9")
        self.assertEqual(detail["确认版本"], 2)
        self.assertEqual([v["version"] for v in detail["历史版本"]], [1, 2])
        self.assertEqual([v["化验值"] for v in detail["历史版本"]], ["3.0", "9.9"])
        trace = [t for t in self.kernel.traceability(keyword="AS-3")]
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["化验值"], "9.9")
        self.assertEqual(trace[0]["确认版本"], 2)

    def test_no_duplicate_rows_in_ledger_or_trace(self) -> None:
        self.kernel.migrate_legacy(seed_rows())
        for value in ("1.1", "1.2", "1.3"):
            self.kernel.execute("提交复检", "AS-3", {"化验值": value})
            self.kernel.execute("确认结论", "AS-3", {})
        ledger = self.kernel.ledger(page=1, size=100)[0]
        self.assertEqual(len([r for r in ledger if r["化验编号"] == "AS-3"]), 1)
        self.assertEqual(len([t for t in self.kernel.traceability(keyword="AS-3")]), 1)
        self.assertEqual(self.kernel.get_order("AS-3")["确认版本"], 4)

    # ---- 并发与幂等 -----------------------------------------------------------
    def test_concurrent_confirmation_only_one_succeeds(self) -> None:
        self.kernel.migrate_legacy(seed_rows())  # AS-2 已录入
        outcomes: list[str] = []

        def confirm(rid: str) -> None:
            try:
                self.kernel.execute("确认结论", "AS-2", {}, request_id=rid)
                outcomes.append("ok")
            except domain.DomainConflict:
                outcomes.append("conflict")

        threads = [threading.Thread(target=confirm, args=(f"c{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(outcomes).count("ok"), 1)
        self.assertEqual(sorted(outcomes).count("conflict"), 7)
        # 成功确认的值仍在，未被失败者覆盖
        self.assertEqual(self.kernel.get_order("AS-2")["确认版本"], 1)

    def test_failed_request_retry_still_conflicts_and_never_overwrites(self) -> None:
        self.kernel.migrate_legacy(seed_rows())
        self.kernel.execute("确认结论", "AS-2", {}, request_id="winner")
        with self.assertRaises(domain.DomainConflict):
            self.kernel.execute("确认结论", "AS-2", {}, request_id="loser")
        with self.assertRaises(domain.DomainConflict):
            self.kernel.execute("确认结论", "AS-2", {}, request_id="loser")  # 重试
        self.assertEqual(self.kernel.get_order("AS-2")["确认版本"], 1)

    def test_idempotent_replay_returns_first_result(self) -> None:
        self.kernel.migrate_legacy(seed_rows())
        first = self.kernel.execute("确认结论", "AS-2", {"确认人": "甲"}, request_id="dup")
        second = self.kernel.execute("确认结论", "AS-2", {"确认人": "乙（重试）"}, request_id="dup")
        self.assertTrue(second["replayed"])
        self.assertEqual(second["确认版本"], first["确认版本"])

    def test_confirmed_result_cannot_be_returned(self) -> None:
        self.kernel.migrate_legacy(seed_rows())
        with self.assertRaises(domain.DomainConflict):
            self.kernel.execute("退回修改", "AS-3", {})

    # ---- 持久化与出站 ---------------------------------------------------------
    def test_restart_recovers_state_and_idempotency_index(self) -> None:
        self.kernel.migrate_legacy(seed_rows())
        self.kernel.execute("确认结论", "AS-2", {}, request_id="keep-1")
        self.kernel.execute("提交复检", "AS-2", {"化验值": "7.7"}, request_id="keep-2")
        self.kernel.execute("确认结论", "AS-2", {}, request_id="keep-3")

        revived = AssayKernel(self.tmp)
        detail = revived.get_order("AS-2")
        self.assertEqual(detail["化验值"], "7.7")
        self.assertEqual(detail["确认版本"], 2)
        self.assertEqual(len(detail["历史版本"]), 2)
        # 存量迁移不会重复
        self.assertEqual(revived.migrate_legacy(seed_rows()), 0)
        # 幂等索引随 journal 恢复
        self.assertTrue(revived.execute("确认结论", "AS-2", {}, request_id="keep-1")["replayed"])

    def test_outbound_event_replay_idempotent_after_sink_reset(self) -> None:
        self.kernel.migrate_legacy(seed_rows())
        self.kernel.execute("确认结论", "AS-2", {}, request_id="o1")
        self.kernel.execute("提交复检", "AS-2", {"化验值": "8.8"})
        self.kernel.execute("确认结论", "AS-2", {}, request_id="o2")
        # 提交时已即时投递；显式重放不重复追加
        self.assertEqual(self.kernel.pending_outbound(), [])
        self.assertEqual(self.kernel.replay_outbound()["delivered"], 0)
        # 模拟连接/通道复位：删除出站通道，journal 扫描后应原样补齐且不重复
        os.remove(self.kernel.sink.path)
        self.assertEqual(len(self.kernel.pending_outbound()), 2)
        self.assertEqual(self.kernel.replay_outbound()["delivered"], 2)
        ids = list(self.kernel.sink.read_events())
        self.assertEqual(len(ids), 2)
        self.assertEqual(len({e["event_id"] for e in ids}), 2)

    def test_snapshot_corruption_falls_back_to_journal(self) -> None:
        self.kernel.migrate_legacy(seed_rows())
        self.kernel.execute("确认结论", "AS-2", {})
        self.kernel.save_snapshot()
        with open(Path(self.tmp) / "snapshot.json", "w", encoding="utf-8") as fh:
            fh.write("{not json")
        revived = AssayKernel(self.tmp)
        self.assertEqual(revived.get_order("AS-2")["确认版本"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
