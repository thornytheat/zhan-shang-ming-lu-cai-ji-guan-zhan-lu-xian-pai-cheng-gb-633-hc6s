"""采集原文快照：按批次冻结在线页面的原始字节与列契约。

为什么需要它：
- 改版前失败的批次续跑时，在线名录可能已经换成 v2 拆分列。续跑必须按
  当时列义处理**原始行**，不能因为页面改版而换展商；
- 所以采信成功后立刻把每页原始字节、表头、契约版本写进 page_snapshots，
  续跑优先读快照，根本不重新发起抓取。

SnapshotFetcher 包装任意 fetcher：
- 构造时传入该批次已冻结的快照（url → Snapshot）；
- get(path) 命中快照就还原 Fetched（from_snapshot=True），未命中才透传给
  底层 fetcher，保证「从已采页面恢复，重试不重复入库」。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from . import decode, reader
from ..m2_booth import contract


@dataclass
class Snapshot:
    """一页采集原文的冻结记录。raw 是解码前的原始字节。"""

    url: str
    source: str
    kind: str
    page_no: int
    raw: bytes
    sha256: str
    declared_charset: str | None
    encoding: str
    header: list[str]
    contract_version: str
    status: str

    def to_fetched(self) -> reader.Fetched:
        """从冻结字节完整还原采信当时的 Fetched（含解码文本与表格行）。"""
        fetched = reader.Fetched(
            url=self.url, status=self.status,
            http_status=200 if self.status != "missing" else 404,
            raw=self.raw, sha256=self.sha256, source=self.source,
            kind=self.kind, page_no=self.page_no,
            declared_charset=self.declared_charset, from_snapshot=True,
            contract_version=self.contract_version)
        if self.status == "missing" or not self.raw:
            fetched.note = "快照：采集时页面缺失"
            fetched.codes = ["S_PAGE_MISSING"]
            return fetched
        info = decode.decode(self.raw, self.declared_charset)
        fetched.text = info["text"]
        fetched.encoding = info["encoding"]
        fetched.codes = list(info["codes"])
        if self.kind == "attachment":
            from . import paging

            fetched.header, fetched.rows, truncated = paging.parse_csv(fetched.text)
            fetched.table_closed = not truncated
            if truncated:
                fetched.codes.append("S_PAGE_TRUNCATED")
            if decode.has_bom(self.raw) or (
                    fetched.header and fetched.header[0].startswith("﻿")):
                fetched.codes.append("S_BOM_STRIPPED")
        else:
            reader.parse_html(fetched)
            if not fetched.rows and not fetched.header:
                fetched.codes.append("S_NO_TABLE")
            if not fetched.table_closed:
                fetched.status = "truncated"
                fetched.codes.append("S_PAGE_TRUNCATED")
            if fetched.dup_headers:
                fetched.codes.append("S_DUP_HEADER")
        # 以冻结的契约版本为准，重新检测只作交叉核对；缺省回落 v1
        from ..m2_booth import schema

        if not fetched.contract_version:
            fetched.contract_version = (
                contract.map_header(fetched.header).get("version")
                or schema.SCHEMA_V1)
        return fetched


class SnapshotFetcher:
    """对底层 fetcher 加一层快照只读缓存；缺页才透传。"""

    kind = "snapshot"

    def __init__(self, inner, snapshots: dict[str, Snapshot] | None = None):
        self.inner = inner
        self.snapshots = dict(snapshots or {})
        self.restored: list[str] = []

    def get(self, path: str) -> reader.Fetched:
        url = self._url_for(path)
        snapshot = self.snapshots.get(url)
        if snapshot is None:
            return self.inner.get(path)
        self.restored.append(url)
        return snapshot.to_fetched()

    def _url_for(self, path: str) -> str:
        base = getattr(self.inner, "base", "http://sample-site").rstrip("/")
        return base + "/" + path.lstrip("/")


def snapshot_from_page(page: reader.Fetched) -> Snapshot:
    """采信后把一页冻结成快照；契约版本按当时表头检测，缺省回落 v1。"""
    from ..m2_booth import schema

    version = page.contract_version
    if not version and page.header:
        version = contract.map_header(page.header).get("version", "")
    digest = page.sha256 or hashlib.sha256(page.raw).hexdigest()
    return Snapshot(
        url=page.url, source=page.source, kind=page.kind, page_no=page.page_no,
        raw=bytes(page.raw), sha256=digest,
        declared_charset=page.declared_charset, encoding=page.encoding,
        header=list(page.header), contract_version=version or schema.SCHEMA_V1,
        status=page.status)
