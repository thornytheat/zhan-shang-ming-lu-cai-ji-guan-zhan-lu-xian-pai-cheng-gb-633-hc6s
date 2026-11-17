"""名录改版（展位号拆 馆/排/位）兼容历史数据的测试。

覆盖业务要求与 DEPTH 登记的陷阱：
- TRAP-31 列契约 v1/v2 随批次冻结，续跑不随在线改版换展商；
- TRAP-32 新采集缺拆分列整页拒收（Q_SCHEMA_MISSING_SPLIT）；
- TRAP-33 拆列与整列冲突整行隔离并保留来源（Q_SPLIT_CONFLICT）；
- TRAP-34 拆分列排/位字符串保形，前导零保留；
- TRAP-35 失败后从冻结快照恢复，重试不重抓、不重复入库。

另覆盖：页面/API 可查看采用的列义、展商来源与隔离原因；
已有成功批次的排程不变。
"""
from __future__ import annotations

import datetime as dt
import pathlib

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import create_app
from app.store import Store
from app.modules.m1_catalog import reader, service as catalog_service
from app.modules.m1_catalog.snapshot import SnapshotFetcher, snapshot_from_page
from app.modules.m2_booth import contract, schema, service as booth_service
from app.modules.m2_booth import split as split_mod
from app.modules.m5_batches import history, service as batch_service

ROOT = pathlib.Path(__file__).resolve().parent.parent
V2_SITE = ROOT / "tests" / "fixtures" / "v2_site"
EVAL_DATE = dt.date(2026, 11, 15)


def make_settings(tmp_path, site_dir):
    return config.Settings(db_path=str(tmp_path / "boothplan.sqlite"),
                           eval_date=EVAL_DATE,
                           sample_site_dir=str(site_dir),
                           sample_site_base="http://sample-site")


def page_with(header, rows, *, url="http://sample-site/dir/x", version_header=None):
    page = reader.Fetched(url=url, status="ok", http_status=200, raw=b"",
                          sha256="0" * 64, source="dir", kind="list", page_no=1)
    page.header = header
    page.rows = rows
    # 与采信流水线一致：按当时表头冻结契约版本（测试可用 version_header 覆盖）
    page.contract_version = version_header if version_header is not None \
        else contract.map_header(header).get("version", "")
    return page


# ---------- 1. 契约版本检测 ----------
def test_detect_versions_from_header():
    v1 = contract.map_header(["展商名称", "展位号", "展馆"])
    assert v1["version"] == schema.SCHEMA_V1
    assert schema.missing_required(v1, schema.SCHEMA_V1) == []

    v2 = contract.map_header(["展商名称", "馆", "排", "位"])
    assert v2["version"] == schema.SCHEMA_V2
    assert schema.missing_required(v2, schema.SCHEMA_V2) == []


def test_legacy_alias_馆号_still_maps_to_hall_raw_not_split():
    # 改版前表头里的“馆号”是 hall_raw，不能被 v2 拆分列抢走
    mapping = contract.map_header(["企业名称", "展位编号", "馆号", "备注"])
    assert mapping["fields"]["hall_raw"] == 2
    assert "hall_col" not in mapping["fields"]
    assert mapping["version"] == schema.SCHEMA_V1


def test_new_crawl_rejects_catalog_missing_split_columns(settings):
    """新采集：v2 名录缺拆分列 → 整页拒收，不产出任何展商。"""
    # 页面声明走 v2（已有 馆/排），但缺必填的 位 列
    page = page_with(["展商名称", "馆", "排"],
                     [["甲电子", "A1", "03"]], version_header=schema.SCHEMA_V2)
    assert page.contract_version == schema.SCHEMA_V2
    exhibitors, issues = booth_service.build([page])
    codes = {item["code"] for item in issues}
    assert "Q_SCHEMA_MISSING_SPLIT" in codes
    assert exhibitors == []


def test_v1_legacy_page_without_booth_column_still_uses_v1_rule(settings):
    page = page_with(["展商名称", "展馆"], [["甲电子", "A1"]])
    assert page.contract_version == schema.SCHEMA_V1
    exhibitors, issues = booth_service.build([page])
    assert exhibitors == []
    assert any(i["code"] == "Q_BOOTH_EMPTY" for i in issues)


# ---------- 2. 拆分列解析与前导零 ----------
def test_split_parse_keeps_leading_zeros():
    parsed = split_mod.parse_split("a1", "3", "05")
    assert parsed["kind"] == "grid"
    assert parsed["hall"] == "A1"
    assert parsed["row"] == "03"          # 补零但仍是字符串
    assert parsed["col"] == "05"          # 前导零保留
    assert parsed["booth_code"] == "A1-03-05"
    assert isinstance(parsed["row"], str) and isinstance(parsed["col"], str)


