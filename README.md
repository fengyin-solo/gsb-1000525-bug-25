# 地质勘探数据管理平台

面向地质勘探的钻孔编录、岩心取样、物探数据、化探分析、测绘资料与储量估算的综合数据管理后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 钻孔编录 | `borehole` | 钻孔 | 钻孔编号、勘探区、孔口坐标 |
| 岩心管理 | `core` | 岩心样本 | 岩心编号、所属钻孔、取样深度起 |
| 地层划分 | `stratigraphy` | 地层单元 | 单元编号、钻孔编号、地层名称 |
| 地球物理 | `geophysics` | 物探测线 | 测线编号、勘探区、物探方法 |
| 化探分析 | `geochem` | 化探样品 | 样品编号、样品类型、采样点位 |
| 化验数据 | `assay` | 化验结果 | 化验编号、样品编号、元素名称 |
| 地质填图 | `mapping` | 填图单元 | 图幅编号、图幅名称、比例尺 |
| 测绘控制 | `survey_point` | 控制点 | 点号、点类型、坐标X |
| 钻探日志 | `drilling_log` | 钻探记录 | 日志编号、钻孔编号、钻进深度 |
| 储量估算 | `reserve` | 矿体块段 | 块段编号、矿体名称、面积 |
| 样品登记 | `sample_registry` | 送检样品 | 送检编号、样品名称、采样位置 |
| 勘探设备 | `equipment` | 勘探仪器 | 仪器编号、仪器名称、型号规格 |
| 水文地质 | `hydro` | 水文观测点 | 观测编号、观测类型、所在钻孔 |
| 剖面编录 | `section` | 实测剖面 | 剖面编号、剖面名称、剖面长度 |
| 地质报告 | `geological_report` | 勘探报告 | 报告编号、勘探区、报告类型 |
| 遥感解译 | `remote` | 遥感数据 | 数据编号、数据源、分辨率 |
| 矿产评价 | `mineral` | 矿化线索 | 线索编号、勘探区、矿种 |
| 环境地质 | `environmental` | 环境调查点 | 调查编号、调查区域、灾害类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 化验数据：版本化持久化与投影

化验模块（`assay`）不是就地改内存，而是事件溯源，相关文件：

- `app/persistence.py`：追加式事件日志（`journal.jsonl`）、原子快照（`snapshot.json`）、
  出站通道日志（`dispatch.log.jsonl`），全部标准库实现，文件在 `backend/data/`
  （可用环境变量 `ASSAY_DATA_DIR` 覆盖，已在 `.gitignore` 忽略）。
- `app/domain/assay.py`：纯函数 reducer、命令校验、三个读模型投影与存量迁移。
- `app/kernel.py`：事件提交（事务边界）、journal/snapshot 恢复、outbox 幂等投递。
- `app/assay_kernel.py`：单例装配，启动时把缺版本号的存量记录回填为 v1（只迁一次）。

口径：

- 同一**化验编号**是聚合根，每次复检生成新版本；**最近确认复检**为准，
  历史结果按原版本保留（`GET /api/assay/{化验编号}` 的 `历史版本`）。
- 未确认复检不会进入任何投影，因此不会覆盖或重复显示。
- 「确认结论」在一个事务内回写三处：化验台账、样品追溯清单
  （`GET /api/sample_registry/traceability` 与 `GET /api/assay/trace` 同源）、
  报告数据面板（`GET /api/assay/report_panel`），并追加一条出站确认事件。
- 动作请求可带 `request_id`（或 `Idempotency-Key` 头）：重连重放返回首次结论；
  并发确认只有一个成功，冲突返回 409，未成功请求不会覆盖已确认结果。
- 出站事件由后台 relay 每 2 秒扫描投递，也可 `POST /api/assay/outbound/replay`
  手动重放；按 `event_id` 去重，进程/连接复位后不重复通知。

后端回归测试：`cd backend && python3 -m unittest discover -s tests`。
