"""列契约：列名 → 语义字段的映射、必填校验、演进别名。

门户经常改列名（"展商" → "企业名称"）、调列序、增列；契约只认语义字段，
不认列位置，所以列序变了不影响结果。认不出的列登记但不阻断。

改版（boothplan/1.3.0）后展位号拆成 馆/排/位 三列，契约版本的识别与冻结
在 schema.py；本文件只负责表头 → 语义字段的映射，保持与版本无关。
"""
from __future__ import annotations

from ... import config
from . import schema
from .normalize import clean_text, to_halfwidth

# v1 必填集合保留导出，实际必填按 schema.required_for(version) 判定
REQUIRED = schema.V1_REQUIRED


def canonical_key(header_cell: str | None) -> str | None:
    """列名归一：去 BOM、全角转半角、去空白与括号内单位。"""
    text = clean_text(header_cell).lstrip("\ufeff")
    if not text:
        return None
    if text in config.COLUMN_ALIASES:
        return config.COLUMN_ALIASES[text]
    bare = to_halfwidth(text).replace(" ", "")
    if bare in config.COLUMN_ALIASES:
        return config.COLUMN_ALIASES[bare]
    for alias, field_name in config.COLUMN_ALIASES.items():
        if bare.startswith(to_halfwidth(alias).replace(" ", "")):
            return field_name
    return None


def map_header(header: list[str] | None) -> dict:
    """返回 {fields, unmapped, duplicated, missing, bom_removed, width, version}。

    missing 仍按 v1 必填给出（兼容旧调用）；版本化必填请用
    schema.missing_required(mapping, mapping["version"])。
    """
    header = list(header or [])
    fields: dict[str, int] = {}
    unmapped: list[str] = []
    duplicated: list[str] = []
    bom_removed = bool(header) and header[0].startswith("\ufeff")
    for index, cell in enumerate(header):
        key = canonical_key(cell)
        if key is None:
            unmapped.append(clean_text(cell) or f"列{index + 1}")
            continue
        if key in fields:
            duplicated.append(key)
            continue
        fields[key] = index
    version = schema.detect_version({"fields": fields})
    missing = schema.missing_required({"fields": fields}, schema.SCHEMA_V1)
    return {
        "fields": fields, "unmapped": unmapped, "duplicated": duplicated,
        "missing": missing, "bom_removed": bom_removed,
        "width": len(header), "version": version,
    }


def cell(row: list[str], fields: dict, name: str) -> str | None:
    index = fields.get(name)
    if index is None or index >= len(row):
        return None
    return row[index]


def column_contract(version: str | None = None) -> list[dict]:
    """列契约目录；version 给定时标注该版本是否必填，否则 required 一律 False。"""
    required = schema.required_for(version) if version else ()
    return [
        {"field": name, "label": label, "type": kind, "rule": rule,
         "required": name in required}
        for name, label, kind, rule in config.COLUMNS
    ]


def evolution_table() -> list[dict]:
    """旧列名 → 语义字段，用于门户改版时对照。"""
    rows = []
    for alias, field_name in config.COLUMN_ALIASES.items():
        if alias == field_name:
            continue
        rows.append({"alias": alias, "field": field_name,
                     "matched": field_name in [c[0] for c in config.COLUMNS]})
    return sorted(rows, key=lambda r: (r["field"], r["alias"]))