def test_split_parse_fullwidth_and_lowercase():
    parsed = split_mod.parse_split("Ｃ２", "07", "9")
    assert parsed["hall"] == "C2"
    assert parsed["row"] == "07"
    assert parsed["col"] == "09"


@pytest.mark.parametrize("hall,row,col,code", [
    ("A1", "-", "05", "Q_SPLIT_EMPTY_PART"),
    ("Z9", "1", "1", "Q_SPLIT_BAD_HALL"),
    ("A1", "3", "X5", "Q_SPLIT_BAD_PART"),
    ("A1", "", "1", "Q_SPLIT_EMPTY_PART"),
])
def test_split_parse_bad_parts(hall, row, col, code):
    parsed = split_mod.parse_split(hall, row, col)
    assert parsed["kind"] == "none"
    assert parsed["code"] == code


def test_whole_matches_split():
    assert split_mod.whole_matches_split("A1-3-05", "A1-03-05") is True
    assert split_mod.whole_matches_split("A1-09-99", "A1-03-06") is False
    # 整列解析不出来时不在这里报冲突（交给 v1 原因码）
    assert split_mod.whole_matches_split("3x3", "A1-03-05") is True
    assert split_mod.whole_matches_split("-", "A1-03-05") is True


def test_v2_build_from_fixture_site(tmp_path):
    settings = make_settings(tmp_path, V2_SITE)
    fetcher = catalog_service.make_fetcher(settings)
    bundle = catalog_service.ingest(settings, source="dir", fetcher=fetcher)
    page = bundle.pages[0]
    assert page.contract_version == schema.SCHEMA_V2
    exhibitors, issues = booth_service.build(bundle.pages)
    by_name = {r["name"]: r for r in exhibitors}
    codes = [i["code"] for i in issues]

    # 正常拆分 + 前导零
    assert by_name["北方数控"]["booth_code"] == "A1-03-05"
    assert by_name["北方数控"]["row_no"] == "03"
    assert by_name["北方数控"]["col_no"] == "05"
    assert by_name["北方数控"]["booth_source"] == booth_service.SOURCE_SPLIT
    assert by_name["北方数控"]["contract_version"] == schema.SCHEMA_V2
    # 无整列时 booth_raw 用规范拼写法占位
    assert by_name["东方液压"]["booth_raw"] == "B2-01-12"
    # 小写馆 + 单位补零
    assert by_name["西林仪表"]["hall"] == "C2"
    assert by_name["西林仪表"]["col_no"] == "09"

    # 拆列与整列冲突 → 整行隔离，原值保留
    assert "冲突机械" not in by_name
    conflict = [i for i in issues if i["code"] == "Q_SPLIT_CONFLICT"]
    assert len(conflict) == 1
    assert "A1-09-99" in conflict[0]["raw_value"]
    assert "A1-03-06" in conflict[0]["raw_value"]

    # 空哨兵 / 非数字 / 展馆列不一致
    assert "Q_SPLIT_EMPTY_PART" in codes
    assert "Q_SPLIT_BAD_PART" in codes
    mismatch = [i for i in issues if i["code"] == "Q_SPLIT_HALL_MISMATCH"]
    assert len(mismatch) == 1 and "跨馆电气" in mismatch[0]["subject"]

    # 隔离行不进展商表，但来源行号保留
    assert all(r["row_ref"].startswith("dir/p1#") for r in exhibitors)


# ---------- 3. 冻结契约：续跑不随在线改版换展商 ----------
def test_frozen_contract_version_overrides_live_header(settings):
    """页面在线改成 v2 表头，但批次冻结为 v1：续跑仍按 v1 处理原始行。"""
    header_v2 = ["展商名称", "馆", "排", "位"]
    rows = [["甲电子", "A1", "03", "05"]]
    page = page_with(header_v2, rows, version_header=schema.SCHEMA_V1)
    # v1 必填 booth_raw 缺失 → 按 v1 规则报 Q_BOOTH_EMPTY，而不是 v2 成功
    exhibitors, issues = booth_service.build([page])
    assert exhibitors == []
    assert any(i["code"] == "Q_BOOTH_EMPTY" for i in issues)
    assert not any(i["code"] == "Q_SCHEMA_MISSING_SPLIT" for i in issues)


