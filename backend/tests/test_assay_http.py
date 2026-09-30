"""化验 HTTP 链路集成测试（隔离数据目录，作为独立进程运行）。

运行：ASSAY_DATA_DIR=$(mktemp -d) python3 -m tests.test_assay_http
"""
from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("ASSAY_DATA_DIR", tempfile.mkdtemp(prefix="assay-http-"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


class AssayHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_01_migrated_ledger_and_detail(self) -> None:
        resp = self.client.get("/api/assay?size=100")
        self.assertEqual(resp.status_code, 200)
        assa3 = [r for r in resp.json()["items"] if r["化验编号"] == "ASSA-0003"][0]
        self.assertEqual(assa3["确认版本"], 1)
        self.assertIsNotNone(assa3["确认时间"])

        detail = self.client.get("/api/assay/ASSA-0003").json()
        self.assertEqual(len(detail["历史版本"]), 1)
        self.assertEqual(self.client.get("/api/assay/MISSING").status_code, 404)
        # /report_panel、/trace、/export 不能被 /{entry_key} 吞掉
        self.assertEqual(self.client.get("/api/assay/report_panel").status_code, 200)
        self.assertEqual(self.client.get("/api/assay/trace").status_code, 200)
        self.assertEqual(self.client.get("/api/assay/export").status_code, 200)

    def test_02_lifecycle_and_conflict(self) -> None:
        c = self.client
        r = c.post("/api/assay/ASSA-0001/actions",
                   json={"values": {"action": "录入结果", "化验值": "1.25", "单位": "g/t"}})
        self.assertEqual(r.status_code, 200)
        r = c.post("/api/assay/ASSA-0001/actions",
                   json={"values": {"action": "确认结论", "request_id": "h-1"}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["entry"]["确认版本"], 1)

        # 幂等回放
        r = c.post("/api/assay/ASSA-0001/actions",
                   json={"values": {"action": "确认结论", "request_id": "h-1"}})
        self.assertIn("幂等", r.json()["message"])

        # 复检未确认：投影仍是旧值
        r = c.post("/api/assay/ASSA-0001/actions",
                   json={"values": {"action": "提交复检", "化验值": "2.50"}})
        self.assertEqual(r.status_code, 200)
        row = [x for x in c.get("/api/assay?keyword=ASSA-0001").json()["items"]
               if x["化验编号"] == "ASSA-0001"][0]
        self.assertEqual(row["化验值"], "1.25")

        # 并发确认：恰好一个 200，其余 409，且结论不被覆盖
        codes: list[int] = []

        def hit(rid: str) -> None:
            rr = c.post("/api/assay/ASSA-0001/actions",
                        json={"values": {"action": "确认结论", "request_id": rid}})
            codes.append(rr.status_code)

        import threading
        threads = [threading.Thread(target=hit, args=(f"race-{i}",)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(codes).count(200), 1)
        self.assertEqual(sorted(codes).count(409), 5)

        # 三个投影同值
        final = c.get("/api/assay/ASSA-0001").json()
        self.assertEqual(final["化验值"], "2.50")
        self.assertEqual(final["确认版本"], 2)
        trace_a = c.get("/api/assay/trace?keyword=ASSA-0001").json()["items"]
        trace_b = c.get("/api/sample_registry/traceability?keyword=ASSA-0001").json()["items"]
        self.assertEqual(trace_a, trace_b)
        self.assertEqual(len(trace_a), 1)
        self.assertEqual(trace_a[0]["化验值"], "2.50")
        panel = c.get("/api/assay/report_panel").json()
        self.assertIn("ASSA-0001", [i["化验编号"] for i in panel["items"]])

        # 出站重放端点可用且幂等
        first = c.post("/api/assay/outbound/replay").json()
        second = c.post("/api/assay/outbound/replay").json()
        self.assertEqual(second["delivered"], 0)
        self.assertIn("delivered", first)

    def test_03_create_validation_and_dup(self) -> None:
        c = self.client
        missing = c.post("/api/assay", json={"values": {"化验编号": "X-1"}})
        self.assertFalse(missing.json()["ok"])
        ok = c.post("/api/assay", json={"values": {"化验编号": "NEW-1", "样品编号": "NS-1", "元素名称": "Au"}})
        self.assertTrue(ok.json()["ok"])
        dup = c.post("/api/assay", json={"values": {"化验编号": "NEW-1", "样品编号": "NS-1", "元素名称": "Au"}})
        # 重复创建经 action 路径才会 409；create 走统一 ok=False 文案
        self.assertFalse(dup.json()["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
