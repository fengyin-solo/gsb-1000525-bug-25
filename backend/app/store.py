"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    @staticmethod
    def _assay_overview() -> dict[str, object]:
        # 延迟导入：assay_kernel 装配时会反向引用内存仓库做存量迁移
        from app import assay_kernel

        ledger, total = assay_kernel.kernel.ledger(page=1, size=10000)
        panel = assay_kernel.kernel.report_panel()
        pending = int(panel["待确认结果"]) + int(panel["退回待复检"])
        return {
            "name": "assay",
            "created": total,
            "pending": pending,
            "abnormal": int(panel["退回待复检"]),
        }

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            if name == "assay":
                # 化验模块已事件化：概览与台账/投影取同一口径，避免刷新后数字对不上
                stats = self._assay_overview()
                modules.append(stats)
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


store = Store()
