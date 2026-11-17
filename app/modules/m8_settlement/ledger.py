"""双向读数台账：把基础抄表包与补抄包合并成一份可复核的两向读数清单。

合并规则与 M3 一致：业务键 `(表号, 寄存器, UTC 时间)`。
- 同键同值：保留来源最早的一条，其余记为 `duplicate`（留痕，不参与区间）；
- 同键不同值：全部记为 `conflict`（不猜哪条为准，不静默覆盖）；
- 合并后仍倒走：记为 `conflict` + `Q_REVERSE_READING`；
- 每个方向被隔离的原始行也留在台账里，原值、行号、方向、原因码都在。

因此台账恒有「输入行数 = 采用 + 重复 + 冲突 + 隔离」，逐项可核。
补抄包只是追加读数：同一补抄包重放命中的是同一业务键，台账逐位相同，不会多出电量。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config, reasons
from ..m1_ingest import service as m1_service
from ..m2_contract import contract as contract_mod
from ..m2_contract import coerce
from ..m2_contract import service as m2_service
from ..m3_quality import rules as quality_rules
from ..m3_quality import service as m3_service
from ..m4_intervals import restore

DIRECTIONS = config.DIRECTION_REGISTERS


def row_from_record(record: dict, *, source: str, sample: str, status: str) -> dict:
    return {
        "meter_id": record["meter_id"],
        "register": record["register"],
        "direction": record["register"],
        "read_at": record["read_at"],
        "reading": Decimal(str(record["reading"])),
        "reading_text": record.get("reading_text", ""),
        "unit_raw": record.get("unit_raw", ""),
        "read_type": record.get("read_type", "N"),
        "status": status,
        "code": "",
        "lineno": record["lineno"],
        "segment": record.get("segment", 1),
        "source": source,
        "sample": sample,
        "note": record.get("note", ""),
        "detail": "",
    }


def _direction_of(raw: str) -> str:
    try:
        register = coerce.parse_register(raw)
    except coerce.CoerceError:
        return ""
    return register if register in DIRECTIONS else ""


def quarantine_row(item: dict, *, sample: str,
                   raw_index: dict[int, dict] | None = None) -> dict:
    """隔离行进台账：方向尽量从原始单元格还原，原值与行号一律保留。"""
    cells = (raw_index or {}).get(item.get("lineno") or 0) or {}
    direction = _direction_of(cells.get("register", "")) or _direction_of(
        item.get("raw_value") or "") or _direction_of(item.get("field") or "")
    raw = item.get("raw_value") or cells.get("reading", "")
    # Q_BAD_REGISTER 的 raw_value 是寄存器文本，真正的读数在原始单元格里
    if item.get("field") == "register" and cells.get("reading"):
        raw = cells.get("reading")
    return {
        "meter_id": item.get("meter_id") or "",
        "register": direction,
        "direction": direction,
        "read_at": "",
        "reading": None,
        "reading_text": raw,
        "unit_raw": "",
        "read_type": "",
        "status": "quarantine",
        "code": item.get("code") or "",
        "lineno": item.get("lineno") or 0,
        "segment": item.get("segment") or 1,
        "source": item.get("source") or "base",
        "sample": item.get("sample") or sample,
        "note": "",
        "detail": item.get("detail") or "",
    }


def base_rows(records: list[dict], sample: str) -> list[dict]:
    """已通过 M2/M3 的读数记录 → 台账行。"""
    out = []
    for record in records:
        if record["register"] not in DIRECTIONS:
            continue
        status = "estimate" if record.get("read_type") == "E" else "valid"
        out.append(row_from_record(record, source="base", sample=sample,
                                   status=status))
    return out


def raw_index(rows: list[dict]) -> dict[int, dict]:
    """行号 → 原始单元格，用于给隔离行补回方向。"""
    return {row["lineno"]: row.get("cells", {}) for row in rows}


def supplement_rows(settings, sample: str,
                    contracts: dict[str, contract_mod.MeterContract],
                    meter_ids: set[str]) -> tuple[list[dict], list[dict], dict]:
    """补抄包走与主流程相同的 M1→M2→M3，坏行带原因码留在台账里。"""
    ingested = m1_service.ingest(settings, sample)
    records, contract_q, _ = m2_service.interpret_rows(ingested.rows, contracts)
    quality = m3_service.evaluate(records, contracts)
    rows: list[dict] = []
    for record in quality["valid"]:
        if record["meter_id"] in meter_ids and record["register"] in DIRECTIONS:
            rows.append(row_from_record(record, source="supplement", sample=sample,
                                        status="valid"))
    for record in quality["estimate"]:
        if record["meter_id"] in meter_ids and record["register"] in DIRECTIONS:
            rows.append(row_from_record(record, source="supplement", sample=sample,
                                        status="estimate"))
    index = raw_index(ingested.rows)
    quarantine = [quarantine_row(item, sample=sample, raw_index=index)
                  for item in (list(contract_q) + list(quality["quarantine"]))
                  if item.get("meter_id") in meter_ids]
    meta = {"sample": sample, "sha256": ingested.digest,
            "bytes": ingested.byte_len, "encoding": ingested.encoding,
            "data_rows": ingested.data_lines,
            "accepted": len(rows), "quarantine": len(quarantine)}
    return rows, quarantine, meta


def merge(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """按业务键合并：返回 (采用, 重复, 冲突)。不做后写覆盖。"""
    order = {"base": 0, "supplement": 1}
    groups: dict[tuple, list[tuple[int, int, dict]]] = {}
    for index, row in enumerate(rows):
        key = (row["meter_id"], row["register"], row["read_at"])
        groups.setdefault(key, []).append((order.get(row["source"], 9), index, row))

    kept: list[dict] = []
    duplicates: list[dict] = []
    conflicts: list[dict] = []
    for key in sorted(groups):
        items = sorted(groups[key], key=lambda item: (item[0], item[1]))
        values = {item[2]["reading"] for item in items}
        if len(values) > 1:
            detail = "同键取值不一致：" + " / ".join(sorted(str(v) for v in values))
            for _, _, row in items:
                conflicts.append({**row, "code": "Q_DUPLICATE_CONFLICT",
                                  "status": "conflict", "detail": detail})
            continue
        kept.append(items[0][2])
        for _, _, row in items[1:]:
            duplicates.append({**row, "code": "K_DUPLICATE_IDENTICAL",
                               "status": "duplicate",
                               "detail": f"与 {items[0][2]['source']} 第 "
                                         f"{items[0][2]['lineno']} 行同键同值"})
    kept.sort(key=lambda item: (item["meter_id"], item["register"],
                                item["read_at"], order.get(item["source"], 9),
                                item["lineno"]))
    return kept, duplicates, conflicts


def build(settings, contracts: dict[str, contract_mod.MeterContract],
          base: list[dict], base_quarantine: list[dict], base_sample: str,
          supplements: list[str] | None = None,
          base_raw_index: dict[int, dict] | None = None) -> dict:
    """合并基础与补抄读数，重建双向区间，返回台账与证据。

    `base_quarantine` 接受两种形态：M1/M3 原始隔离行（带 `raw_index` 补方向），
    或已落库的台账证据行（`status=quarantine`，直接使用）。
    """
    meter_ids = {mid for mid, contract in contracts.items() if contract.is_bidirectional}
    empty = {"bidirectional": {}, "contracts": contracts, "valid": [], "estimate": [],
             "readings": [], "intervals": [], "conflicts": [], "duplicates": [],
             "supplements": [], "lane": [], "checks": {
                 "input_rows": 0, "adopted": 0, "duplicate": 0, "conflict": 0,
                 "quarantine": 0, "balanced": True}}
    if not meter_ids:
        return empty

    rows = [row for row in base if row["status"] != "quarantine"
            and row["meter_id"] in meter_ids and row["register"] in DIRECTIONS]
    quarantine = []
    for item in base_quarantine:
        if item.get("meter_id") not in meter_ids:
            continue
        if item.get("status") == "quarantine" and "detail" in item:
            quarantine.append(item)
        else:
            quarantine.append(quarantine_row(item, sample=base_sample,
                                             raw_index=base_raw_index))
    supplement_meta: list[dict] = []
    for sample in supplements or []:
        extra, extra_q, meta = supplement_rows(settings, sample, contracts, meter_ids)
        rows.extend(extra)
        quarantine.extend(extra_q)
        supplement_meta.append(meta)

    kept, duplicates, conflicts = merge(rows)
    kept, dropped = quality_rules.monotonic_quarantine(kept, contracts)
    for item in dropped:
        conflicts.append({**item, "code": "Q_REVERSE_READING", "status": "conflict",
                          "detail": (f"读数 {item['reading']} 小于第 "
                                     f"{item['_prev_lineno']} 行的 {item['_prev']}")})

    valid = [item for item in kept if item["read_type"] == "N"]
    estimate = [item for item in kept if item["read_type"] == "E"]
    bidir = {mid: contracts[mid] for mid in sorted(meter_ids)}
    intervals = restore.build_intervals(valid, estimate, bidir)

    ledger_rows = kept + duplicates + conflicts + quarantine
    counters = {
        "input_rows": len(rows) + len(quarantine),
        "adopted": len(kept),
        "duplicate": len(duplicates),
        "conflict": len(conflicts),
        "quarantine": len(quarantine),
    }
    counters["valid"] = sum(1 for item in kept if item["read_type"] == "N")
    counters["estimate"] = sum(1 for item in kept if item["read_type"] == "E")
    counters["balanced"] = counters["input_rows"] == (
        counters["adopted"] + counters["duplicate"] + counters["conflict"]
        + counters["quarantine"])
    return {
        "bidirectional": bidir,
        "contracts": contracts,
        "valid": valid,
        "estimate": estimate,
        "readings": ledger_rows,
        "intervals": intervals,
        "conflicts": conflicts,
        "duplicates": duplicates,
        "supplements": supplement_meta,
        "lane": sorted({row["meter_id"] for row in ledger_rows}),
        "checks": counters,
    }


def evidence_row(row: dict) -> dict:
    reading = row.get("reading")
    return {
        "source": row.get("source", ""),
        "sample": row.get("sample", ""),
        "lineno": row.get("lineno", 0),
        "segment": row.get("segment", 1),
        "meter_id": row.get("meter_id", ""),
        "register": row.get("register", ""),
        "direction": config.DIRECTION_LABELS.get(row.get("direction", ""), "—"),
        "read_at": row.get("read_at", ""),
        "reading": "" if reading is None else str(reading),
        "reading_text": row.get("reading_text", ""),
        "unit_raw": row.get("unit_raw", ""),
        "read_type": row.get("read_type", ""),
        "status": row.get("status", ""),
        "code": row.get("code", ""),
        "reason": reasons.label(row.get("code") or ""),
        "note": row.get("note", ""),
        "detail": row.get("detail", ""),
    }


__all__ = ["DIRECTIONS", "base_rows", "quarantine_row", "raw_index",
           "supplement_rows", "merge", "build", "evidence_row"]
