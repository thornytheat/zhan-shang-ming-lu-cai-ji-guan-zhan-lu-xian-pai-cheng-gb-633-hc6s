"""展位解析服务：把名录页的行变成带坐标的展商记录。

一行可能同时命中多个问题，判定顺序固定（见 config.CLASSIFY_ORDER 同族规则）：
列数 → 名称 → 展位号 → 展馆一致性 → 面积 → 同名冲突。前面的判掉就不再往下走，
所以隔离原因只有一个主码，副码进备注，避免同一行被反复记。
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

from ... import config, reasons
from . import contract, geometry, normalize, parse, schema, split

# 展商状态：ok 可排程；unplaced 有记录但没坐标；isolated 隔离；dup 同名冲突
STATUS_OK = "ok"
STATUS_UNPLACED = "unplaced"
STATUS_ISOLATED = "isolated"
STATUS_DUP = "dup"
STATUS_LABELS = {
    STATUS_OK: "可排程", STATUS_UNPLACED: "无坐标",
    STATUS_ISOLATED: "隔离", STATUS_DUP: "同名冲突",
}
# 展位取值来源：whole=v1 展位号整列；split=v2 馆/排/位拆分列
SOURCE_WHOLE = "whole"
SOURCE_SPLIT = "split"
SOURCE_LABELS = {SOURCE_WHOLE: "展位号整列", SOURCE_SPLIT: "馆/排/位拆分列"}


def issue(code: str, *, stage: str = "booth", subject: str = "",
          field_name: str | None = None, raw_value: str | None = None,
          detail: str | None = None, row_ref: str | None = None,
          source_url: str | None = None) -> dict:
    reason = reasons.get(code)
    return {
        "code": code, "stage": reason.stage if stage is None else stage,
        "level": reason.level, "subject": subject, "field": field_name,
        "raw_value": raw_value, "detail": detail or reason.title,
        "source_url": source_url, "row_ref": row_ref,
    }


def build(pages: list) -> tuple[list[dict], list[dict]]:
    exhibitors: list[dict] = []
    issues: list[dict] = []
    seen_names: dict[str, dict] = {}
    seen_booths: dict[str, dict] = {}

    for page in pages:
        if page.kind not in ("list", "attachment"):
            continue
        if not page.header:
            continue
        mapping = contract.map_header(page.header)
        # 以采信时冻结的版本为准；老数据没有冻结值时回落到当前表头检测结果，
        # 保证改版前已冻结的批次续跑仍按当时列义处理原始行。
        version = getattr(page, "contract_version", "") or mapping["version"] \
            or schema.SCHEMA_V1
        page.contract_version = version
        blocked = _page_blocker(mapping, version)
        if blocked is not None:
            issues.append(issue(blocked, subject=page.url,
                                detail=f"{reasons.label(blocked)}（{schema.SCHEMA_REGISTRY[version]['name']}）",
                                source_url=page.url,
                                row_ref=f"{page.source}/p{page.page_no}"))
            continue
        fields = mapping["fields"]
        for index, row in enumerate(page.rows):
            row_ref = f"{page.source}/p{page.page_no}#{index + 2}"
            if schema.is_split(version):
                record, row_issues = _one_split_row(page, row, fields, row_ref)
            else:
                record, row_issues = _one_row(page, row, fields, row_ref)
            issues.extend(row_issues)
            if record is None:
                continue
            record["contract_version"] = version
            _dedupe(record, seen_names, seen_booths, issues)
            exhibitors.append(record)

    for record in exhibitors:
        if record["booth_code"]:
            seen_booths.setdefault(record["booth_code"], record)
    return exhibitors, issues


def _page_blocker(mapping: dict, version: str) -> str | None:
    """按冻结契约版本判定整页是否拒收；新采集缺拆分列直接拒绝。"""
    missing = schema.missing_required(mapping, version)
    if not missing:
        return None
    if "exhibitor_name" in missing:
        return "Q_MISSING_NAME"
    if schema.is_split(version):
        # 改版后名录缺 馆/排/位 拆分列：整页拒收，不解析任何数据行
        return "Q_SCHEMA_MISSING_SPLIT"
    if "booth_raw" in missing:
        return "Q_BOOTH_EMPTY"
    return "Q_SCHEMA_MISSING_SPLIT" if schema.is_split(version) else "Q_BOOTH_EMPTY"


def _one_row(page, row: list[str], fields: dict, row_ref: str) -> tuple:
    issues: list[dict] = []
    width = len(page.header)
    if len(row) != width:
        issues.append(issue(
            "Q_COLUMN_COUNT", subject=page.url, field_name="*",
            raw_value=f"{len(row)} 列", row_ref=row_ref, source_url=page.url,
            detail=f"{reasons.label('Q_COLUMN_COUNT')}：表头 {width} 列，本行 {len(row)} 列"))
        return None, issues

    raw_name = contract.cell(row, fields, "exhibitor_name")
    name = normalize.clean_text(raw_name)
    if normalize.is_sentinel(raw_name):
        issues.append(issue("Q_MISSING_NAME", subject=normalize.clean_text(raw_name) or "（空名称）",
                            field_name="exhibitor_name", raw_value=raw_name,
                            row_ref=row_ref, source_url=page.url))
        return None, issues

    raw_booth = contract.cell(row, fields, "booth_raw")
    parsed = parse.parse_booth(raw_booth)
    if parsed["kind"] == "none":
        code = parsed["code"] or "Q_BOOTH_UNPARSABLE"
        issues.append(issue(code, subject=name, field_name="booth_raw",
                            raw_value=raw_booth, row_ref=row_ref,
                            source_url=page.url))
        return None, issues

    raw_hall = contract.cell(row, fields, "hall_raw")
    hall_from_column = parse.hall_of_raw(raw_hall)
    if parsed["kind"] == "grid" and hall_from_column and hall_from_column != parsed["hall"]:
        issues.append(issue(
            "Q_HALL_MISMATCH", subject=name, field_name="hall_raw",
            raw_value=f"馆列 {raw_hall} / 展位号馆 {parsed['hall']}", row_ref=row_ref,
            source_url=page.url, detail="跨馆写法冲突，以展位号为准但整行隔离"))
        return None, issues

    hall = parsed["hall"]
    area = None
    raw_area = contract.cell(row, fields, "area_sqm")
    if not normalize.is_sentinel(raw_area):
        try:
            area = normalize.parse_decimal(raw_area, field="展位面积")
        except ValueError as exc:
            issues.append(issue("Q_BAD_AREA", subject=name, field_name="area_sqm",
                                raw_value=raw_area, row_ref=row_ref,
                                source_url=page.url, detail=str(exc)))

    priority = 5
    raw_priority = contract.cell(row, fields, "visit_priority")
    note_bits: list[str] = []
    if not normalize.is_sentinel(raw_priority):
        try:
            priority = normalize.parse_int(raw_priority, field="拜访优先级")
        except ValueError:
            note_bits.append("优先级无法解析，按 5 处理")

    status = STATUS_OK
    code = None
    if parsed["kind"] == "area":
        status = STATUS_UNPLACED
        code = "Q_BOOTH_AREA_ONLY"
    position = geometry.position(hall, parsed["row"], parsed["col"])
    x_m = y_m = None
    if status == STATUS_OK:
        if position is None:
            status = STATUS_UNPLACED
            code = "Q_HALL_UNKNOWN"
        else:
            x_m, y_m = position
    if note_bits:
        note_bits.append(contract.cell(row, fields, "note") or "")
    else:
        note_bits.append(contract.cell(row, fields, "note") or "")

    record = {
        "ex_id": ex_id(name, parsed["booth_code"] or raw_booth or name),
        "name": name, "hall": hall,
        "booth_raw": normalize.clean_text(raw_booth),
        "booth_code": parsed["booth_code"],
        "row_no": parsed["row"], "col_no": parsed["col"],
        "x_m": x_m, "y_m": y_m, "area_sqm": area,
        "priority": priority,
        "owner": normalize.clean_text(contract.cell(row, fields, "owner")),
        "status": status, "code": code,
        "source_url": page.url, "row_ref": row_ref,
        "note": " · ".join(bit for bit in note_bits if bit),
        "booth_source": SOURCE_WHOLE,
        "hall_col": "", "row_col": "", "col_col": "",
        "contract_version": getattr(page, "contract_version", "") or schema.SCHEMA_V1,
    }
    return record, issues


def _one_split_row(page, row: list[str], fields: dict, row_ref: str) -> tuple:
    """v2 拆分列路径：馆/排/位 三列独立取值，前导零按字符串保形。"""
    issues: list[dict] = []
    width = len(page.header)
    if len(row) != width:
        issues.append(issue(
            "Q_COLUMN_COUNT", subject=page.url, field_name="*",
            raw_value=f"{len(row)} 列", row_ref=row_ref, source_url=page.url,
            detail=f"{reasons.label('Q_COLUMN_COUNT')}：表头 {width} 列，本行 {len(row)} 列"))
        return None, issues

    raw_name = contract.cell(row, fields, "exhibitor_name")
    name = normalize.clean_text(raw_name)
    if normalize.is_sentinel(raw_name):
        issues.append(issue("Q_MISSING_NAME", subject=normalize.clean_text(raw_name) or "（空名称）",
                            field_name="exhibitor_name", raw_value=raw_name,
                            row_ref=row_ref, source_url=page.url))
        return None, issues

    raw_hall_col = contract.cell(row, fields, "hall_col")
    raw_row_col = contract.cell(row, fields, "row_col")
    raw_col_col = contract.cell(row, fields, "col_col")
    parsed = split.parse_split(raw_hall_col, raw_row_col, raw_col_col)
    if parsed["kind"] == "none":
        code = parsed["code"] or "Q_BOOTH_UNPARSABLE"
        issues.append(issue(
            code, subject=name,
            field_name=" / ".join(schema.SPLIT_FIELDS),
            raw_value=f"馆={raw_hall_col or ''} 排={raw_row_col or ''} 位={raw_col_col or ''}",
            row_ref=row_ref, source_url=page.url))
        return None, issues

    # 整列 booth_raw 在 v2 退为可空的原写法列；与拆分列冲突则整行隔离，
    # 两写不取舍，原值全部保留（Q_SPLIT_CONFLICT）。
    raw_booth = contract.cell(row, fields, "booth_raw")
    if not normalize.is_sentinel(raw_booth):
        if not split.whole_matches_split(raw_booth, parsed["booth_code"]):
            issues.append(issue(
                "Q_SPLIT_CONFLICT", subject=name, field_name="booth_raw",
                raw_value=(f"整列 {normalize.clean_text(raw_booth)} vs 拆分 "
                           f"{parsed['booth_code']}"),
                row_ref=row_ref, source_url=page.url,
                detail=("拆分列与展位号整列指向不同展位，整行隔离；"
                        f"原值：馆={raw_hall_col or ''} 排={raw_row_col or ''} "
                        f"位={raw_col_col or ''}")))
            return None, issues

    # 展馆列 hall_raw 与拆分馆列交叉校验，冲突整行隔离
    raw_hall = contract.cell(row, fields, "hall_raw")
    hall_from_column = parse.hall_of_raw(raw_hall)
    if hall_from_column and hall_from_column != parsed["hall"]:
        issues.append(issue(
            "Q_SPLIT_HALL_MISMATCH", subject=name, field_name="hall_raw",
            raw_value=f"展馆列 {raw_hall} / 拆分馆 {parsed['hall']}", row_ref=row_ref,
            source_url=page.url, detail="展馆列与拆分列馆号冲突，整行隔离"))
        return None, issues

    hall = parsed["hall"]
    area = None
    raw_area = contract.cell(row, fields, "area_sqm")
    if not normalize.is_sentinel(raw_area):
        try:
            area = normalize.parse_decimal(raw_area, field="展位面积")
        except ValueError as exc:
            issues.append(issue("Q_BAD_AREA", subject=name, field_name="area_sqm",
                                raw_value=raw_area, row_ref=row_ref,
                                source_url=page.url, detail=str(exc)))

    priority = 5
    raw_priority = contract.cell(row, fields, "visit_priority")
    note_bits: list[str] = []
    if not normalize.is_sentinel(raw_priority):
        try:
            priority = normalize.parse_int(raw_priority, field="拜访优先级")
        except ValueError:
            note_bits.append("优先级无法解析，按 5 处理")
    note_bits.append(contract.cell(row, fields, "note") or "")
    if not normalize.is_sentinel(raw_booth):
        note_bits.append(f"原展位号写法：{normalize.clean_text(raw_booth)}")

    position = geometry.position(hall, parsed["row"], parsed["col"])
    x_m = y_m = None
    status = STATUS_OK
    code = None
    if position is None:
        status = STATUS_UNPLACED
        code = "Q_HALL_UNKNOWN"
    else:
        x_m, y_m = position

    record = {
        "ex_id": ex_id(name, parsed["booth_code"]),
        "name": name, "hall": hall,
        # booth_raw 留原写法；没有整列时用拆分拼出的规范写法占位，仍可回源
        "booth_raw": (normalize.clean_text(raw_booth)
                      if not normalize.is_sentinel(raw_booth) else parsed["booth_code"]),
        "booth_code": parsed["booth_code"],
        "row_no": parsed["row"], "col_no": parsed["col"],
        "x_m": x_m, "y_m": y_m, "area_sqm": area,
        "priority": priority,
        "owner": normalize.clean_text(contract.cell(row, fields, "owner")),
        "status": status, "code": code,
        "source_url": page.url, "row_ref": row_ref,
        "note": " · ".join(bit for bit in note_bits if bit),
        "booth_source": SOURCE_SPLIT,
        "hall_col": parsed["parts"]["hall_col"],
        "row_col": parsed["parts"]["row_col"],
        "col_col": parsed["parts"]["col_col"],
        "contract_version": schema.SCHEMA_V2,
    }
    return record, issues


def _dedupe(record: dict, seen_names: dict, seen_booths: dict,
            issues: list[dict]) -> None:
    """同名展商冲突与展位号占用：两条都留证据，取首次出现为主（TRAP-14）。"""
    key = normalize.normalize_name(record["name"])
    first = seen_names.get(key)
    if first is not None:
        same_hall = first["hall"] == record["hall"]
        detail = "同名同馆，保留首次出现" if same_hall else "同名不同馆，保留首次出现"
        record["status"] = STATUS_DUP
        record["code"] = "Q_DUP_EXHIBITOR"
        record["note"] = (record["note"] + " · " if record["note"] else "") + detail
        issues.append(issue(
            "Q_DUP_EXHIBITOR", subject=record["name"], field_name="exhibitor_name",
            raw_value=f"{first['booth_code'] or first['booth_raw']} vs "
                      f"{record['booth_code'] or record['booth_raw']}",
            row_ref=record["row_ref"], source_url=record["source_url"], detail=detail))
        return
    seen_names[key] = record
    code = record["booth_code"]
    if not code:
        return
    holder = seen_booths.get(code)
    if holder is not None and normalize.normalize_name(holder["name"]) != key:
        issues.append(issue(
            "Q_DUP_BOOTH", subject=code, field_name="booth_raw",
            raw_value=f"{holder['name']} / {record['name']}",
            row_ref=record["row_ref"], source_url=record["source_url"],
            detail="同一展位号被两家展商占用，疑似合展"))
        return
    seen_booths[code] = record


def ex_id(name: str, marker: str) -> str:
    """展商编号：名称 + 展位的摘要，重跑不变。"""
    digest = hashlib.sha1(f"{normalize.normalize_name(name)}|{marker}".encode("utf-8"))
    return "E" + digest.hexdigest()[:9]


def summary(exhibitors: list[dict], issues: list[dict]) -> dict:
    counts = {}
    for record in exhibitors:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    sources = {}
    for record in exhibitors:
        key = record.get("booth_source") or SOURCE_WHOLE
        sources[key] = sources.get(key, 0) + 1
    return {
        "records": len(exhibitors),
        "ok": counts.get(STATUS_OK, 0),
        "unplaced": counts.get(STATUS_UNPLACED, 0),
        "isolated": counts.get(STATUS_ISOLATED, 0),
        "dup": counts.get(STATUS_DUP, 0),
        "issues": len(issues),
        "halls": len({r["hall"] for r in exhibitors if r["hall"]}),
        "from_whole": sources.get(SOURCE_WHOLE, 0),
        "from_split": sources.get(SOURCE_SPLIT, 0),
    }


def rows_for_page(exhibitors: list[dict]) -> list[dict]:
    rows = []
    for record in exhibitors:
        rows.append({
            "ex_id": record["ex_id"], "name": record["name"],
            "hall": record["hall"], "booth": record["booth_code"] or record["booth_raw"],
            "row_no": record["row_no"] or "—", "col_no": record["col_no"] or "—",
            "x_m": record["x_m"], "y_m": record["y_m"],
            "area": record["area_sqm"], "priority": record["priority"],
            "owner": record["owner"], "status": record["status"],
            "status_label": STATUS_LABELS.get(record["status"], record["status"]),
            "code": record["code"] or "—", "note": record["note"],
            "row_ref": record["row_ref"],
            "booth_source": record.get("booth_source", SOURCE_WHOLE),
            "booth_source_label": SOURCE_LABELS.get(
                record.get("booth_source", SOURCE_WHOLE), "—"),
            "contract_version": record.get("contract_version", "") or "—",
            "hall_col": record.get("hall_col", "") or "—",
            "row_col": record.get("row_col", "") or "—",
            "col_col": record.get("col_col", "") or "—",
            "source_url": record["source_url"],
        })
    return rows


def decimal_or_none(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
