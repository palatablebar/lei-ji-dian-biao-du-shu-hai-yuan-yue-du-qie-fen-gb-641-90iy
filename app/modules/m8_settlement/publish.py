"""发布：把月度净结算写成不可变的版本；发布失败整体回滚，旧版保持可复核。

规则：
1. 内容哈希只覆盖「月份、状态、逐户两向用量与金额、价目、暂挂说明」，
   不含 run_id、时间戳、样例文件名——同样的结算内容重复发布不会新增版本；
2. 内容变化时追加新版本（`seq` 递增），旧版本改为 `superseded`，
   但整份 JSON 保留，页面与下载都能指定旧版本复核；
3. 「结算行 + 台账 + 版本」在同一事务里写入；任一步失败整体回滚，
   旧版本与上一版结算行原样保留（`store.save_settlement` 只用一个事务）；
4. 版本号 `V<月份>-<seq>` 可单独下载，补抄变化按「上一发布版 → 本版」计算。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

from ... import config
from . import compute

STREAM_PREFIX = "pv-net"


def stream_id(stream_name: str) -> str:
    """一座台区一个发布流；样例名只作可读前缀，不影响哈希。"""
    return f"{STREAM_PREFIX}:{stream_name}"


def content_hash(payload: dict) -> str:
    stable = {
        "month": payload["month"],
        "status": payload["status"],
        "net_amount": payload["net_amount"],
        "markers": payload.get("markers") or [],
        "entries": [{
            "meter_id": item["meter_id"],
            "status": item["status"],
            "net_amount": item["net_amount"],
            "purchase_kwh": item["purchase_kwh"],
            "feedin_kwh": item["feedin_kwh"],
            "lines": [{"direction": line["direction"], "state": line["state"],
                       "usage_kwh": line["usage_kwh"], "price": line["price"],
                       "amount_yuan": line["amount_yuan"],
                       "included": line["included"],
                       "provisional": line["provisional"]}
                      for line in item["lines"]],
        } for item in payload["entries"]],
    }
    digest = hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()


def version_id(stream: str, month: str, seq: int) -> str:
    short = hashlib.sha256(stream.encode("utf-8")).hexdigest()[:6]
    return f"SV-{short}-{month.replace('-', '')}-V{seq:02d}"


def now_iso() -> str:
    return dt.datetime.now(config.UTC).isoformat()


def build_versions(store, *, ledger: dict, settlement: dict, stream: str) -> list[dict]:
    """为每个发生变化的月份准备版本；未变化的月份不新增版本。"""
    latest = store.latest_versions(stream)
    versions: list[dict] = []
    for group in settlement["months"]:
        payload = compute.month_payload(group, ledger)
        digest = content_hash(payload)
        current = latest.get(group["month"])
        if current is not None and current["content_hash"] == digest:
            continue
        seq = 1 if current is None else current["seq"] + 1
        previous = current["payload"] if current is not None else None
        payload["version"] = {
            "version_id": version_id(stream, group["month"], seq),
            "seq": seq,
            "content_hash": digest,
            "created_at": now_iso(),
            "status": "published",
            "previous_version_id": current["version_id"] if current else None,
            "supersedes": current["version_id"] if current else None,
        }
        payload["change"] = compute.compare_versions(previous, {
            "net_amount": str(group["net_amount"]), "status": group["status"],
            "lines": [{"meter_id": entry["meter_id"],
                       "direction": line["direction"],
                       "state": "adopted" if line["included"] else "suspended",
                       "usage_kwh": str(line["usage_kwh"]),
                       "amount_yuan": str(line["amount_yuan"])}
                      for entry in group["entries"] for line in entry["lines"]]})
        versions.append({
            "version_id": payload["version"]["version_id"],
            "month": group["month"],
            "content_hash": digest,
            "created_at": payload["version"]["created_at"],
            "payload": payload,
            "note": _note_for(group),
            "seq": seq,
        })
    return versions


def _note_for(group: dict) -> str:
    if group["status"] == "partial":
        parts = []
        for entry in group["entries"]:
            for direction in entry.get("held") or []:
                parts.append(f"{entry['meter_id']} 缺{config.DIRECTION_LABELS.get(direction, direction)}")
        return "缺向暂结：" + "、".join(parts) + "；缺向侧暂挂不并入净额"
    return "两向齐备，按净额结清"


__all__ = ["stream_id", "content_hash", "version_id", "build_versions", "now_iso"]
