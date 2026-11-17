# 深度登记 meterback（累计电表读数还原与月度切分）

- 规模：中型（8 个功能模块 / 8 个应用页 / 105 条测试）
- 核心规则：**区间还原 + 按日权重月度切分 + 双向净结算**。读数按 `(表号, 寄存器, UTC 时间)` 排序后逐对还原：区间用量 =（本次 − 上次）× 综合倍率，满量容量翻转时补容量，换表处拆成「旧表末段 + 新表首读起算」两段，估抄行不作锚点但给区间打标记；区间用量按覆盖天数切到自然月（`[月初, 下月初)` 半开），余数用最大余额法分到 0.001 kWh，同月「总表 = 峰 + 平 + 谷」的残差并入最大分量。光伏户的购电、上网各一条累计序列，两向分别计价后再轧差；月底只有一向先到时按「单方向暂结 + 另一向暂挂」发布，补抄到达后追加新版本，旧版保留复核。
- 使用者与工作流：临江供电所抄表班 4 人、辖区 8 只单向示例表 + 2 只光伏双向表（实际 320 只），月度线损与电费原先靠 Excel 手拉；现在一次处理得到「区间用量 + 月度分摊 + 电费 + 光伏净结算」，每个数字都能追回原始行号与原因码。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 读数采信 | 导入样例，看逐行诊断、编码与分段 | POST /api/readings/import、页面 /readings | `app/modules/m1_ingest/service.py` `app/modules/m1_ingest/reader.py` `app/modules/m1_ingest/decode.py` `app/modules/m1_ingest/sniff.py` | `tests/test_m1_ingest.py` |
| M2 表计契约 | 看容量、倍率、寄存器、单位与电价，试跑一行解析 | GET /api/contract、POST /api/contract/preview | `app/modules/m2_contract/contract.py` `app/modules/m2_contract/coerce.py` `app/modules/m2_contract/units.py` | `tests/test_m2_contract.py` |
| M3 质量隔离 | 看原因码分布与隔离行，导出隔离区 CSV | GET /api/quarantine、GET /api/quarantine.csv | `app/modules/m3_quality/rules.py` `app/modules/m3_quality/quarantine.py` | `tests/test_m3_quality.py` |
| M4 区间还原 | 重算区间，看翻转、换表、估抄标记 | POST /api/intervals/rebuild、页面 /intervals | `app/modules/m4_intervals/restore.py` `app/modules/m4_intervals/rollover.py` `app/modules/m4_intervals/swaps.py` | `tests/test_m4_intervals.py` |
| M5 月度切分 | 重算分摊与账单，看每月合计与尾差 | POST /api/months/allocate、页面 /months | `app/modules/m5_months/allocate.py` `app/modules/m5_months/weights.py` `app/modules/m5_months/tou.py` | `tests/test_m5_months.py` |
| M6 批次与对账 | 跑全流程、查批次历史、看四账、失败重试 | POST /api/runs、GET /api/batches、GET /api/reconcile、页面 / 与 /batches | `app/modules/m6_batches/service.py` `app/modules/m6_batches/compare.py` `app/modules/m6_batches/history.py` `app/modules/m6_batches/resume.py` | `tests/test_m6_batches.py` |
| M7 导出 | 导出月度分摊、区间、隔离、读数证据、双向净结算、运行清单 | GET /api/export/monthly.csv、GET /api/export/manifest.json、页面 /export | `app/modules/m7_export/evidence.py` `app/modules/m7_export/csv_out.py` `app/modules/m7_export/manifest_out.py` | `tests/test_m7_export.py` |
| M8 双向净结算 | 看购电/上网分向用量、缺向暂挂、补抄发布与版本复核、两向原始读数台账 | POST /api/settlement/compute、POST /api/settlement/publish、GET /api/settlement、页面 /settlement 与 /ledger | `app/modules/m8_settlement/ledger.py` `app/modules/m8_settlement/compute.py` `app/modules/m8_settlement/publish.py` | `tests/test_m8_settlement.py` |

