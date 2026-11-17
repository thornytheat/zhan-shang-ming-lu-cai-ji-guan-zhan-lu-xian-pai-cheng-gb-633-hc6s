"""M5 入口：批次列表、单批详情、版本链、冻结快照，以及批次版本页。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ... import errors
from ...web import Ctx, get_ctx, render
from . import history, service

router = APIRouter(tags=["M5 批次版本"])


@router.get("/api/batches")
def api_batches(limit: int = 20, ctx: Ctx = Depends(get_ctx)) -> dict:
    rows = history.rows(ctx.store, limit=limit)
    return {"count": len(rows), "batches": rows}


@router.get("/api/batches/{batch_id}/snapshots")
def api_batch_snapshots(batch_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    """查看批次冻结的采集原文：每页原始文本、表头与列契约版本。

    续跑只读这份快照，不重新抓取在线页面；隔离行的来源 URL/行号与之对应。
    """
    if ctx.store.get_batch(batch_id) is None:
        raise errors.NotFound(f"批次 {batch_id} 不存在", code="E_NOT_FOUND")
    snapshots = ctx.store.load_snapshots(batch_id)
    rows = []
    for url, snap in sorted(snapshots.items(),
                            key=lambda kv: (kv[1].source, kv[1].page_no)):
        rows.append({
            "url": url, "source": snap.source, "kind": snap.kind,
            "page_no": snap.page_no, "status": snap.status,
            "sha256": snap.sha256, "bytes": len(snap.raw),
            "encoding": snap.encoding,
            "declared_charset": snap.declared_charset,
            "header": snap.header,
            "contract_version": snap.contract_version,
            "raw_text_preview": _preview(snap),
        })
    return {"batch_id": batch_id, "count": len(rows), "snapshots": rows}


def _preview(snap, limit: int = 2000) -> str:
    """原始字节按冻结编码解码后的文本预览；解码失败给占位，不抛异常。"""
    if not snap.raw:
        return ""
    for enc in (snap.encoding, "utf-8", "gb18030"):
        if not enc:
            continue
        try:
            return snap.raw.decode(enc, errors="replace")[:limit]
        except (LookupError, UnicodeDecodeError):
            continue
    return snap.raw.decode("utf-8", errors="replace")[:limit]


@router.get("/api/batches/{batch_id}")
def api_batch(batch_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    payload = service.detail(ctx.store, batch_id)
    return {
        "batch": payload["batch"],
        "stages": payload["stage_rows"],
        "counts": payload["counts"],
        "totals": payload["totals"],
        "reconciliation": payload["reconciliation"],
        "watermark": payload["watermark"],
        "versions": payload["version_rows"],
    }


@router.get("/api/batches/{batch_id}/plan")
def api_resume_plan(batch_id: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.plan(ctx.store, batch_id)


@router.get("/batches")
def page_batches(request: Request, batch_id: str | None = None,
                 ctx: Ctx = Depends(get_ctx)):
    rows = history.rows(ctx.store, limit=20)
    current = None
    if batch_id:
        current = service.detail(ctx.store, batch_id)
    elif rows:
        current = service.detail(ctx.store, rows[0]["batch_id"])
    if current is not None:
        current["totals_rows"] = history.totals_rows(current["totals"])
    snapshot_rows = []
    if current is not None:
        snapshot_rows = ctx.store.snapshot_rows(current["batch"]["batch_id"])
    return render(request, "batches.html", batch=ctx.current_batch(batch_id),
                  page="batches", rows=rows, current=current,
                  snapshot_rows=snapshot_rows,
                  page_title="批次版本")
