"""运行清单导出：run_id、输入 sha256、参数、阶段计数、四账，可离线复算。"""
from __future__ import annotations

import json
from decimal import Decimal

from ... import reasons


def build(store, batch: dict, manifest_json: dict | None) -> dict:
    payload = {
        "batch_id": batch["batch_id"],
        "run_id": batch["run_id"],
        "batch_key": batch["batch_key"],
        "sample": batch["sample"],
        "status": batch["status"],
        "rule_version": batch["rule_version"],
        "input_digest": batch["input_digest"],
        "created_at": batch["created_at"],
        "finished_at": batch.get("finished_at"),
        "counts": store.count_rows(batch["batch_id"]),
        "totals": store.load_totals(batch["batch_id"]),
        "manifest": manifest_json or {},
        "reasons": reasons.catalog(),
    }
    settlement = settlement_payload(store, batch["batch_id"])
    if settlement is not None:
        payload["settlement"] = settlement
    return payload


def settlement_payload(store, batch_id: str) -> dict | None:
    """月度净结算摘要：采用方向、暂挂部分、发布版本与补抄输入，离线也能复核。"""
    from ..m8_settlement import store_view
    months = store.load_settlement_months(batch_id)
    batch = store.get_batch(batch_id)
    if not months or batch is None:
        return None
    versions = store.list_versions(store_view.stream_for(store, batch))
    by_month: dict[str, list[dict]] = {}
    for item in versions:
        by_month.setdefault(item["month"], []).append(item)
    return {
        "months": [{
            "month": item["month"], "status": item["status"],
            "net_amount": str(item["net_amount"]),
            "purchase_kwh": str(item["purchase_kwh"]),
            "feedin_kwh": str(item["feedin_kwh"]),
            "held_count": item["held_count"],
            "content_hash": item["content_hash"],
            "supplements": item["supplements"],
            "versions": [{
                "version_id": version["version_id"], "seq": version["seq"],
                "status": version["status"],
                "net_amount": version["payload"].get("net_amount", ""),
                "change": version["payload"].get("change"),
                "published_at": version["created_at"],
            } for version in by_month.get(item["month"], [])],
        } for item in months],
        "lines": [{
            "month": item["month"], "meter_id": item["meter_id"],
            "direction": item["direction"], "state": item["state"],
            "usage_kwh": str(item["usage_kwh"]), "price": str(item["price"]),
            "amount_yuan": str(item["amount_yuan"]),
            "included": item["included"], "provisional": item["provisional"],
            "reason": item["reason"],
        } for item in store.load_settlement_lines(batch_id)],
        "inputs": [{"sample": item["sample"], "sha256": item["sha256"],
                    "rows": item["rows"], "encoding": item["encoding"]}
                   for item in store.load_settlement_inputs(batch_id)],
        "checks": store_view.checks(store, batch_id),
    }


def render_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def summary(payload: dict | None) -> list[dict]:
    if not payload:
        return []
    manifest_part = payload.get("manifest") or {}
    inputs = manifest_part.get("inputs") or []
    rows = [
        {"label": "run_id", "value": payload["run_id"]},
        {"label": "批次号", "value": payload["batch_id"]},
        {"label": "输入摘要", "value": payload["input_digest"]},
        {"label": "规则版本", "value": payload["rule_version"]},
        {"label": "样例", "value": payload["sample"]},
        {"label": "输入文件", "value": "、".join(item["name"] for item in inputs)},
        {"label": "阶段数", "value": str(len(manifest_part.get("stages") or []))},
        {"label": "四账", "value": "平衡" if manifest_part.get("all_balanced") else "不平"},
    ]
    settlement = payload.get("settlement")
    if settlement:
        months = settlement.get("months") or []
        held = sum(item.get("held_count") or 0 for item in months)
        partial = sum(1 for item in months if item.get("status") == "partial")
        net = sum((Decimal(item["net_amount"]) for item in months), Decimal("0"))
        rows += [
            {"label": "结算月份", "value": str(len(months))},
            {"label": "缺向暂结", "value": f"{partial} 个月 / 暂挂 {held} 向"},
            {"label": "净款合计", "value": f"{net:.2f} 元"},
            {"label": "补抄包", "value": "、".join(
                item["sample"] for item in settlement.get("inputs") or []) or "无"},
        ]
    return rows
