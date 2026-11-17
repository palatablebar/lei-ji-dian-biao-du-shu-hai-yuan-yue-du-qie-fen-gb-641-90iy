"""M8 入口：月度净结算、补抄重算、发布、版本复核与两个方向的读数证据。"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Request, Response

from ... import config, errors
from ...web import Ctx, get_ctx, render
from ..m6_batches import resume as resume_mod
from . import ledger, service, store_view

router = APIRouter(tags=["M8 双向净结算"])


def _dec(value):
    return Decimal(str(value)) if value is not None else None


def _direction_payload(item: dict) -> dict:
    return {
        "direction": item["direction"],
        "label": config.DIRECTION_LABELS.get(item["direction"], item["direction"]),
        "state": item["state"],
        "state_label": config.SETTLEMENT_STATUS_LABELS.get(item["state"], item["state"]),
        "usage_kwh": str(item["usage_kwh"]),
        "price": str(item["price"]),
        "amount_yuan": str(item["amount_yuan"]),
        "included": item["included"],
        "provisional": item["provisional"],
        "reason": item["reason"],
        "reason_label": config.SETTLEMENT_STATUS_LABELS.get(item["reason"], item["reason"]),
        "note": item["note"],
    }


def _batch(ctx: Ctx, batch_id: str | None) -> dict:
    return resume_mod.require_batch(
        ctx.store, batch_id or (ctx.current_batch() or {}).get("batch_id", ""))


@router.get("/api/settlement")
def api_settlement(batch_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    batch = _batch(ctx, batch_id)
    view = store_view.overview(ctx.store, batch["batch_id"])
    versions = store_view.version_index(ctx.store, view["stream"])
    return {
        "batch_id": batch["batch_id"],
        "stream": view["stream"],
        "months": view["months"],
        "totals": view["totals"],
        "checks": view["checks"],
        "versions": versions,
        "supplements": view["supplements"],
    }


@router.post("/api/settlement/compute")
def api_compute(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    batch = _batch(ctx, body.get("batch_id"))
    supplements = _supplement_list(body)
    # 默认只算不发：页面预览不能冲掉已发布版本；要发布走 /publish 或显式 publish=true。
    publish_now = bool(body.get("publish"))
    result = service.recompute(ctx.settings, ctx.store, batch["batch_id"],
                               supplements=supplements, publish_now=publish_now)
    if publish_now:
        view = store_view.overview(ctx.store, batch["batch_id"])
        months = view["months"]
        versions = view["versions"]
    else:
        months = store_view.preview_months(result["settlement"])
        versions = store_view.version_index(
            ctx.store, store_view.stream_for(ctx.store, batch))
    return {
        "batch_id": batch["batch_id"],
        "published": publish_now,
        "versions": versions,
        "months": months,
        "totals": result["settlement"]["stats"],
        "ledger": _ledger_payload(result["ledger"]),
    }


def _ledger_payload(book: dict) -> dict:
    return {
        "checks": book["checks"],
        "readings": [ledger.evidence_row(row) for row in book["readings"]],
        "conflicts": [ledger.evidence_row(row) for row in book["conflicts"]],
        "supplements": book["supplements"],
    }


@router.post("/api/settlement/publish")
def api_publish(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    batch = _batch(ctx, body.get("batch_id"))
    result = service.recompute(ctx.settings, ctx.store, batch["batch_id"],
                               supplements=_supplement_list(body), publish_now=True)
    view = store_view.overview(ctx.store, batch["batch_id"])
    return {"batch_id": batch["batch_id"], "published": True,
            "versions": view["versions"], "months": view["months"]}


def _supplement_list(body: dict) -> list[str] | None:
    """请求没给 `supplements` 时返回 None（沿用上次已并入的补抄包）；给了空列表才算清空。"""
    if "supplements" not in body:
        return None
    raw = body.get("supplements") or []
    if isinstance(raw, str):
        return [raw]
    return [str(item) for item in raw if item]


@router.get("/api/settlement/versions")
def api_versions(batch_id: str | None = None, month: str | None = None,
                 ctx: Ctx = Depends(get_ctx)) -> dict:
    batch = _batch(ctx, batch_id)
    stream = store_view.stream_for(ctx.store, batch)
    items = ctx.store.list_versions(stream, month)
    return {"stream": stream, "versions": [store_view.version_payload(item)
                                          for item in items]}


@router.get("/api/settlement/versions/{version_id}")
def api_version(version_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    item = ctx.store.get_version(version_id)
    if item is None:
        raise errors.NotFound(f"发布版本 {version_id} 不存在",
                              code="E_PUBLISH_NOT_FOUND")
    return store_view.version_payload(item)

@router.get("/api/settlement/readings")
def api_readings(batch_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    batch = _batch(ctx, batch_id)
    rows = ctx.store.load_settlement_readings(batch["batch_id"])
    return {"batch_id": batch["batch_id"],
            "readings": [store_view.reading_payload(row) for row in rows],
            "checks": store_view.checks(ctx.store, batch["batch_id"])}


@router.get("/api/settlement.csv")
def api_settlement_csv(batch_id: str | None = None,
                       ctx: Ctx = Depends(get_ctx)) -> Response:
    batch = _batch(ctx, batch_id)
    body, filename = store_view.settlement_csv(ctx.store, batch["batch_id"])
    return Response(content=body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/api/settlement/{version_id}-change.csv")
def api_version_change_csv(version_id: str, ctx: Ctx = Depends(get_ctx)) -> Response:
    """必须先于 `/api/settlement/{version_id}.csv` 注册，否则前缀会被贪心吃掉。"""
    item = ctx.store.get_version(version_id)
    if item is None:
        raise errors.NotFound(f"发布版本 {version_id} 不存在",
                              code="E_PUBLISH_NOT_FOUND")
    body, filename = store_view.version_change_csv(item)
    return Response(content=body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/api/settlement/{version_id}.csv")
def api_version_csv(version_id: str, ctx: Ctx = Depends(get_ctx)) -> Response:
    item = ctx.store.get_version(version_id)
    if item is None:
        raise errors.NotFound(f"发布版本 {version_id} 不存在",
                              code="E_PUBLISH_NOT_FOUND")
    body, filename = store_view.version_csv(item)
    return Response(content=body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/settlement")
def page_settlement(request: Request, batch_id: str | None = None,
                    version_id: str | None = None,
                    ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch(batch_id)
    view = {"months": [], "versions": [], "totals": {}, "checks": {}, "stream": "",
            "supplements": []}
    selected = None
    history: list[dict] = []
    if batch is not None:
        view = store_view.overview(ctx.store, batch["batch_id"])
        if version_id:
            selected = ctx.store.get_version(version_id)
            if selected is not None:
                history = ctx.store.list_versions(selected["stream_id"],
                                                  selected["month"])
        elif view["versions"]:
            month = sorted(view["versions"])[0]
            selected = ctx.store.list_versions(view["stream"], month)[-1]
            if selected is not None:
                history = ctx.store.list_versions(view["stream"], month)
    return render(request, "settlement.html", page="settlement",
                  page_title="双向净结算", batch=batch, view=view,
                  selected=store_view.version_payload(selected) if selected else None,
                  history=[store_view.version_row(item) for item in history],
                  pv_samples=service.available_samples(ctx.settings),
                  sample_options=_supplement_options(ctx.settings))


@router.get("/ledger")
def page_ledger(request: Request, batch_id: str | None = None,
                ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch(batch_id)
    readings: list[dict] = []
    conflicts: list[dict] = []
    checks = {"input_rows": 0, "adopted": 0, "duplicate": 0, "conflict": 0,
              "quarantine": 0, "balanced": True}
    supplements: list[dict] = []
    if batch is not None:
        readings = [store_view.reading_payload(row) for row
                    in ctx.store.load_settlement_readings(batch["batch_id"])]
        conflicts = [store_view.conflict_payload(row) for row
                     in ctx.store.load_settlement_conflicts(batch["batch_id"])]
        checks = store_view.checks(ctx.store, batch["batch_id"])
        supplements = ctx.store.load_settlement_inputs(batch["batch_id"])
    return render(request, "ledger.html", page="ledger", page_title="双向读数台账",
                  batch=batch, readings=readings, conflicts=conflicts,
                  checks=checks, supplements=supplements,
                  by_direction=_by_direction(readings))


def _by_direction(readings: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for row in readings:
        if row["status"] in ("valid", "estimate"):
            counts[row["direction"]] = counts.get(row["direction"], 0) + 1
    return [{"direction": key, "count": value}
            for key, value in sorted(counts.items())]


def _supplement_options(settings) -> list[dict]:
    """可作为补抄包追加的样例：白名单里带 `supplement` 的样例。"""
    from ..m1_ingest import service as m1_service
    return [item for item in m1_service.available_samples(settings)
            if "supplement" in item["name"]]