def test_snapshot_roundtrip_preserves_raw_and_version(settings, fetcher):
    bundle = catalog_service.ingest(settings, source="dir", fetcher=fetcher)
    original = bundle.pages[0]
    snap = snapshot_from_page(original)
    assert snap.contract_version == schema.SCHEMA_V1
    restored = snap.to_fetched()
    assert restored.from_snapshot is True
    assert restored.raw == original.raw
    assert restored.sha256 == original.sha256
    assert restored.header == original.header
    assert restored.rows == original.rows
    assert restored.contract_version == schema.SCHEMA_V1
    assert restored.encoding == original.encoding


def test_snapshot_fetcher_does_not_refetch(settings, fetcher):
    bundle = catalog_service.ingest(settings, source="dir", fetcher=fetcher)
    snapshots = {p.url: snapshot_from_page(p) for p in bundle.pages}

    class BoomFetcher:
        base = "http://sample-site"
        def get(self, path):  # pragma: no cover - 快照命中时不应被调用
            raise AssertionError("续跑命中快照，不允许重新抓取在线页面")

    snap_fetcher = SnapshotFetcher(BoomFetcher(), snapshots)
    again = catalog_service.ingest(settings, source="dir", fetcher=snap_fetcher)
    assert len(again.pages) == len(bundle.pages)
    assert all(p.from_snapshot for p in again.pages)
    assert all(p.sha256 in {s.sha256 for s in snapshots.values()} for p in again.pages)
    # 快照恢复记一条 info 原因码
    assert any("S_SNAPSHOT_RESUME" in p.codes for p in again.pages)


def test_resume_after_failure_keeps_original_exhibitors(tmp_path):
    """失败批次续跑：在线目录已换成 v2，仍按冻结的 v1 原文得到同一批展商。"""
    legacy_dir = tmp_path / "legacy"
    (legacy_dir / "dir").mkdir(parents=True)
    legacy_html = (ROOT / "sample_site" / "dir" / "list_p1.html").read_bytes()
    (legacy_dir / "dir" / "list_p1.html").write_bytes(legacy_html)

    settings = make_settings(tmp_path, legacy_dir)
    store = Store(settings.db_path)
    first = batch_service.run(settings=settings, store=store, source="dir")
    assert first["status"] == "done"
    bid = first["batch"]["batch_id"]
    exhibitor_ids = {e["ex_id"] for e in store.load_exhibitors(bid)}

    # 在线目录“改版”成 v2 拆分列
    settings2 = make_settings(tmp_path, V2_SITE)
    store2 = Store(settings2.db_path)  # 同一个库文件
    batch = store2.get_batch(bid)
    assert batch is not None
    # 手工把批次置为失败，模拟 catalog 之后阶段中断
    batch["status"] = "failed"
    store2.upsert_batch(batch)

    result = batch_service.resume_batch(settings=settings2, store=store2, batch_id=bid)
    assert result["status"] == "done"
    new_bid = result["batch"]["batch_id"]
    # 续跑后的展商与冻结原文一致，没有被在线 v2 页面替换
    resumed_ids = {e["ex_id"] for e in store2.load_exhibitors(new_bid)}
    assert exhibitor_ids <= resumed_ids
    # 从快照恢复的名录页带冻结标记，且契约版本仍是 v1
    pages = [p for p in store2.load_pages(new_bid) if p["source"] == "dir"]
    assert pages and all(p.get("from_snapshot") for p in pages)
    assert all(p.get("contract_version") == schema.SCHEMA_V1 for p in pages)
    # 原失败批次不被覆盖；首跑冻结的名录页在新批次里仍是同一份原文（同 sha256）
    assert store2.get_batch(bid)["status"] == "failed"
    old_snaps = {s["url"]: s["sha256"] for s in store2.snapshot_rows(bid)}
    new_snaps = {s["url"]: s["sha256"] for s in store2.snapshot_rows(new_bid)}
    for url, digest in old_snaps.items():
        assert new_snaps.get(url) == digest
    # 续跑没有给原失败批次追加任何快照行（重试不重复入库）
    assert len(store2.snapshot_rows(bid)) == len(old_snaps)


# ---------- 4. 重试不重复入库 ----------
def test_retry_does_not_duplicate_snapshots_or_rows(settings, store, fetcher):
    first = batch_service.run(settings=settings, store=store, source="all",
                              fetcher=fetcher)
    bid = first["batch"]["batch_id"]
    snap_count_first = len(store.snapshot_rows(bid))
    assert snap_count_first > 0
    rows_first = store.count_rows(bid)

    # 再跑一次（同输入命中幂等），快照与各表行数不变
    second = batch_service.run(settings=settings, store=store, source="all",
                               fetcher=fetcher)
    assert second["idempotent_replay"] is True
    assert len(store.snapshot_rows(bid)) == snap_count_first
    assert store.count_rows(bid) == rows_first


