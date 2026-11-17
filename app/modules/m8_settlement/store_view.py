"""页面/接口展示层：把落库结果拼成「采用方向、暂挂部分、版本变化」三块可读视图。

每个数字都来自同一份落库行：结算行（`settlements`）、月度汇总（`settlement_months`）、
版本快照（`settlement_versions`）。页面与下载共用这里的函数，不各自再算一遍。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config, reasons
from ..m7_export import csv_out

SETTLEMENT_HEADER = ["月份", "表号", "方向", "采用状态", "用量kWh", "电价", "金额元",
                     "是否计入净额", "暂挂", "原因", "版本", "月状态", "较上版"]
CHANGE_HEADER = ["版本", "月份", "表号", "方向", "变化", "原用量kWh", "现用量kWh",
                 "原金额元", "现金额元"]


def stream_for(store, batch: dict) -> str:
    """批次 → 发布流。流只按样例名（去掉扩展名）区分，与版本内容哈希无关。"""
    from . import publish
    return publish.stream_id(batch["sample"].rsplit(".", 1)[0])


def overview(store, batch_id: str) -> dict:
    months = store.load_settlement_months(batch_id)
    lines = store.load_settlement_lines(batch_id)
    inputs = store.load_settlement_inputs(batch_id)
    stream = ""
    versions: dict[str, list[dict]] = {}
    batch = store.get_batch(batch_id)
    if batch is not None:
        stream = stream_for(store, batch)
        for item in store.list_versions(stream):
            versions.setdefault(item["month"], []).append(item)
    grouped: dict[str, list[dict]] = {}
    for line in lines:
        grouped.setdefault(line["month"], []).append(line)
    rows = []
    for month in months:
        latest = versions.get(month["month"], [])
        rows.append({
            "month": month["month"],
            "status": month["status"],
            "status_label": config.SETTLEMENT_STATUS_LABELS.get(
                month["status"], month["status"]),
            "net_amount": month["net_amount"],
            "purchase_kwh": month["purchase_kwh"],
            "feedin_kwh": month["feedin_kwh"],
            "held_count": month["held_count"],
            "lines": grouped.get(month["month"], []),
            "version": latest[-1] if latest else None,
            "version_count": len(latest),
        })
    totals = {
        "month_count": len(rows),
        "settled_months": sum(1 for row in rows if row["status"] == "settled"),
        "partial_months": sum(1 for row in rows if row["status"] == "partial"),
        "held_directions": sum(row["held_count"] for row in rows),
        "net_amount": sum((row["net_amount"] for row in rows),
                          Decimal("0.00")).quantize(config.MONEY_QUANT),
        "purchase_kwh": sum((row["purchase_kwh"] for row in rows),
                            Decimal("0.000")).quantize(config.USAGE_QUANT),
        "feedin_kwh": sum((row["feedin_kwh"] for row in rows),
                          Decimal("0.000")).quantize(config.USAGE_QUANT),
    }
    return {
        "months": rows,
        "stream": stream,
        "versions": {month: [
            {"version_id": item["version_id"], "seq": item["seq"],
             "status": item["status"], "month": item["month"],
             "note": item["note"], "created_at": item["created_at"],
             "net_amount": item["payload"].get("net_amount", ""),
             "month_status": item["payload"].get("status", ""),
             "change": item["payload"].get("change") or {}}
            for item in items] for month, items in versions.items()},
        "totals": totals,
        "checks": checks(store, batch_id),
        "supplements": inputs,
    }


def preview_months(settlement: dict) -> list[dict]:
    """未发布的草稿预览：形状与 `overview()["months"]` 一致，但不落库。"""
    rows = []
    for group in settlement["months"]:
        lines = []
        for entry in group["entries"]:
            for line in entry["lines"]:
                lines.append({
                    "month": group["month"], "meter_id": entry["meter_id"],
                    "direction": line["direction"], "usage_kwh": line["usage_kwh"],
                    "price": line["price"], "amount_yuan": line["amount_yuan"],
                    "state": line["state"], "included": line["included"],
                    "provisional": line["provisional"], "reason": line["reason"],
                })
        rows.append({
            "month": group["month"], "status": group["status"],
            "status_label": config.SETTLEMENT_STATUS_LABELS.get(
                group["status"], group["status"]),
            "net_amount": group["net_amount"],
            "purchase_kwh": group["purchase_kwh"],
            "feedin_kwh": group["feedin_kwh"],
            "held_count": group["held_count"], "lines": lines,
            "version": None, "version_count": 0,
        })
    return rows


def checks(store, batch_id: str) -> dict:
    """台账分区：以落库时算好的分区计数为准，避免重算后口径漂移。"""
    stored = store.load_settlement_checks(batch_id)
    if stored:
        return {key: (_as_int(value) if key != "balanced" else value == "True")
                for key, value in stored.items()}
    rows = store.load_settlement_readings(batch_id)
    counts = {"input_rows": 0, "adopted": 0, "duplicate": 0, "conflict": 0,
              "quarantine": 0}
    by_status = {"valid": 0, "estimate": 0, "duplicate": 0, "conflict": 0,
                 "quarantine": 0}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    counts["adopted"] = by_status["valid"] + by_status["estimate"]
    counts["duplicate"] = by_status["duplicate"]
    counts["conflict"] = by_status["conflict"]
    counts["quarantine"] = by_status["quarantine"]
    counts["input_rows"] = sum(counts[key] for key in
                               ("adopted", "duplicate", "conflict", "quarantine"))
    counts["valid"] = by_status["valid"]
    counts["estimate"] = by_status["estimate"]
    counts["balanced"] = counts["input_rows"] == (
        counts["adopted"] + counts["duplicate"] + counts["conflict"]
        + counts["quarantine"])
    return counts


def _as_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def reading_payload(row: dict) -> dict:
    reading = row.get("reading")
    return {
        "source": row["source"], "sample": row["sample"],
        "lineno": row["lineno"], "segment": row["segment"],
        "meter_id": row["meter_id"], "register": row["register"],
        "direction": config.DIRECTION_LABELS.get(row["direction"], "—"),
        "read_at": row["read_at"], "reading_text": row["reading_text"],
        "reading": "" if reading is None else str(reading),
        "unit_raw": row["unit_raw"], "read_type": row["read_type"],
        "status": row["status"], "status_label": config.SETTLEMENT_STATUS_LABELS.get(
            row["status"], row["status"]),
        "code": row["code"] or "", "reason": reasons.label(row["code"]),
        "note": row["note"], "detail": row["detail"],
    }


def conflict_payload(row: dict) -> dict:
    return {
        **{key: row.get(key, "") for key in
           ("meter_id", "register", "read_at", "source", "sample", "lineno",
            "reading_text", "code", "detail")},
        "direction": config.DIRECTION_LABELS.get(row.get("direction", ""), "—"),
        "reason": reasons.label(row.get("code")),
    }


def version_payload(row: dict | None) -> dict | None:
    if row is None:
        return None
    payload = row.get("payload") or {}
    return {
        **payload,
        "version_id": row["version_id"],
        "seq": row["seq"],
        "status": row["status"],
        "note": row["note"],
        "created_at": row["created_at"],
        "batch_id": row["batch_id"],
        "content_hash": row["content_hash"],
        "previous_version_id": (payload.get("version") or {}).get(
            "previous_version_id"),
    }


def version_row(row: dict) -> dict:
    return {
        "version_id": row["version_id"], "seq": row["seq"], "status": row["status"],
        "month": row["month"], "note": row["note"], "created_at": row["created_at"],
        "net_amount": row["payload"].get("net_amount", ""),
        "month_status": row["payload"].get("status", ""),
        "change": (row["payload"].get("change") or {}).get("change", ""),
        "net_delta": (row["payload"].get("change") or {}).get("net_delta", ""),
    }


def version_index(store, stream: str) -> dict:
    out: dict[str, list[dict]] = {}
    for item in store.list_versions(stream):
        out.setdefault(item["month"], []).append(version_row(item))
    return out


def version_csv(item: dict) -> tuple[str, str]:
    payload = item["payload"]
    rows = []
    for entry in payload.get("entries", []):
        for line in entry["lines"]:
            rows.append([
                payload["month"], entry["meter_id"],
                config.REGISTER_LABELS.get(line["direction"], line["direction"]),
                config.SETTLEMENT_STATUS_LABELS.get(line["state"], line["state"]),
                f"{Decimal(line['usage_kwh']):.3f}", str(line["price"]),
                f"{Decimal(line['amount_yuan']):.2f}",
                "是" if line["included"] else "否",
                "是" if line["provisional"] else "否",
                line.get("reason", ""),
                item["version_id"],
                config.SETTLEMENT_STATUS_LABELS.get(payload.get("status", ""),
                                                    payload.get("status", "")),
                " ".join(payload.get("markers") or []),
            ])
    body = csv_out.render(SETTLEMENT_HEADER, rows)
    return body, f"settlement-{item['version_id']}.csv"


def version_change_csv(item: dict) -> tuple[str, str]:
    """该版本相对上一版的逐向差异，单独成表，方便财务核对补抄带来的净款变化。"""
    payload = item["payload"]
    change = payload.get("change") or {}
    rows = [[item["version_id"], payload["month"], line.get("meter_id", ""),
             config.REGISTER_LABELS.get(line["direction"], line["direction"]),
             config.SETTLEMENT_STATUS_LABELS.get(line["change"], line["change"]),
             line.get("usage_from", ""), line.get("usage_to", ""),
             line.get("amount_from", ""), line.get("amount_to", "")]
            for line in change.get("lines") or []]
    header = list(CHANGE_HEADER)
    body = csv_out.render(header, rows)
    return body, f"settlement-change-{item['version_id']}.csv"


def settlement_csv(store, batch_id: str) -> tuple[str, str]:
    rows = []
    lines = store.load_settlement_lines(batch_id)
    versions: dict[str, dict] = {}
    batch = store.get_batch(batch_id)
    if batch is not None:
        for item in store.list_versions(stream_for(store, batch)):
            versions[item["month"]] = item
    for line in lines:
        version = versions.get(line["month"])
        rows.append([
            line["month"], line["meter_id"],
            config.REGISTER_LABELS.get(line["direction"], line["direction"]),
            config.SETTLEMENT_STATUS_LABELS.get(line["state"], line["state"]),
            f"{line['usage_kwh']:.3f}", str(line["price"]),
            f"{line['amount_yuan']:.2f}",
            "是" if line["included"] else "否",
            "是" if line["provisional"] else "否",
            line["reason"],
            version["version_id"] if version else "",
            config.SETTLEMENT_STATUS_LABELS.get(line["month_status"],
                                                line["month_status"]),
            " ".join((version["payload"].get("markers") or []) if version else []),
        ])
    return csv_out.render(SETTLEMENT_HEADER, rows), f"settlement-{batch_id}.csv"
