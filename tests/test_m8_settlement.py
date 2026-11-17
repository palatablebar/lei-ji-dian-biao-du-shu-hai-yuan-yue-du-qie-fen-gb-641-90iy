"""M8 双向净结算：缺向暂挂、两向分别计价、补抄升版、发布中断、幂等重算。

样例 `readings_pv.csv`：P001 / P002 两台光伏户，购电与上网各一条累计序列。
2026-01~04 两向齐；2026-05 P001 缺上网、P002 缺购电（各只有一道先到）。
补抄包 `readings_pv_supplement.csv` 补上 2026-06-01 的两条收口读数。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app import config, errors
from app.modules.m8_settlement import compute, ledger, publish, service
from app.modules.m8_settlement import store_view

SUPPLEMENT = "readings_pv_supplement.csv"


@pytest.fixture()
def pv_batch(settings, store):
    from app.modules.m6_batches import service as batch_service
    return batch_service.run(settings, store, sample="readings_pv.csv")


@pytest.fixture()
def clean_batch(settings, store):
    from app.modules.m6_batches import service as batch_service
    return batch_service.run(settings, store, sample="readings_clean.csv")


def _months(store, batch_id):
    return {item["month"]: item for item in store.load_settlement_months(batch_id)}


def _lines(store, batch_id, month):
    return {(item["meter_id"], item["direction"]): item
            for item in store.load_settlement_lines(batch_id)
            if item["month"] == month}


# ---------- 缺向裁决 ----------

def test_missing_direction_month_is_partial_with_suspended_side(settings, store, pv_batch):
    """缺向月份：已到方向照常计入，缺的那一向标暂挂、不计入净额。"""
    batch_id = pv_batch["batch"]["batch_id"]
    months = _months(store, batch_id)
    may = months["2026-05"]
    assert may["status"] == "partial"
    assert may["held_count"] == 2
    lines = _lines(store, batch_id, "2026-05")
    # P001: 购电到了，上网缺
    assert lines[("P001", "PURCHASE")]["state"] == "adopted"
    assert lines[("P001", "PURCHASE")]["included"] is True
    assert lines[("P001", "FEEDIN")]["state"] == "suspended"
    assert lines[("P001", "FEEDIN")]["included"] is False
    assert lines[("P001", "FEEDIN")]["usage_kwh"] == Decimal("0.000")
    assert lines[("P001", "FEEDIN")]["reason"] == "K_DIRECTION_HELD"
    # P002: 上网到了，购电缺
    assert lines[("P002", "FEEDIN")]["state"] == "adopted"
    assert lines[("P002", "PURCHASE")]["state"] == "suspended"
    # 净额只含已到方向：90.00 + 32.00
    assert may["net_amount"] == Decimal("58.00")


def test_settled_month_uses_two_prices_not_net_quantity(settings, store, pv_batch):
    """两向齐的月份：购电 × 购电价 − 上网 × 上网价，不能用净电量套购电价。"""
    batch_id = pv_batch["batch"]["batch_id"]
    lines = _lines(store, batch_id, "2026-01")
    purchase = lines[("P001", "PURCHASE")]
    feedin = lines[("P001", "FEEDIN")]
    assert purchase["usage_kwh"] == Decimal("300.000")
    assert purchase["price"] == Decimal("0.60")
    assert feedin["usage_kwh"] == Decimal("120.000")
    assert feedin["price"] == Decimal("0.40")
    month = _months(store, batch_id)["2026-01"]
    assert month["net_amount"] == (Decimal("480") * Decimal("0.60")
                                   - Decimal("200") * Decimal("0.40"))
    # 净电量套购电价会得到 168.00，与分向计价差 40.00（上网按 0.60 抵了）
    naive = (Decimal("480") - Decimal("200")) * Decimal("0.60")
    assert naive != month["net_amount"]


def test_naive_net_quantity_reported_as_counterexample(settings, store, pv_batch):
    """缺向月份若拿净电量套购电价，暂挂侧按 0 算会凭空少算上网收益。"""
    batch = store.get_batch(pv_batch["batch"]["batch_id"])
    result = service.compute_batch(settings, store, batch)
    may = next(group for group in result["settlement"]["months"]
               if group["month"] == "2026-05")
    entry = next(item for item in may["entries"] if item["meter_id"] == "P001")
    assert entry["naive_net"] == Decimal("90.00") == entry["net_amount"]
    assert entry["held"] == ["FEEDIN"]


def test_zero_reading_is_valid_not_missing(settings, store, pv_batch):
    """0 用量是有效读数：1 月购电 0.0 → 300.0 只有一条区间，但方向算已覆盖。"""
    batch = store.get_batch(pv_batch["batch"]["batch_id"])
    result = service.compute_batch(settings, store, batch)
    jan = next(group for group in result["settlement"]["months"]
               if group["month"] == "2026-01")
    assert jan["status"] == "settled"


# ---------- 补抄与版本 ----------

def test_supplement_upgrades_partial_to_settled(settings, store, pv_batch):
    batch_id = pv_batch["batch"]["batch_id"]
    service.recompute(settings, store, batch_id, supplements=[SUPPLEMENT],
                      publish_now=True)
    may = _months(store, batch_id)["2026-05"]
    assert may["status"] == "settled"
    assert may["held_count"] == 0
    assert may["net_amount"] == Decimal("100.00")


def test_supplement_publishes_new_version_and_keeps_old(settings, store, pv_batch):
    batch_id = pv_batch["batch"]["batch_id"]
    stream = store_view.stream_for(store, store.get_batch(batch_id))
    before = store.list_versions(stream, "2026-05")
    assert len(before) == 1 and before[0]["payload"]["status"] == "partial"

    service.recompute(settings, store, batch_id, supplements=[SUPPLEMENT],
                      publish_now=True)
    after = store.list_versions(stream, "2026-05")
    assert [item["seq"] for item in after] == [1, 2]
    assert after[0]["status"] == "superseded"
    assert after[1]["status"] == "published"
    # 旧版内容原样保留，可单独复核
    assert after[0]["payload"]["net_amount"] == "58.00"
    assert after[1]["payload"]["net_amount"] == "100.00"
    assert after[1]["payload"]["version"]["previous_version_id"] == \
        after[0]["version_id"]


def test_version_change_reports_supplemented_direction(settings, store, pv_batch):
    batch_id = pv_batch["batch"]["batch_id"]
    service.recompute(settings, store, batch_id, supplements=[SUPPLEMENT],
                      publish_now=True)
    stream = store_view.stream_for(store, store.get_batch(batch_id))
    latest = store.list_versions(stream, "2026-05")[-1]["payload"]
    changes = {(line["meter_id"], line["direction"]): line["change"]
               for line in latest["change"]["lines"]}
    assert changes[("P001", "FEEDIN")] == "supplement"
    assert changes[("P002", "PURCHASE")] == "supplement"
    assert changes[("P001", "PURCHASE")] == "unchanged"
    assert latest["change"]["net_delta"] == "42.00"
    assert latest["change"]["status_from"] == "partial"
    assert latest["change"]["status_to"] == "settled"


def test_supplement_readings_listed_for_boundary_month(settings, store, pv_batch):
    """补抄变化要列出该月用到的补抄读数，含期末收口那一条。"""
    batch_id = pv_batch["batch"]["batch_id"]
    service.recompute(settings, store, batch_id, supplements=[SUPPLEMENT],
                      publish_now=True)
    stream = store_view.stream_for(store, store.get_batch(batch_id))
    latest = store.list_versions(stream, "2026-05")[-1]["payload"]
    readings = latest["supplement_readings"]
    assert {(row["meter_id"], row["direction"]) for row in readings} == {
        ("P001", "上网"), ("P002", "购电")}
    assert all(row["boundary"] == "期末收口" for row in readings)


def test_replaying_same_supplement_adds_no_rows_or_versions(settings, store, pv_batch):
    """同一抄表包重跑：台账行数、净额、版本数都不变。"""
    batch_id = pv_batch["batch"]["batch_id"]
    service.recompute(settings, store, batch_id, supplements=[SUPPLEMENT],
                      publish_now=True)
    stream = store_view.stream_for(store, store.get_batch(batch_id))
    rows = len(store.load_settlement_readings(batch_id))
    versions = len(store.list_versions(stream))
    totals = {item["month"]: item["net_amount"]
              for item in store.load_settlement_months(batch_id)}

    service.recompute(settings, store, batch_id, supplements=[SUPPLEMENT],
                      publish_now=True)
    assert len(store.load_settlement_readings(batch_id)) == rows
    assert len(store.list_versions(stream)) == versions
    assert {item["month"]: item["net_amount"]
            for item in store.load_settlement_months(batch_id)} == totals


def test_publish_without_supplements_keeps_applied_packages(settings, store, pv_batch):
    """只点「发布」不能把已补抄的月份打回缺向。"""
    batch_id = pv_batch["batch"]["batch_id"]
    service.recompute(settings, store, batch_id, supplements=[SUPPLEMENT],
                      publish_now=True)
    service.recompute(settings, store, batch_id, publish_now=True)
    may = _months(store, batch_id)["2026-05"]
    assert may["status"] == "settled"
    assert may["net_amount"] == Decimal("100.00")


# ---------- 发布中断与旧版保留 ----------

def test_failed_publish_rolls_back_and_keeps_old_version(settings, store, pv_batch,
                                                         monkeypatch):
    """发布途中失败：结算行与版本整体回滚，旧版仍是 published 且内容不变。"""
    import sqlite3
    batch_id = pv_batch["batch"]["batch_id"]
    stream = store_view.stream_for(store, store.get_batch(batch_id))
    before_versions = store.list_versions(stream, "2026-05")
    before_months = _months(store, batch_id)
    before_lines = store.load_settlement_lines(batch_id)
    before_readings = len(store.load_settlement_readings(batch_id))

    # 在事务中途（追加版本时）失败，验证真正的整体回滚，而不是仅拦截入口。
    def explode(conn, **kwargs):
        raise sqlite3.OperationalError("模拟发布中断")

    monkeypatch.setattr(store, "append_version", explode)
    with pytest.raises(sqlite3.OperationalError):
        service.recompute(settings, store, batch_id, supplements=[SUPPLEMENT],
                          publish_now=True)
    monkeypatch.undo()

    assert store.list_versions(stream, "2026-05") == before_versions
    assert _months(store, batch_id) == before_months
    assert store.load_settlement_lines(batch_id) == before_lines
    assert len(store.load_settlement_readings(batch_id)) == before_readings
    # 旧版仍是唯一发布版，可以继续复核
    current = store.list_versions(stream, "2026-05")
    assert [item["status"] for item in current] == ["published"]
    assert current[0]["payload"]["net_amount"] == "58.00"


def test_preview_does_not_touch_published_rows(settings, store, pv_batch):
    """只算不发的预览：不写结算行、不新增版本。"""
    batch_id = pv_batch["batch"]["batch_id"]
    stream = store_view.stream_for(store, store.get_batch(batch_id))
    before_versions = store.list_versions(stream)
    before = _months(store, batch_id)
    result = service.recompute(settings, store, batch_id,
                               supplements=[SUPPLEMENT], publish_now=False)
    assert result["persist"]["published"] is False
    assert store.list_versions(stream) == before_versions
    assert _months(store, batch_id) == before


# ---------- 台账与两向原始读数 ----------

def test_ledger_keeps_both_directions_and_partitions(settings, store, pv_batch):
    batch_id = pv_batch["batch"]["batch_id"]
    rows = store.load_settlement_readings(batch_id)
    adopted = [row for row in rows if row["status"] in ("valid", "estimate")]
    assert {row["direction"] for row in adopted} == {"PURCHASE", "FEEDIN"}
    assert all(row["reading"] is not None for row in adopted)
    counts = store_view.checks(store, batch_id)
    assert counts["input_rows"] == counts["adopted"] + counts["duplicate"] \
        + counts["conflict"] + counts["quarantine"]
    assert counts["balanced"] is True


def test_ledger_quarantined_rows_retain_direction_and_raw(tmp_path, monkeypatch):
    """被隔离的两向读数不能从台账消失：原值、行号、方向都要留。"""
    meter_file = tmp_path / "meters.csv"
    meter_file.write_text(
        "meter_id,name,capacity,multiplier,unit,registers,replaced_at,replaced_by\n"
        "P900,测试光伏,99999.9,1,kWh,PURCHASE;FEEDIN,,\n", encoding="utf-8")
    (tmp_path / "prices.csv").write_text(
        "register,price\nPURCHASE,0.60\nFEEDIN,0.40\n", encoding="utf-8")
    (tmp_path / "pv_bad.csv").write_text(
        "meter_id,register,read_at,reading,unit,read_type,note\n"
        "P900,PURCHASE,2026-01-01T00:00:00+08:00,0,kWh,N,\n"
        "P900,PURCHASE,2026-02-01T00:00:00+08:00,100,kWh,N,\n"
        "P900,FEEDIN,2026-01-01T00:00:00+08:00,0,kWh,N,\n"
        "P900,FEEDIN,2026-02-01T00:00:00+08:00,-5,kWh,N,负数上网\n",
        encoding="utf-8")
    monkeypatch.setitem(config.SAMPLES, "pv_bad.csv", "测试样例")
    test_settings = config.Settings(db_path=str(tmp_path / "db.sqlite"),
                                    sample_dir=str(tmp_path))
    from app.modules.m2_contract import contract as contract_mod
    from app.modules.m1_ingest import service as m1_service
    from app.modules.m2_contract import service as m2_service
    from app.modules.m3_quality import service as m3_service
    from app.modules.m8_settlement import service as settlement_service
    contracts = contract_mod.load(test_settings)
    ingested = m1_service.ingest(test_settings, "pv_bad.csv")
    records, contract_q, _ = m2_service.interpret_rows(ingested.rows, contracts)
    quality = m3_service.evaluate(records, contracts)
    book = ledger.build(
        test_settings, contracts,
        settlement_service.base_rows_from(quality["valid"], quality["estimate"],
                                          "pv_bad.csv"),
        ingested.quarantine + contract_q + quality["quarantine"], "pv_bad.csv",
        [], ledger.raw_index(ingested.rows))
    quarantined = [row for row in book["readings"] if row["status"] == "quarantine"]
    assert len(quarantined) == 1
    row = quarantined[0]
    assert row["direction"] == "FEEDIN"
    assert row["reading_text"] == "-5"
    assert row["lineno"] == 5
    assert row["code"] == "Q_NEGATIVE_READING"
    assert book["checks"]["balanced"] is True


def test_bidirectional_registers_do_not_pollute_single_direction_accounts(
        settings, store, pv_batch):
    """双向表只进结算账；单向批次的区间账仍只统计主寄存器，二者互不影响。"""
    batch_id = pv_batch["batch"]["batch_id"]
    totals = store.load_totals(batch_id)
    # 主寄存器（购电）区间合计 = 月度分摊合计
    assert Decimal(totals["interval_usage"]) == Decimal(totals["month_usage"])
    reconcile = __import__("json").loads(totals["reconcile_json"])
    assert reconcile["all_balanced"] is True


def test_net_amount_sums_to_month_groups(settings, store, pv_batch):
    batch_id = pv_batch["batch"]["batch_id"]
    months = store.load_settlement_months(batch_id)
    lines = store.load_settlement_lines(batch_id)
    for month in months:
        included = [line for line in lines if line["month"] == month["month"]
                    and line["included"]]
        net = sum((line["amount_yuan"] if line["direction"] == "PURCHASE"
                   else -line["amount_yuan"] for line in included), Decimal("0.00"))
        assert net == month["net_amount"]


def test_no_bidirectional_meters_reports_clear_error(settings, store, clean_batch):
    batch_id = clean_batch["batch"]["batch_id"]
    with pytest.raises(errors.BadRequest) as excinfo:
        service.recompute(settings, store, batch_id, publish_now=True)
    assert excinfo.value.code == "E_SETTLEMENT_MISSING"


# ---------- HTTP 接口与导出 ----------

def test_http_publish_then_old_version_download(client):
    """HTTP 走通：跑批次 → 补抄发布 → 旧版仍可单独下载复核。"""
    import json
    run = client.post("/api/runs", json={"sample": "readings_pv.csv"})
    assert run.status_code == 200
    first = client.get("/api/settlement").json()
    assert first["months"][-1]["status"] == "partial"
    old_id = first["versions"]["2026-05"][0]["version_id"]

    published = client.post("/api/settlement/publish",
                            json={"supplements": [SUPPLEMENT]})
    assert published.status_code == 200
    after = client.get("/api/settlement").json()
    assert after["months"][-1]["status"] == "settled"
    assert len(after["versions"]["2026-05"]) == 2

    old = client.get(f"/api/settlement/versions/{old_id}")
    assert old.status_code == 200
    assert old.json()["status"] == "superseded"
    assert old.json()["net_amount"] == "58.00"

    old_csv = client.get(f"/api/settlement/{old_id}.csv")
    assert old_csv.status_code == 200
    assert old_csv.text.startswith("﻿")
    assert "暂挂" in old_csv.text
    assert old_id in old_csv.text


def test_http_preview_does_not_publish(client):
    client.post("/api/runs", json={"sample": "readings_pv.csv"})
    preview = client.post("/api/settlement/compute",
                          json={"supplements": [SUPPLEMENT]})
    assert preview.status_code == 200
    assert preview.json()["published"] is False
    stored = client.get("/api/settlement").json()
    assert stored["months"][-1]["status"] == "partial"
    assert len(stored["versions"]["2026-05"]) == 1


def test_http_missing_version_returns_404(client):
    client.post("/api/runs", json={"sample": "readings_pv.csv"})
    missing = client.get("/api/settlement/versions/SV-nope")
    assert missing.status_code == 404
    assert missing.json()["error"] == "E_PUBLISH_NOT_FOUND"


def test_http_unknown_supplement_rejected(client):
    client.post("/api/runs", json={"sample": "readings_pv.csv"})
    bad = client.post("/api/settlement/publish",
                      json={"supplements": ["../secret.csv"]})
    assert bad.status_code == 400
    assert bad.json()["error"] == "E_SAMPLE_UNKNOWN"


def test_export_includes_settlement_and_manifest_state(client):
    import json
    client.post("/api/runs", json={"sample": "readings_pv.csv"})
    client.post("/api/settlement/publish", json={"supplements": [SUPPLEMENT]})

    csv_body = client.get("/api/export/settlement.csv").text
    header = csv_body.splitlines()[0].lstrip("﻿")
    assert header.startswith("月份,表号,方向,采用状态")
    assert "P001,上网,采用" in csv_body

    manifest = json.loads(client.get("/api/export/manifest.json").text)
    settlement = manifest["settlement"]
    assert settlement["checks"]["balanced"] is True
    assert settlement["months"][-1]["status"] == "settled"
    assert any(item["sample"] == SUPPLEMENT for item in settlement["inputs"])
    versions = settlement["months"][-1]["versions"]
    assert [item["seq"] for item in versions] == [1, 2]
    assert versions[0]["status"] == "superseded"
    assert versions[1]["change"]["change"] == "revised"
    assert versions[1]["change"]["net_delta"] == "42.00"


def test_partial_month_carries_marker(settings, store, pv_batch):
    """缺向月份在版本快照里带 `K_SETTLED_PARTIAL`，页面与下载都能识别。"""
    batch_id = pv_batch["batch"]["batch_id"]
    stream = store_view.stream_for(store, store.get_batch(batch_id))
    partial = store.list_versions(stream, "2026-05")[-1]["payload"]
    assert "K_SETTLED_PARTIAL" in partial["markers"]
    settled = store.list_versions(stream, "2026-01")[-1]["payload"]
    assert settled["markers"] == []


def test_version_change_csv_lists_supplemented_directions(client):
    """变化 CSV：逐向列出原/现用量与金额，补抄方向单独标出。"""
    client.post("/api/runs", json={"sample": "readings_pv.csv"})
    first = client.get("/api/settlement").json()
    client.post("/api/settlement/publish", json={"supplements": [SUPPLEMENT]})
    latest = client.get("/api/settlement").json()["versions"]["2026-05"][-1]
    assert latest["version_id"] != first["versions"]["2026-05"][0]["version_id"]

    body = client.get(f"/api/settlement/{latest['version_id']}-change.csv").text
    lines = body.splitlines()
    assert lines[0].lstrip("﻿").startswith("版本,月份,表号,方向,变化")
    assert "补抄" in body
    assert "未变" in body


def test_settlement_csv_marks_month_status_and_partial(client):
    """下载的当前结算 CSV 也带月状态与缺向标记，不只页面能看。"""
    client.post("/api/runs", json={"sample": "readings_pv.csv"})
    body = client.get("/api/settlement.csv").text
    header = body.splitlines()[0].lstrip("﻿")
    assert header.endswith("月状态,较上版")
    may = [line for line in body.splitlines() if line.startswith("2026-05")]
    assert any("缺向暂结" in line for line in may)
    assert any("K_SETTLED_PARTIAL" in line for line in may)