# ---------- 5. 页面 / API 可查列义、来源、隔离原因 ----------
def test_contract_api_exposes_schema_versions(tmp_path):
    settings = make_settings(tmp_path, V2_SITE)
    store = Store(settings.db_path)
    client = TestClient(create_app(settings))
    body = client.get("/api/contract").json()
    versions = {row["version"] for row in body["schema_versions"]}
    assert {"v1", "v2"} <= versions
    v2_required = set(body["required_v2"])
    assert {"exhibitor_name", "hall_col", "row_col", "col_col"} <= v2_required
    # 原因码目录登记了新码
    reason_codes = {r["code"] for r in body["reasons"]}
    assert {"Q_SCHEMA_MISSING_SPLIT", "Q_SPLIT_CONFLICT",
            "Q_SPLIT_EMPTY_PART", "Q_SPLIT_BAD_PART",
            "Q_SPLIT_BAD_HALL", "Q_SPLIT_HALL_MISMATCH"} <= reason_codes


def test_exhibitors_page_shows_contract_and_source(tmp_path):
    settings = make_settings(tmp_path, V2_SITE)
    store = Store(settings.db_path)
    batch_service.run(settings=settings, store=store, source="dir")
    client = TestClient(create_app(settings))
    text = client.get("/exhibitors").text
    assert "本批次冻结的列契约" in text
    assert "v2" in text
    assert "馆/排/位拆分列" in text
    # 隔离原因在页面可见
    assert "Q_SPLIT_CONFLICT" in text


def test_index_page_shows_frozen_contract_column(tmp_path):
    settings = make_settings(tmp_path, V2_SITE)
    store = Store(settings.db_path)
    batch_service.run(settings=settings, store=store, source="dir")
    client = TestClient(create_app(settings))
    text = client.get("/").text
    assert "列契约" in text
    assert "v2" in text


# ---------- 6. 旧库迁移不破坏既有成功批次 ----------
def test_legacy_db_migrates_and_keeps_schedule(tmp_path):
    """用 1.2 风格的旧库（无新列）打开：迁移补列，旧展商与排程不变。"""
    import sqlite3

    legacy_db = tmp_path / "legacy.sqlite"
    # 先按当前 schema 建库跑一批，再模拟旧库：删掉新列相关表/列做不到，
    # 改为直接验证迁移对“已存在新列”的库幂等且旧数据可读。
    settings = config.Settings(
        db_path=str(legacy_db), eval_date=EVAL_DATE,
        sample_site_dir=str(ROOT / "sample_site"),
        sample_site_base="http://sample-site")
    store = Store(settings.db_path)
    result = batch_service.run(settings=settings, store=store, source="all")
    bid = result["batch"]["batch_id"]
    exhibitors_before = store.load_exhibitors(bid)
    stops_before = store.count_rows(bid)["stops"]

    # 再次打开触发迁移路径；新列已存在时必须跳过，数据不变
    store_again = Store(settings.db_path)
    exhibitors_after = store_again.load_exhibitors(bid)
    assert [e["ex_id"] for e in exhibitors_after] == \
        [e["ex_id"] for e in exhibitors_before]
    assert store_again.count_rows(bid)["stops"] == stops_before
    # 历史批次没有拆分来源时回落到 whole / v1 展示默认值
    assert all(e.get("booth_source", "whole") in ("whole", "") for e in exhibitors_after)

    # 真旧库（缺新列）能被迁移补齐
    conn = sqlite3.connect(str(legacy_db))
    conn.execute("ALTER TABLE exhibitors RENAME TO exhibitors_old")
    conn.execute(
        "CREATE TABLE exhibitors AS SELECT * FROM exhibitors_old")
    conn.execute("ALTER TABLE exhibitors DROP COLUMN hall_col")
    conn.execute("ALTER TABLE exhibitors DROP COLUMN row_col")
    conn.execute("ALTER TABLE exhibitors DROP COLUMN col_col")
    conn.execute("ALTER TABLE exhibitors DROP COLUMN booth_source")
    conn.execute("ALTER TABLE exhibitors DROP COLUMN contract_version")
    conn.execute("DROP TABLE exhibitors_old")
    conn.commit()
    conn.close()
    Store(settings.db_path)  # 不抛异常即迁移成功
    assert len(Store(settings.db_path).load_exhibitors(bid)) == len(exhibitors_before)
