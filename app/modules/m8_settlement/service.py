"""结算编排：从批次读数（可叠加补抄包）算出月度净结算草稿/发布版。

与 M6 共用同一批落库读数：基础批次已通过 M1→M3 的读数直接复用，
补抄包走同样的 M1→M3 后按业务键并入台账。计算与发布分离：
`compute_batch` 只算不写，`persist` 把结算行、台账与版本写进一个事务。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config, errors
from ..m2_contract import contract as contract_mod
from . import compute, ledger, publish


def _records_from_store(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        reading = row.get("reading_norm")
        if reading is None:
            continue
        out.append({
            "meter_id": row["meter_id"], "register": row["register"],
            "read_at": row["read_at"], "reading": Decimal(str(reading)),
            "reading_text": row.get("reading_text", ""),
            "unit_raw": row.get("unit_raw", ""), "read_type": row["read_type"],
            "status": row["status"], "lineno": row["lineno"],
            "segment": row.get("segment", 1), "sample": "",
            "note": row.get("note", ""), "flags": list(row.get("flags") or []),
        })
    return out


def base_rows_from_store(store, batch: dict) -> list[dict]:
    """从已落库批次重算时用：把 `reading_norm` 折回质量层的记录形态。"""
    records = []
    for row in store.load_readings(batch["batch_id"], "valid") + \
            store.load_readings(batch["batch_id"], "estimate"):
        reading = row.get("reading_norm")
        if reading is None:
            continue
        records.append({**row, "reading": Decimal(str(reading))})
    valid = [item for item in records if item["status"] == "valid"]
    estimate = [item for item in records if item["status"] == "estimate"]
    return base_rows_from(valid, estimate, batch["sample"])


def base_quarantine_from_store(store, batch_id: str) -> list[dict]:
    """优先复用上次落库的台账证据（隔离行带方向），没有就回退到 M1/M3 隔离区。

    只取 `source='base'`：补抄包的隔离行每次由补抄包重新生成，
    若也复用就会在同一份台账里出现两遍。
    """
    stored = [row for row in store.load_settlement_readings(batch_id)
              if row["status"] == "quarantine" and row["source"] == "base"]
    if stored:
        return [{
            "meter_id": row["meter_id"], "register": row["register"],
            "direction": row["direction"], "read_at": row["read_at"],
            "reading": row["reading"], "reading_text": row["reading_text"],
            "unit_raw": row["unit_raw"], "read_type": row["read_type"],
            "status": "quarantine", "code": row["code"] or "",
            "lineno": row["lineno"], "segment": row["segment"],
            "source": row["source"], "sample": row["sample"],
            "note": row["note"], "detail": row["detail"],
        } for row in stored]
    return store.load_quarantine(batch_id)


def base_rows_from(valid: list[dict], estimate: list[dict],
                   sample: str) -> list[dict]:
    """质量层输出的记录 → 台账行；有效与估抄分开保持状态。

    这里的记录直接来自 M2/M3，读数字段是 `reading`（已折到契约单位），
    与落库后 `reading_norm` 的形态不同，所以不走 `_records_from_store`。
    """
    rows: list[dict] = []
    for status, records in (("valid", valid), ("estimate", estimate)):
        for record in records:
            rows.append(ledger.row_from_record(record, source="base",
                                               sample=sample, status=status))
    return rows


def compute_batch(settings, store, batch: dict, *, supplements: list[str] | None = None,
                  contracts=None, prices=None, base_rows=None,
                  base_quarantine=None, base_raw_index=None) -> dict:
    contracts = contracts or contract_mod.load(settings)
    prices = prices or contract_mod.load_prices(settings)
    if base_rows is None:
        base_rows = base_rows_from_store(store, batch)
    if base_quarantine is None:
        base_quarantine = base_quarantine_from_store(store, batch["batch_id"])
    book = ledger.build(settings, contracts, base_rows, base_quarantine,
                        batch["sample"], supplements, base_raw_index)
    settlement = compute.compute(book, prices, contracts)
    stream = publish.stream_id(batch["sample"].rsplit(".", 1)[0])
    return {"ledger": book, "settlement": settlement, "stream": stream,
            "prices": prices, "contracts": contracts}


def available(settings) -> bool:
    contracts = contract_mod.load(settings)
    return any(contract.is_bidirectional for contract in contracts.values())


def available_samples(settings) -> list[dict]:
    from ..m1_ingest import service as m1_service
    return [item for item in m1_service.available_samples(settings)
            if item["name"].startswith("readings_pv")]


def sample_has_directions(settings, sample: str) -> bool:
    """样例里是否真的有购电/上网读数，决定能不能进结算。"""
    from ..m1_ingest import service as m1_service
    ingested = m1_service.ingest(settings, sample)
    contracts = contract_mod.load(settings)
    from ..m2_contract import service as m2_service
    records, _, _ = m2_service.interpret_rows(ingested.rows, contracts)
    return any(record["register"] in config.DIRECTION_REGISTERS
               for record in records)


def _prepare_versions(store, result: dict) -> list[dict]:
    return publish.build_versions(
        store, ledger=result["ledger"], settlement=result["settlement"],
        stream=result["stream"])


def persist(store, batch_id: str, result: dict, *, publish_now: bool) -> dict:
    """把结算草稿/发布版写入一个事务；失败整体回滚，旧版保持可复核。"""
    book = result["ledger"]
    settlement = result["settlement"]
    if not book["bidirectional"]:
        return {"published": False, "versions": [], "touched": []}
    versions = _prepare_versions(store, result) if publish_now else []
    inputs = [{"sample": item["sample"], "sha256": item["sha256"],
               "bytes": item["bytes"], "data_rows": item["data_rows"],
               "encoding": item["encoding"]}
              for item in book["supplements"]]
    months = [{
        "month": group["month"], "status": group["status"],
        "net_amount": group["net_amount"], "purchase_kwh": group["purchase_kwh"],
        "feedin_kwh": group["feedin_kwh"], "held_count": group["held_count"],
        "computed_at": publish.now_iso(),
        "rule_version": config.RULE_VERSION,
        "supplements": [item["sample"] for item in book["supplements"]],
        "content_hash": publish.content_hash(
            compute.month_payload(group, book)),
    } for group in settlement["months"]]
    lines = [{
        "month": group["month"], "meter_id": entry["meter_id"],
        "direction": line["direction"], "usage_kwh": line["usage_kwh"],
        "price": line["price"], "amount_yuan": line["amount_yuan"],
        "state": line["state"], "included": line["included"],
        "provisional": line["provisional"], "reason": line["reason"],
        "month_status": group["status"],
    } for group in settlement["months"] for entry in group["entries"]
        for line in entry["lines"]]
    readings = [{
        "source": row["source"], "sample": row["sample"],
        "lineno": row["lineno"], "segment": row["segment"],
        "meter_id": row["meter_id"], "register": row["register"],
        "direction": row["direction"], "read_at": row["read_at"],
        "reading_text": row["reading_text"], "reading": row.get("reading"),
        "unit_raw": row["unit_raw"], "read_type": row["read_type"],
        "status": row["status"], "code": row.get("code") or "",
        "note": row.get("note", ""), "detail": row.get("detail", ""),
    } for row in book["readings"]]
    conflicts = [{
        "meter_id": row["meter_id"], "register": row["register"],
        "direction": row["direction"], "read_at": row["read_at"],
        "source": row["source"], "sample": row["sample"],
        "lineno": row["lineno"], "reading_text": row["reading_text"],
        "code": row["code"], "detail": row.get("detail", ""),
    } for row in book["conflicts"]]
    written = store.save_settlement(
        batch_id=batch_id,
        settlement={"months": months, "lines": lines, "readings": readings,
                    "conflicts": conflicts, "inputs": inputs,
                    "checks": book["checks"]},
        stream_id=result["stream"],
        versions=[{"version_id": item["version_id"], "month": item["month"],
                   "content_hash": item["content_hash"],
                   "created_at": item["created_at"],
                   "payload": item["payload"], "note": item["note"]}
                  for item in versions],
        publish=publish_now)
    return {"published": publish_now, "versions": versions,
            "touched": written["touched"]}


def stored_supplements(store, batch_id: str) -> list[str]:
    """上次结算实际并入的补抄包清单（基础批次不进 `settlement_inputs`）。

    只返回白名单内且文件仍在的样例：样例被下线后重算应退回基础批次，而不是整批失败。
    """
    kept = []
    for item in store.load_settlement_inputs(batch_id):
        sample = item["sample"]
        if sample in config.SAMPLES:
            kept.append(sample)
    return kept


def recompute(settings, store, batch_id: str | None, *,
              supplements: list[str] | None = None,
              publish_now: bool = False) -> dict:
    """重算结算。

    `publish_now=False` 只算不写：预览不能冲掉已发布的结算行与版本；
    `publish_now=True` 才在一个事务里写「结算行 + 台账 + 新版本」，
    旧版本原样保留并标为 `superseded`。
    """
    from ..m6_batches import resume as resume_mod
    batch = resume_mod.require_batch(store, batch_id or "")
    if not available(settings):
        raise errors.BadRequest("契约里没有双向（购电/上网）表，无需净结算",
                                code="E_SETTLEMENT_MISSING")
    # 不显式给补抄包时沿用上次结算已并入的包：单纯「发布」不能把补抄月份打回缺向。
    applied = (stored_supplements(store, batch["batch_id"])
               if supplements is None else list(supplements))
    for sample in applied:
        _validate_supplement(settings, sample)
    result = compute_batch(settings, store, batch, supplements=applied)
    if not result["ledger"]["valid"] and not result["ledger"]["estimate"]:
        raise errors.BadRequest("批次没有购电/上网读数，无净结算可做",
                                code="E_SETTLEMENT_MISSING")
    if publish_now:
        outcome = persist(store, batch["batch_id"], result, publish_now=True)
    else:
        outcome = {"published": False, "versions": [], "touched": [],
                   "preview": True}
    result["persist"] = outcome
    result["batch"] = batch
    result["supplements"] = applied
    return result


def publish_existing(settings, store, batch_id: str | None) -> dict:
    """按库里已有的补抄清单重新发布当前结算内容。"""
    return recompute(settings, store, batch_id, publish_now=True)


def _validate_supplement(settings, sample: str) -> None:
    from ..m1_ingest import service as m1_service
    if sample not in config.SAMPLES:
        raise errors.BadRequest(f"补抄样例 {sample!r} 不在白名单内",
                                code="E_SAMPLE_UNKNOWN")
    m1_service.sample_path(settings, sample)
