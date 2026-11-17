"""不变量：守恒、幂等、月桶划分、账单闭合、读数分区。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.modules.m5_months import weights
from app.modules.m6_batches import service
from fixtures import expected


def test_conservation_intervals_equal_months(run_result):
    totals = run_result["totals"]
    assert Decimal(totals["interval_usage"]) == Decimal(totals["month_usage"])
    assert Decimal(totals["interval_usage"]) == expected.TOTAL_USAGE


def test_idempotent_run_keeps_rows(settings, store, batch_id):
    before = store.count_rows(batch_id)
    totals_before = store.load_totals(batch_id)
    service.run(settings, store, sample="readings_dirty.csv")
    assert store.count_rows(batch_id) == before
    assert store.load_totals(batch_id) == totals_before


def test_month_slices_partition_every_interval(store, batch_id):
    for interval in store.load_intervals(batch_id):
        slices = weights.month_slices(
            dt.datetime.fromisoformat(interval["start_at"]),
            dt.datetime.fromisoformat(interval["end_at"]))
        assert sum(value for _, value in slices) == interval["days"]
        assert all(value > 0 for _, value in slices)


def test_bill_components_sum_equals_amount(store, batch_id):
    for bill in store.load_bills(batch_id):
        total = sum((Decimal(item["amount"]) for item in bill["components"]), Decimal("0"))
        assert total == bill["amount_yuan"]
        assert bill["usage_kwh"] > 0


def test_readings_partition_holds(store, batch_id):
    valid = store.load_readings(batch_id, status="valid")
    estimate = store.load_readings(batch_id, status="estimate")
    quarantine = store.load_quarantine(batch_id)
    totals = store.load_totals(batch_id)
    assert len(valid) + len(estimate) + len(quarantine) == int(totals["raw_lines"])
    assert len(valid) == expected.VALID
    assert len(estimate) == expected.ESTIMATE


def test_interval_usage_never_negative(store, batch_id):
    for interval in store.load_intervals(batch_id):
        assert interval["usage_kwh"] > 0
        assert interval["delta"] > 0
        assert interval["end_at"] > interval["start_at"]


def test_pv_settlement_net_matches_included_lines(settings, store):
    """结算守恒：月净额 = 计入行（购电正、上网负）金额之和，跨月加总也对得上。"""
    from app.modules.m6_batches import service as batch_service
    result = batch_service.run(settings, store, sample="readings_pv.csv")
    batch_id = result["batch"]["batch_id"]
    months = store.load_settlement_months(batch_id)
    lines = store.load_settlement_lines(batch_id)
    for month in months:
        net = sum((line["amount_yuan"] if line["direction"] == "PURCHASE"
                   else -line["amount_yuan"])
                  for line in lines
                  if line["month"] == month["month"] and line["included"])
        assert net == month["net_amount"]
    assert sum((item["net_amount"] for item in months), Decimal("0.00")) == \
        Decimal(result["totals"]["settlement_net_amount"])


def test_single_direction_batches_are_untouched(settings, store):
    """单向批次口径不变：不产生结算行，区间账与月度账仍逐位相等。"""
    from app.modules.m6_batches import service as batch_service
    result = batch_service.run(settings, store, sample="readings_dirty.csv")
    batch_id = result["batch"]["batch_id"]
    assert store.load_settlement_months(batch_id) == []
    assert store.load_settlement_lines(batch_id) == []
    stages = {item["stage"]: item for item in result["stages"]}
    assert stages["settlement"]["status"] == "skipped"
    assert Decimal(result["totals"]["interval_usage"]) == Decimal(
        result["totals"]["month_usage"]) == expected.TOTAL_USAGE
