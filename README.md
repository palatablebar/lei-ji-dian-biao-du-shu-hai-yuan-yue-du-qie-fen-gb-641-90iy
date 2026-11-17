# meterback · 累计电表读数还原与月度切分

抄表班的读数还原工具：把累计示数还原成区间用量，按日权重切到自然月，出月度电费，并对四账；
光伏户的购电、上网两道累计读数分别计价后轧成月度净款。

- 使用者：供电所抄表班（示例 8 只单向表 + 2 只光伏双向表，覆盖换表、满量翻转、估抄、单位混写、缺向抄表）
- 输入：`sample_data/` 里的读数 CSV（可叠加补抄包）+ 表计契约 `meters.csv` + 电价 `prices.csv`
- 输出：区间用量、月度分摊、月度账单、双向净结算与发布版本、隔离区证据、运行清单

## 功能模块

| 模块 | 动作 | 入口 |
|---|---|---|
| M1 读数采信 | 编码回退、BOM、重复表头、列数、截断、空态三态 | `POST /api/readings/import`、`/readings` |
| M2 表计契约 | 容量、倍率、寄存器、单位、换表事件、电价、单行试解析 | `GET /api/contract`、`POST /api/contract/preview` |
| M3 质量隔离 | 负数、倒走、同键冲突、未知表、超满量、换表后抄表 | `GET /api/quarantine`、`GET /api/quarantine.csv` |
| M4 区间还原 | 满量翻转、换表两段、新表首读起算、估抄标记 | `POST /api/intervals/rebuild`、`/intervals` |
| M5 月度切分 | 日权重、最大余额、三分量闭合、账单尾差 | `POST /api/months/allocate`、`/months` |
| M6 批次与对账 | 全流程、批次历史、四账、失败重试 | `POST /api/runs`、`GET /api/reconcile`、`/`、`/batches` |
| M7 导出 | 月度 / 区间 / 隔离 / 读数证据 / 净结算 / 运行清单 | `GET /api/export/*`、`/export` |
| M8 双向净结算 | 购电/上网分向计价、缺向暂挂、补抄升版、两向台账 | `POST /api/settlement/publish`、`/settlement`、`/ledger` |

## 核心规则

1. 读数按 `(表号, 寄存器, UTC 时间)` 排序；区间用量 =（本次 − 上次）× 综合倍率。
2. 本次 < 上次时，只有「上次 ≥ 满量 × 0.9」才按满量翻转补容量，否则判倒走并隔离。
3. 换表把区间拆成两段：旧表到换表时点，新表首读从零起算。
4. 估抄行不作锚点、不参与单调判定，落在区间内时给区间打标记。
5. 区间用量按覆盖天数切到 `[月初, 下月初)`，余数用最大余额法分到 0.001 kWh。
6. 同月「总表 = 峰 + 平 + 谷」的残差并入最大分量；账单先合计再舍入，分项补差。
7. 双向表按方向各还原一段序列；购电量 × 购电价、上网量 × 上网价分别算出金额后轧差，绝不拿净电量套单一电价。
8. 月底只有一向先到：已到方向按本向电价计入净额，另一向 `暂挂`（不计入），月状态 `缺向暂结`；补抄到达后重算并发布新版本，旧版原样保留可复核。
9. 两向读数台账按业务键合并基础包与补抄包：同键同值只留一条、同键不同值全部留痕、输入行 = 采用 + 重复 + 冲突 + 隔离。

## 定义与已知不覆盖

字段、单位、时间、空值、去重键、尾差与缺向裁决规则见 `docs/DATA-DICTIONARY.md`；不覆盖的部分列在 `docs/DEPTH.md` 末节。

## 启动

```bash
docker compose up -d --build      # 宿主 18200 → 容器 8000
curl -sS http://localhost:18200/api/health
```

打开 <http://localhost:18200/>，选样例点「开始处理」。

## 演示步骤

单向全流程（原口径，四账仍全平）：