端到端：`tests/test_e2e.py`（脏样例 → 六页 200 → 四账 → 导出，含 4xx 失败输入与幂等重跑）。
不变量：`tests/test_invariants.py`（区间账 = 月度账、幂等重跑行数不变、月桶划分、账单闭合、读数分区）。

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 国产抄表软件导出的 GB18030 文件被声明成 UTF-8 | `open()` 直接抛 UnicodeDecodeError，或中文表头全乱码 | 声明只作首个候选，严格解码失败按 utf-8-sig → utf-8 → gb18030 回退，留 `K_CHARSET_FALLBACK` | `app/modules/m1_ingest/decode.py` | `tests/test_m1_ingest.py::test_gbk_headers_decoded_by_fallback` |
| TRAP-02 | Excel 另存的文件头带 UTF-8 BOM | 首列名变成 `\ufeffmeter_id`，按列名取值全空 | 解码即剥离 BOM，并对首格再 strip 一次，留 `K_BOM_STRIPPED` | `app/modules/m1_ingest/decode.py`、`app/modules/m1_ingest/sniff.py` | `tests/test_m1_ingest.py::test_bom_stripped_before_header_mapping` |
| TRAP-04 | 两次导出拼接，中间又出现一行表头 | 表头被当成一条读数，列数校验误报 | 逐格比对表头签名，命中即分段 +1 并跳过该行 | `app/modules/m1_ingest/sniff.py` | `tests/test_m1_ingest.py::test_repeated_header_split_into_segments` |
| TRAP-05 | 行尾多一个分隔符、列数与表头不一致 | 静默补空值，或后续列全部错位 | 严格列数校验，整行进隔离区并保留原始行文本 | `app/modules/m1_ingest/service.py` | `tests/test_m1_ingest.py::test_column_count_mismatch_isolated_with_lineno` |
| TRAP-06 | `1,212.5`、`３，０５０.0` 这类千分位与全角写法 | `Decimal()` 抛异常，或整列变成空值 | NFKC 折半角 + 去千分位 + 去单位后缀，再 Decimal 化 | `app/modules/m2_contract/coerce.py` | `tests/test_m2_contract.py::test_thousands_and_fullwidth_normalized` |
| TRAP-07 | `-` / `N/A` / `未知` 混在读数里 | 被当成 0 参与区间计算，凭空多一段零 | 哨兵清单集中定义，返回 None；`0` 仍是有效读数 | `app/modules/m2_contract/coerce.py`、`app/config.py` | `tests/test_m2_contract.py::test_sentinel_reading_is_three_state` |
| TRAP-08 | 表号 `007` 被当成数字 7 | 对不上契约，或换表链断掉 | 全链路按字符串保形，`007` 与 `7` 是两个表号 | `app/modules/m2_contract/contract.py` | `tests/test_m2_contract.py::test_meter_id_leading_zero_preserved`、`tests/test_m3_quality.py::test_unknown_meter_and_leading_zero_mismatch` |
| TRAP-10 | 0.001 kWh 刻度上累加几十行 | float 累加掉尾差，月度合计差 0.001~0.01 | 全链路 Decimal，量化只在切分与账单两处显式发生 | `app/modules/m5_months/weights.py` | `tests/test_m5_months.py::test_decimal_totals_exact` |
| TRAP-11 | 同一时刻写成 `2026-01-01T00:00:00+08:00` 与 `2025-12-31T16:00:00Z` | 落进两个时间桶，区间多一段零用量 | 先归一 UTC 再排序，无偏移按 +08:00 处理 | `app/modules/m2_contract/coerce.py` | `tests/test_m2_contract.py::test_timezone_offsets_collapse_to_same_instant` |
| TRAP-14 | 同一 (表号, 寄存器, 时间) 两条不同读数 | 后写覆盖，前一条消失且不留痕 | 两条都进隔离区，带各自原值与行号，不猜哪条为准 | `app/modules/m3_quality/rules.py` | `tests/test_m3_quality.py::test_duplicate_key_conflict_isolates_both_rows` |
| TRAP-15 | 同一块表一条 kWh 一条 MWh | 差值差 1000 倍，区间用量离谱 | 读数先折成契约单位，用量再折 kWh，单位跟着数字走 | `app/modules/m2_contract/units.py` | `tests/test_m2_contract.py::test_unit_mixing_normalized_to_contract_unit` |
| TRAP-16 | 传输截断，末行只剩半截且没有换行符 | 半行被当有效读数，凭空多一段 | 末行无换行 + 列数不齐 → `Q_TRUNCATED_ROW`，文件级留 `K_TRUNCATED_TAIL` | `app/modules/m1_ingest/service.py` | `tests/test_m1_ingest.py::test_truncated_tail_row_isolated` |
| TRAP-18 | 0 字节文件、只有表头、数据行全是空行 | 三种都返回「成功导入 0 行」，或直接除零 | 三个文件级原因码分开返回，不落半批数据 | `app/modules/m1_ingest/service.py` | `tests/test_m1_ingest.py::test_empty_states_return_distinct_codes` |
| TRAP-23 | 区间止点与下一段起点都算一次 | 相邻区间重复计入一天，月度用量偏高 | 区间与月桶都声明左闭右开 `[起, 止)`，相邻段首尾相接 | `app/modules/m5_months/weights.py`、`app/modules/m4_intervals/restore.py` | `tests/test_m5_months.py::test_month_slices_are_half_open`、`tests/test_m4_intervals.py::test_intervals_are_half_open_and_contiguous` |
| TRAP-24 | 各月/各分量各自四舍五入后合计不等 | 分完差 0.001~0.02，账永远差几分 | 最大余额法分月；分量残差并入最大分量；账单先合计再舍入，分项补差 | `app/modules/m5_months/weights.py`、`app/modules/m5_months/tou.py` | `tests/test_m5_months.py::test_largest_remainder_keeps_total_exact`、`tests/test_m5_months.py::test_tou_components_closed_to_total` |
| TRAP-25 | 光伏户月底只有购电或只有上网先到 | 拿「购电 − 上网」的净电量套购电价，缺的那一向被当成 0，上网收益凭空消失或购电被算成倒送 | 两向分别计价后轧差；缺向侧 `suspended` 暂挂不并入净额，月状态 `partial`，明确标注缺哪一向 | `app/modules/m8_settlement/compute.py` | `tests/test_m8_settlement.py::test_settled_month_uses_two_prices_not_net_quantity`、`tests/test_m8_settlement.py::test_missing_direction_month_is_partial_with_suspended_side` |
| TRAP-26 | 等两向读数齐了才结账 | 月结拖过财务关账日，或先结一版后无处标记「哪一向还欠着」 | 缺向即可发布：已到方向按本向电价结，缺向暂挂留痕，补抄后追加新版本 | `app/modules/m8_settlement/publish.py`、`app/modules/m8_settlement/handler.py` | `tests/test_m8_settlement.py::test_supplement_publishes_new_version_and_keeps_old` |
| TRAP-27 | 补抄包重复导入 / 基础包重跑 | 同一读数被算两次，电量翻倍 | 两向台账按 `(表号, 寄存器, UTC 时间)` 合并，同键同值只留最早一条，重跑台账逐位相同 | `app/modules/m8_settlement/ledger.py`、`app/modules/m8_settlement/service.py` | `tests/test_m8_settlement.py::test_replaying_same_supplement_adds_no_rows_or_versions` |
| TRAP-28 | 发布写到一半断掉 | 新版本写了一半、旧版被标成替代，结果既不能复核也说不清 | 结算行、台账、版本同一事务写入；失败整体回滚，旧版仍 `published` 且整份 JSON 可复核 | `app/store.py`、`app/modules/m8_settlement/publish.py` | `tests/test_m8_settlement.py::test_failed_publish_rolls_back_and_keeps_old_version` |
| TRAP-29 | 隔离区只有原因码没有方向 | 被隔离的上网读数与购电读数混在一张表里，追不回是哪一向 | 台账给隔离行补回方向（从原始单元格还原寄存器），原值、行号、来源包全部保留 | `app/modules/m8_settlement/ledger.py` | `tests/test_m8_settlement.py::test_ledger_quarantined_rows_retain_direction_and_raw` |

