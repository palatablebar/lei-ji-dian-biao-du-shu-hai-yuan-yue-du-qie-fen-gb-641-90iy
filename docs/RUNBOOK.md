# 运行手册 meterback

## 1 启动

```bash
cd projects/gb-641
docker compose up -d --build
curl -sS http://localhost:18200/api/health
```

页面：<http://localhost:18200/>。首次打开没有批次，选样例点「开始处理」即可跑通。

容器内端口 8000，宿主 18200；SQLite 落在命名卷 `meterback-data` 的 `/data/meterback.sqlite`。

## 2 主流程

1. 总览页选 `readings_dirty.csv`（脏样例，13 类问题），点「开始处理」。
2. 阶段卡显示八段「进 / 出 / 隔离」（单向批次结算段显示「跳过」）；任一阶段失败可点该卡的「重试」。
3. 读数诊断页看逐行诊断与隔离区，点「隔离区 CSV」下载证据。
4. 区间还原页看翻转、换表末段、新表首读与标记，可点「重算区间」。
5. 月度分摊页看每月合计、账单与尾差，可点「重算分摊」。
6. 有光伏户时进「双向净结算」看月度净款、采用/暂挂方向与发布版本；「双向台账」看两向原始读数。
7. 批次对账页看四账与运行清单；导出页下载文件。

命令行同一套接口：

```bash
curl -sS -X POST http://localhost:18200/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"sample":"readings_dirty.csv"}'
curl -sS http://localhost:18200/api/reconcile | python3 -m json.tool
curl -sS -o /tmp/monthly.csv http://localhost:18200/api/export/monthly.csv
```

## 3 常见操作

- **同输入重跑**：再点一次「开始处理」或 POST 同一 `sample`，命中批次键，返回同一 `batch_id`，行数与合计不变。
- **强制重算**：POST `/api/batches/{batch_id}/retry`，用同一个 `sample` 重跑并覆盖该批次的行。
- **换样例**：下拉框里其余样例分别是干净、GBK 中文表头、末行截断、空文件、仅表头、全空行、表头缺列。
- **重跑测试**：`docker compose exec -T app python -m pytest -q`。
- **清库**：`docker compose down -v` 删除命名卷；下次启动自动建表。

## 4 失败排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 422 `E_EMPTY_FILE` | 样例文件 0 字节 | 选非空样例 |
| 422 `E_HEADER_ONLY` | 只有表头 | 补数据行 |
| 422 `E_ALL_BLANK` | 数据行全空 | 检查导出是否只带格式 |
| 422 `E_HEADER_INVALID` | 缺 `meter_id/register/read_at/reading` | 按列别名表改名 |
| 422 `E_CONTRACT_INVALID` | `meters.csv` 重复表号、零倍率、换表目标缺失 | 修契约 |
| 400 `E_SAMPLE_UNKNOWN` | 样例名不在白名单 | 用页面下拉框里的名字 |
| 404 `E_BATCH_NOT_FOUND` | 还没有批次就导出 | 先跑一次处理 |
| 四账「不平」 | 逻辑或数据被改 | 看差额字段与说明，逐账核对 |

日志：`docker compose logs -f app`（uvicorn 访问日志）。运行清单里的 `input_digest` 可用来确认两次运行读的是同一份输入。

## 5 复算

1. 从导出页取 `monthly.csv` 与 `manifest.json`；
2. `manifest.json` 的 `inputs[].sha256` 与样例文件比对，确认输入未变；
3. 逐月加总 `用量kWh` 应等于 `manifest.json` 的 `counters` 与 `accounts[1]`（区间账）两侧；
4. 账单 `金额元` 之和应等于 `accounts[3]` 两侧，分项之和等于账单额。
5. 有光伏户时，`manifest.json` 的 `settlement.months` 逐月给出状态与净额，
   `settlement.lines` 逐行列出两个方向；净额 = Σ（购电行金额）− Σ（上网行金额），只统计 `included=true` 的行。

## 6 双向净结算与补抄

### 6.1 缺向月份的裁决

月底只有一向先到时**不等齐**，按「单方向暂结 + 另一向暂挂」形成可发布的净结算结果：

- 已到方向按本方向电价计入本月净额；
- 缺向行 `state=suspended`、`included=0`、用量与金额都是 0，页面与下载都标「暂挂」；
- 该月状态 `partial`（缺向暂结），不缺行、不丢读数；
- 绝不把「购电 − 上网」的净电量套购电价——那会把缺的那一向当 0，上网收益与购电支出都会算错。

补抄到达后重算：两向齐 → 状态升为 `settled`，该月追加新版本，旧版标 `superseded` 但整份保留，
下载与页面都能指定旧版复核。

### 6.2 操作

```bash
# 只算不发：预览补抄后的结果，不动已发布的行与版本
curl -sS -X POST http://localhost:18200/api/settlement/compute \
  -H 'Content-Type: application/json' \
  -d '{"supplements":["readings_pv_supplement.csv"]}'

# 补抄并发布：追加新版本，旧版保留
curl -sS -X POST http://localhost:18200/api/settlement/publish \
  -H 'Content-Type: application/json' \
  -d '{"supplements":["readings_pv_supplement.csv"]}'

# 重发当前内容：不传 supplements 会沿用上次已并入的补抄包，内容没变则不新增版本
curl -sS -X POST http://localhost:18200/api/settlement/publish -d '{}'

# 看某月全部版本与某版的差异
curl -sS 'http://localhost:18200/api/settlement/versions?month=2026-05'
curl -sS http://localhost:18200/api/settlement/versions/SV-xxxx-202605-V01
```

### 6.3 发布中断

发布在一个 SQLite 事务里写「结算行 + 两向台账 + 版本」，中途任何失败整体回滚。
中断后旧版仍是唯一 `published` 版本，`/settlement` 与旧版 CSV 都还能打开；
重新点「补抄并发布」即可，不会出现半版数据。

### 6.4 重跑不新增电量

- 同一基础样例重跑：命中批次键，返回同一 `batch_id`，行数与合计不变；
- 同一补抄包重复发布：业务键相同，台账与版本都不变（内容哈希相同不追加版本）；
- 强制重试（`/api/batches/{id}/retry`）沿用已并入的补抄包，不会把补抄月份打回缺向。

### 6.5 两向原始读数

`/ledger` 与 `/api/settlement/readings` 列出基础包与补抄包的全部两向读数：

- `status` 为 `valid / estimate / duplicate / conflict / quarantine`；
- 被质量层隔离的行也保留原始值、行号、来源包与原因码，方向从原始单元格还原；
- 台账顶部的分区必须满足「输入行 = 采用 + 重复 + 冲突 + 隔离」，否则页面标「不平」。

## 7 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `METERBACK_DB` | `/data/meterback.sqlite` | SQLite 路径 |
| `METERBACK_SAMPLE_DIR` | `/app/sample_data` | 样例目录 |

规则版本写在 `app/config.py` 的 `RULE_VERSION`，进入运行清单；改规则必须同时加测试，否则同输入重跑会出现两个批次。
