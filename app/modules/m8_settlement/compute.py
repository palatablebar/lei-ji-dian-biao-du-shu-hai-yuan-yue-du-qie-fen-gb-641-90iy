"""双向净结算：购电、上网分别计价后轧差，缺向月份按「单方向暂结 + 另一向暂挂」发布。

裁决（缺向月份）：
1. 某户某月两个方向都有区间覆盖 → `settled`，净额 = 购电金额 − 上网金额；
2. 只有一个方向有区间覆盖 → `partial`：把已到方向按自己的电价计入本月净额，
   另一方向记 `suspended`（用量与金额都记 0，`included=0`），明确标注暂挂；
   不拿净电量去套购电价——净电量会把上网收益算反（见 `naive_net` 对照）；
3. 两个方向都没有区间 → 该月不出结算行（没有可结的量），月状态 `blocked` 只在
   同月其它户可结时保留说明。

补抄读数到达后重算，同一个月的两个方向都齐 → 升为 `settled`，该月发布新版本；
旧版本保留 `superseded` 供复核，下载结果里给出「较上版净额变化」与补抄读数明细。

本模块只做计算，不写库；发布由 `publish.py` 在单个事务里完成。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, ROUND_HALF_UP

from ... import config
from ..m2_contract import contract as contract_mod
from ..m5_months import weights
from . import ledger as ledger_mod


def _amount(usage: Decimal, price: Decimal) -> Decimal:
    return (usage * price).quantize(config.MONEY_QUANT, rounding=ROUND_HALF_UP)


def direction_usage(intervals: list[dict]) -> dict[tuple, Decimal]:
    """按 (月份, 表号, 方向) 汇总区间用量，口径与 M5 的最大余额法一致。"""
    out: dict[tuple, Decimal] = {}
    for interval in intervals:
        if interval["register"] not in config.DIRECTION_REGISTERS:
            continue
        start = dt.datetime.fromisoformat(interval["start_at"])
        end = dt.datetime.fromisoformat(interval["end_at"])
        _, parts = weights.allocate_interval(start, end, interval["usage_kwh"])
        for month, usage in parts:
            key = (month, interval["meter_id"], interval["register"])
            out[key] = out.get(key, Decimal("0")) + usage
    return out


def covered_months(intervals: list[dict]) -> dict[tuple, set[str]]:
    """某方向在某月「有区间覆盖」——即使用量为 0 也算读数已到。

    缺向判定看覆盖不看用量：0 用量是有效读数，缺读数是另一回事。
    """
    out: dict[tuple, set[str]] = {}
    for interval in intervals:
        if interval["register"] not in config.DIRECTION_REGISTERS:
            continue
        start = dt.datetime.fromisoformat(interval["start_at"])
        end = dt.datetime.fromisoformat(interval["end_at"])
        for month, days in weights.month_slices(start, end):
            if days > 0:
                out.setdefault((interval["meter_id"], interval["register"]),
                               set()).add(month)
    return out


def month_axis(intervals: list[dict]) -> list[str]:
    """结算月份轴：只取真正有区间覆盖的月份。

    区间止点落在下月 1 日 0 点，按半开区间它不属于下个月，所以不能用止点月份。
    """
    months = set()
    for interval in intervals:
        for month, days in weights.month_slices(
                dt.datetime.fromisoformat(interval["start_at"]),
                dt.datetime.fromisoformat(interval["end_at"])):
            if days > 0:
                months.add(month)
    return sorted(months)


def _lines_for(month: str, meter_id: str, contract: contract_mod.MeterContract,
               usage: dict[tuple, Decimal], coverage: dict[tuple, set[str]],
               prices: dict[str, Decimal]) -> list[dict]:
    lines = []
    for direction in config.DIRECTION_REGISTERS:
        present = month in coverage.get((meter_id, direction), set())
        kwh = usage.get((month, meter_id, direction), Decimal("0"))
        price = prices.get(direction, config.DEFAULT_PRICES[direction])
        if present:
            amount = _amount(kwh, price)
            lines.append({
                "direction": direction,
                "state": "adopted",
                "usage_kwh": kwh.quantize(config.USAGE_QUANT),
                "price": price,
                "amount_yuan": amount,
                "included": True,
                "provisional": False,
                "reason": "K_NET_ZERO" if kwh == 0 else "",
                "note": "用量为 0 的有效读数" if kwh == 0 else "",
            })
        else:
            lines.append({
                "direction": direction,
                "state": "suspended",
                "usage_kwh": Decimal("0.000"),
                "price": price,
                "amount_yuan": Decimal("0.00"),
                "included": False,
                "provisional": True,
                "reason": "K_DIRECTION_HELD",
                "note": f"{config.DIRECTION_LABELS[direction]}读数未到，暂挂不并入净额",
            })
    return lines


def _month_status(lines: list[dict]) -> str:
    adopted = [line for line in lines if line["state"] == "adopted"]
    if not adopted:
        return "blocked"
    if any(line["state"] == "suspended" for line in lines):
        return "partial"
    return "settled"


def _naive_net(lines: list[dict], purchase_price: Decimal) -> Decimal:
    """反例对照：净电量直接套购电价。缺向时净电量本身就是错的。"""
    net_kwh = sum(((line["usage_kwh"] if line["direction"] == "PURCHASE"
                    else -line["usage_kwh"]) for line in lines), Decimal("0"))
    return _amount(net_kwh, purchase_price)


def compute(ledger: dict, prices: dict[str, Decimal],
            contracts: dict[str, contract_mod.MeterContract]) -> dict:
    """输入合并后的台账（含双向区间），输出逐月逐户的净结算草稿。"""
    intervals = [item for item in ledger["intervals"]
                 if item["meter_id"] in ledger["bidirectional"]]
    usage = direction_usage(intervals)
    coverage = covered_months(intervals)
    months = month_axis(intervals)

    entries: list[dict] = []
    for month in months:
        for meter_id in sorted(ledger["bidirectional"]):
            contract = contracts[meter_id]
            lines = _lines_for(month, meter_id, contract, usage, coverage, prices)
            status = _month_status(lines)
            if status == "blocked":
                continue
            purchase = next(line for line in lines if line["direction"] == "PURCHASE")
            feedin = next(line for line in lines if line["direction"] == "FEEDIN")
            net = sum(((line["amount_yuan"] if line["direction"] == "PURCHASE"
                        else -line["amount_yuan"]) for line in lines
                       if line["state"] == "adopted"), Decimal("0.00"))
            entries.append({
                "month": month,
                "meter_id": meter_id,
                "status": status,
                "net_amount": net.quantize(config.MONEY_QUANT),
                "purchase_kwh": purchase["usage_kwh"],
                "feedin_kwh": feedin["usage_kwh"],
                "held": [line["direction"] for line in lines
                         if line["state"] == "suspended"],
                "lines": lines,
                "naive_net": _naive_net(lines, prices.get(
                    "PURCHASE", config.DEFAULT_PRICES["PURCHASE"])),
                "month_start": weights.month_start(month).isoformat(),
                "month_end": weights.month_start(
                    weights.shift_month(month, 1)).isoformat(),
            })

    month_groups = _group_months(entries)
    return {
        "entries": entries,
        "months": month_groups,
        "stats": _stats(entries, month_groups),
    }


def _status_rank(status: str) -> int:
    return {"settled": 0, "partial": 1, "blocked": 2}.get(status, 3)


def _group_months(entries: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        grouped.setdefault(entry["month"], []).append(entry)
    out = []
    for month in sorted(grouped):
        rows = sorted(grouped[month], key=lambda item: item["meter_id"])
        status = max((row["status"] for row in rows), key=_status_rank)
        out.append({
            "month": month,
            "status": status,
            "net_amount": sum((row["net_amount"] for row in rows),
                              Decimal("0.00")).quantize(config.MONEY_QUANT),
            "purchase_kwh": sum((row["purchase_kwh"] for row in rows),
                                Decimal("0.000")).quantize(config.USAGE_QUANT),
            "feedin_kwh": sum((row["feedin_kwh"] for row in rows),
                              Decimal("0.000")).quantize(config.USAGE_QUANT),
            "held_count": sum(1 for row in rows for line in row["lines"]
                              if line["state"] == "suspended"),
            "entries": rows,
        })
    return out


def _stats(entries: list[dict], months: list[dict]) -> dict:
    return {
        "entry_count": len(entries),
        "month_count": len(months),
        "settled_months": sum(1 for item in months if item["status"] == "settled"),
        "partial_months": sum(1 for item in months if item["status"] == "partial"),
        "held_directions": sum(item["held_count"] for item in months),
        "net_amount": sum((item["net_amount"] for item in months),
                          Decimal("0.00")).quantize(config.MONEY_QUANT),
        "purchase_kwh": sum((item["purchase_kwh"] for item in months),
                            Decimal("0.000")).quantize(config.USAGE_QUANT),
        "feedin_kwh": sum((item["feedin_kwh"] for item in months),
                          Decimal("0.000")).quantize(config.USAGE_QUANT),
    }


def supplement_readings_for(month: str, ledger_rows: list[dict]) -> list[dict]:
    """该月结算用到的补抄读数，含月末那一条「收口读数」。

    月末 00:00 的读数（下月月初）是本月区间的止点，按半开区间它不属于下月，
    但它决定了本月最后一个区间的用量，所以必须算进「补抄变化」。
    """
    start = weights.month_start(month)
    end = weights.month_start(weights.shift_month(month, 1))
    out = []
    for row in ledger_rows:
        if row.get("source") != "supplement" or not row.get("read_at"):
            continue
        stamp = dt.datetime.fromisoformat(row["read_at"]).astimezone(config.CHINA_TZ)
        if not (start <= stamp <= end):
            continue
        item = ledger_mod.evidence_row(row)
        item["boundary"] = "期末收口" if stamp == end else ""
        out.append(item)
    return out


def compare_versions(previous: dict | None, current: dict) -> dict:
    """当前月结算相对上一发布版本的变化（逐户、逐方向）。

    `previous` 是上一发布版本的整月 payload；`current` 是本月草稿的
    `{status, net_amount, lines}`，其中 `lines` 带 `meter_id`。
    """
    if previous is None:
        return {"change": "new", "net_delta": str(current["net_amount"]),
                "status_from": "", "status_to": current["status"],
                "lines": [{"meter_id": line.get("meter_id", ""),
                           "direction": line["direction"],
                           "change": "new" if line["state"] == "adopted"
                           else "suspended",
                           "usage_from": "0.000", "usage_to": str(line["usage_kwh"]),
                           "amount_from": "0.00",
                           "amount_to": str(line["amount_yuan"])}
                          for line in current["lines"]]}

    old_lines: dict[tuple, dict] = {}
    for entry in previous.get("entries", []):
        for line in entry.get("lines", []):
            old_lines.setdefault((entry.get("meter_id"), line["direction"]), line)
    changes = []
    for line in current["lines"]:
        key = (line.get("meter_id"), line["direction"])
        old = old_lines.get(key, {})
        if line["state"] == "adopted" and old.get("state") == "suspended":
            change = "supplement"
        elif line["state"] == "adopted" and old.get("state") == "adopted":
            change = ("unchanged" if
                      (Decimal(old.get("usage_kwh", "0")) == Decimal(
                          str(line["usage_kwh"]))
                       and Decimal(old.get("amount_yuan", "0")) == Decimal(
                           str(line["amount_yuan"])))
                      else "revised")
        elif line["state"] == "suspended" and old.get("state") == "adopted":
            change = "revoked"
        elif line["state"] == "suspended" and old.get("state") == "suspended":
            change = "unchanged"
        else:
            change = "new"
        changes.append({
            "meter_id": line.get("meter_id", ""),
            "direction": line["direction"], "change": change,
            "usage_from": old.get("usage_kwh", "0.000"),
            "usage_to": str(line["usage_kwh"]),
            "amount_from": old.get("amount_yuan", "0.00"),
            "amount_to": str(line["amount_yuan"]),
        })
    delta = (Decimal(str(current["net_amount"]))
             - Decimal(str(previous.get("net_amount", "0")))).quantize(
                 config.MONEY_QUANT)
    if delta == 0 and previous.get("status") == current["status"] \
            and all(item["change"] == "unchanged" for item in changes):
        change = "unchanged"
    else:
        change = "revised"
    return {"change": change, "net_delta": str(delta),
            "status_from": previous.get("status", ""),
            "status_to": current["status"], "lines": changes}


def evidence_for(row: dict) -> dict:
    """证据行转成可 JSON 序列化的展示形态。"""
    return {key: ("" if value is None else str(value))
            if isinstance(value, Decimal) else value
            for key, value in ledger_mod.evidence_row(row).items()} | {
        "reading": "" if row.get("reading") is None else str(row["reading"])}


def month_markers(group: dict) -> list[str]:
    """月度级标记：缺向月份打 `K_SETTLED_PARTIAL`，随版本快照与下载一起落库。"""
    if group["status"] == "partial":
        return ["K_SETTLED_PARTIAL", "K_DIRECTION_HELD"]
    return []


def month_payload(entry_group: dict, ledger: dict) -> dict:
    """可发布的月度快照：不依赖 run_id/时间戳，内容相同则哈希相同。"""
    entries = []
    for row in entry_group["entries"]:
        entries.append({
            "month": row["month"],
            "meter_id": row["meter_id"],
            "status": row["status"],
            "net_amount": str(row["net_amount"]),
            "purchase_kwh": str(row["purchase_kwh"]),
            "feedin_kwh": str(row["feedin_kwh"]),
            "held": list(row["held"]),
            "lines": [{
                "direction": line["direction"],
                "state": line["state"],
                "usage_kwh": str(line["usage_kwh"]),
                "price": str(line["price"]),
                "amount_yuan": str(line["amount_yuan"]),
                "included": line["included"],
                "provisional": line["provisional"],
                "reason": line["reason"],
                "note": line["note"],
            } for line in row["lines"]],
            "naive_net": str(row["naive_net"]),
        })
    return {
        "month": entry_group["month"],
        "status": entry_group["status"],
        "net_amount": str(entry_group["net_amount"]),
        "purchase_kwh": str(entry_group["purchase_kwh"]),
        "feedin_kwh": str(entry_group["feedin_kwh"]),
        "held_count": entry_group["held_count"],
        "markers": month_markers(entry_group),
        "entries": entries,
        "supplement_readings": supplement_readings_for(
            entry_group["month"], ledger["readings"]),
        "inputs": [{"sample": item["sample"], "sha256": item["sha256"]}
                   for item in ledger.get("supplements", [])],
    }


__all__ = ["compute", "direction_usage", "covered_months", "month_axis",
           "compare_versions", "month_payload", "month_markers",
           "supplement_readings_for"]
