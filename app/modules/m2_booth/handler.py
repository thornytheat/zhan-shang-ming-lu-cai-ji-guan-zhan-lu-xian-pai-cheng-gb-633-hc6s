"""M2 入口：列契约与展位语法表，以及展商网格页。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ... import config, errors, reasons
from ...web import Ctx, get_ctx, render
from . import contract, parse, schema, service

router = APIRouter(tags=["M2 展位解析"])


@router.get("/api/contract")
def api_contract(ctx: Ctx = Depends(get_ctx)) -> dict:
    batch = ctx.current_batch()
    page_versions: list[dict] = []
    if batch is not None:
        for page in ctx.store.load_pages(batch["batch_id"]):
            page_versions.append({
                "url": page["url"], "source": page["source"],
                "page_no": page["page_no"],
                "contract_version": page.get("contract_version") or "—",
                "from_snapshot": bool(page.get("from_snapshot")),
            })
    return {
        "rule_version": ctx.settings.rule_version,
        "schema_versions": schema.schema_rows(),
        "columns": contract.column_contract(),
        "columns_v1": contract.column_contract(schema.SCHEMA_V1),
        "columns_v2": contract.column_contract(schema.SCHEMA_V2),
        "required": list(contract.REQUIRED),
        "required_v2": list(schema.V2_REQUIRED),
        "evolution": contract.evolution_table(),
        "grammar": parse.grammar_payload(),
        "batch_contract": page_versions,
        "halls": [
            {"id": key, "name": hall["name"], "zone": hall["zone"],
             "x": str(hall["x"]), "y": str(hall["y"])}
            for key, hall in config.HALLS.items()
        ],
        "geometry": {
            "col_pitch_m": str(config.COL_PITCH_M),
            "row_pitch_m": str(config.ROW_PITCH_M),
            "cross_hall_penalty_m": str(config.CROSS_HALL_PENALTY_M),
            "walk_m_per_min": str(config.WALK_M_PER_MIN),
            "distance_unit": config.DIST_UNIT,
            "decimals": "0.1",
        },
        "reasons": reasons.catalog(),
    }


@router.get("/exhibitors")
def page_exhibitors(request: Request, batch_id: str | None = None,
                    hall: str = "", status: str = "",
                    ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch(batch_id)
    records: list[dict] = []
    issues: list[dict] = []
    summary = {"records": 0, "ok": 0, "unplaced": 0, "dup": 0}
    page_contract: list[dict] = []
    if batch is not None:
        bid = batch["batch_id"]
        records = ctx.store.load_exhibitors(bid)
        issues = [i for i in ctx.store.load_issues(bid) if i["stage"] == "booth"]
        summary = service.summary(records, issues)
        page_contract = [
            {"url": p["url"], "source": p["source"], "page_no": p["page_no"],
             "contract_version": p.get("contract_version") or "—",
             "from_snapshot": bool(p.get("from_snapshot"))}
            for p in ctx.store.load_pages(bid)
        ]
    rows = service.rows_for_page(records)
    if hall:
        rows = [r for r in rows if r["hall"] == hall]
    if status:
        rows = [r for r in rows if r["status"] == status]
    return render(request, "exhibitors.html", batch=batch, page="booth",
                  rows=rows, summary=summary, issues=issues, hall=hall, status=status,
                  status_options=[(k, v) for k, v in service.STATUS_LABELS.items()],
                  grammar=parse.grammar_payload(),
                  page_contract=page_contract,
                  schema_versions=schema.schema_rows(),
                  grid_cols=["契约", "取值", "馆", "展商", "展位号", "拆分馆", "排",
                              "位", "x(m)", "y(m)", "面积(㎡)", "优先级", "对接人",
                              "状态"],
                  page_title="展商网格")


@router.get("/api/exhibitors")
def api_exhibitors(batch_id: str | None = None, hall: str = "",
                   ctx: Ctx = Depends(get_ctx)) -> dict:
    batch = ctx.current_batch(batch_id)
    if batch is None:
        raise errors.NotFound("还没有完成的批次", code="E_NO_BATCH")
    records = ctx.store.load_exhibitors(batch["batch_id"])
    if hall:
        records = [r for r in records if r["hall"] == hall]
    return {"batch_id": batch["batch_id"], "count": len(records),
            "rows": service.rows_for_page(records)}