## 对账规则

- 读数账：原始数据行 = 有效读数 + 估抄 + 隔离行 + 空行（样例 60 = 45 + 1 + 14 + 0）。
- 区间账：主寄存器（总表优先）区间用量合计 = 月度分摊用量合计（0.001 kWh，逐位相等）。
- 月度账：同月同表「总表 = 峰 + 平 + 谷」，闭合后的残差必须为 0，闭合前的尾差条数单独计数。
- 账单账：账单金额 = 分项金额之和（分项先截尾再补分），四账在 `/` 与 `/batches` 页、`/api/reconcile`、运行清单里同源显示。
- 结算账：两向台账「输入行 = 采用 + 重复 + 冲突 + 隔离」；净额只统计 `included=1` 的结算行；月度净额 = 该月各户「购电金额 − 上网金额」之和，在 `/settlement`、`/months` 与运行清单 `settlement` 段同源显示。

## 反例与不变量

- 反例（朴素实现会挂）：`tests/test_m4_intervals.py::test_naive_subtraction_would_drop_rollover`（直接相减得负数，翻转段被丢）、`tests/test_m5_months.py::test_bill_rounding_closed_not_naive`（逐项四舍五入多 1 分）、`tests/test_m1_ingest.py::test_naive_splitlines_would_break_records`（带千分位的单元格被切成两列）、`tests/test_m8_settlement.py::test_settled_month_uses_two_prices_not_net_quantity`（净电量套购电价与分向计价差 40 元）。
- 不变量：`tests/test_invariants.py::test_conservation_intervals_equal_months`（守恒）、`tests/test_invariants.py::test_idempotent_run_keeps_rows`（幂等）、`tests/test_invariants.py::test_month_slices_partition_every_interval`（划分）、`tests/test_invariants.py::test_bill_components_sum_equals_amount`（账单闭合）、`tests/test_invariants.py::test_readings_partition_holds`（分区）。
- 结算不变量：`tests/test_m8_settlement.py::test_net_amount_sums_to_month_groups`（月净额 = 计入行之和）、`tests/test_m8_settlement.py::test_ledger_keeps_both_directions_and_partitions`（台账分区闭合）、`tests/test_m8_settlement.py::test_preview_does_not_touch_published_rows`（预览不改已发布行）。
- 端到端：`tests/test_e2e.py::test_full_pipeline_over_http`、`tests/test_m8_settlement.py::test_http_publish_then_old_version_download`。