1. 总览页 → 样例 `readings_dirty.csv` → 开始处理。
2. 读数诊断页：60 行原始数据 → 45 有效 + 1 估抄 + 14 隔离。
3. 区间还原页：32 段，其中 1 段满量翻转（249.900 kWh）、换表拆两段（60 + 45）。
4. 月度分摊页：5 个月、16 张账单、合计 4499.900 kWh / 3261.96 元。
5. 批次对账页：四账全平；导出页下载文件。

光伏双向净结算：

1. 总览页选 `readings_pv.csv` → 开始处理。结算阶段显示「4 结清 / 1 缺向 / 暂挂 2 向」。
2. `/settlement`：2026-05 为「缺向暂结」——P001 缺上网、P002 缺购电，缺的这两向标 `暂挂`、不并入净额；净款 58.00 元。
3. `/ledger`：购电、上网两向原始读数都在；台账分区「输入 22 = 采用 22 + 重复 0 + 冲突 0 + 隔离 0」。
4. `/settlement` → 补抄包选 `readings_pv_supplement.csv` → 补抄并发布：2026-05 升为「已结清」、净款 100.00 元；版本列表出现 V01（已被替代）与 V02，V02 标出两向各为 `补抄 / 未变`，净款变化 +42.00 元。
5. 点 V01 链接：旧版仍可复核，含当时的暂挂行；V01 的 CSV 可单独下载。
6. `/api/export/manifest.json` 的 `settlement` 段带月度状态、净额、分区计数、补抄输入与两版差异。

## 页面

| 路径 | 内容 |
|---|---|
| `/` | 阶段卡、合计、四账、最近批次 |
| `/readings` | 读数行、隔离区、原因码分布 |
| `/intervals` | 区间明细、换表事件、标记 |
| `/months` | 按月分组的分摊、账单、双向净结算摘要 |
| `/settlement` | 月度净款、采用/暂挂方向、发布版本、版本复核与补抄变化 |
| `/ledger` | 购电/上网两向原始读数、合并的抄表包、冲突与隔离证据 |
| `/batches` | 运行清单、四账、行数、批次历史 |
| `/export` | 导出文件与清单字段 |

## 接口

`POST /api/runs`、`POST /api/readings/import`、`GET /api/readings/{batch_id}`、`GET /api/contract`、`POST /api/contract/preview`、`GET /api/quarantine`、`GET /api/quarantine.csv`、`GET /api/reasons`、`POST /api/intervals/rebuild`、`POST /api/months/allocate`、`GET /api/batches`、`GET /api/reconcile`、`POST /api/batches/{batch_id}/retry`、`GET /api/settlement`、`POST /api/settlement/compute`（只算不发）、`POST /api/settlement/publish`、`GET /api/settlement/versions`、`GET /api/settlement/versions/{version_id}`、`GET /api/settlement/readings`、`GET /api/settlement.csv`、`GET /api/settlement/{version_id}.csv`、`GET /api/health`、`GET /api/export/{monthly,intervals,quarantine,readings,settlement}.csv`、`GET /api/export/manifest.json`。OpenAPI 在 `/docs`。

`POST /api/settlement/publish` 的请求体 `{"supplements": ["readings_pv_supplement.csv"]}`；
不传 `supplements` 时沿用上次已并入的补抄包（只重发不丢补抄）。

## 目录

```text
app/            main.py config.py errors.py reasons.py manifest.py store.py web.py
app/modules/    m1_ingest m2_contract m3_quality m4_intervals m5_months m6_batches m7_export m8_settlement
app/templates/  base.html + 8 页
app/static/     theme.css layout.css components.css app.js table.js pipeline.js settlement.js
docs/           DEPTH.md DATA-DICTIONARY.md RUNBOOK.md
sample_data/    读数样例（脏 / 干净 / GBK / 截断 / 空态 / 光伏 / 光伏补抄）、meters.csv、prices.csv
tests/          conftest.py + 每模块一个测试文件 + test_e2e.py + test_invariants.py
```

## 测试

```bash
docker compose exec -T app python -m pytest -q     # 105 passed
```

规则版本：`RULE_VERSION`（`app/config.py`）。改规则要同步加测试，否则同一份输入会出现两个批次。