## 已知不覆盖

- 只支持单表计的单向有功读数与「购电 / 上网」两向有功读数；需量寄存器、无功寄存器一律按 `Q_BAD_REGISTER` 隔离。
- 电价固定读 `sample_data/prices.csv`（峰 1.05 / 平 0.65 / 谷 0.32 / 购电 0.60 / 上网 0.40 元每 kWh），没有分时日历、阶梯电价与容量电费。
- 双向结算只按自然月轧差，不支持跨月结转（本月暂挂的上网读数补到后只修正本月，不重算前月）；上网收益为负时直接出负净额，不做退费单拆分。
- 补抄包按白名单样例追加，一次最多并入单份补抄包；不支持按行增量上传或按表号选择性补抄。
- 发布版本只保留内容与差异，不保存「谁点的发布」；没有审批流与回滚按钮（回滚=下载旧版后人工重录）。
- 换表链只支持一段（旧表 → 新表）；链式多次换表能识别但只在最后一段起算，中间段会缺口。
- 时区按固定 +08:00 处理，不引入 DST；跨时区台区不支持。
- 并行抄表同一时刻多条读数只按「取值是否一致」判定冲突，没有设备侧时钟偏移校正。
- 页面只做展示与触发，没有登录与权限；批次历史最多展示 20 条。
- 大文件未做分块导入（样例 60 行；按 320 只表月均 700 行估算，单文件仍在内存可接受范围）。
