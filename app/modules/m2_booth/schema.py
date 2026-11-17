"""列契约版本：把「馆-排-位」拆分前后的两版列义冻结下来。

背景：主办方下月把名录的“展位号”整列拆成“馆、排、位”三列。
- v1（legacy）：必填 exhibitor_name + booth_raw，馆/排/位从展位号整列解析；
- v2（split）：必填 exhibitor_name + hall_col + row_col + col_col，
  展位号整列 booth_raw 退为可空的原写法列。

关键约束（业务要求）：
1. 新采集遇到缺拆分列的名录直接拒绝（整页不解析数据行）；
2. 改版前失败批次的续跑必须沿用当时的列义，所以每个页面在采信时就把
   检测到的契约版本冻结进 Fetched.contract_version，随批次入库，
   之后页面在线怎么改版都不影响该批次；
3. 两版的识别只看表头语义字段（contract.map_header 的结果），不认列位置。
"""
from __future__ import annotations

# 契约版本号是批次证据的一部分，一经写入不再改动；演进只允许新增版本。
SCHEMA_V1 = "v1"
SCHEMA_V2 = "v2"
SCHEMA_VERSIONS = (SCHEMA_V1, SCHEMA_V2)

# v1：改版前的整列契约
V1_NAME = "整列展位号（改版前）"
V1_REQUIRED = ("exhibitor_name", "booth_raw")
# v2：改版后的拆分契约
V2_NAME = "馆/排/位 拆分列（改版后）"
V2_REQUIRED = ("exhibitor_name", "hall_col", "row_col", "col_col")

# 拆分三列的固定顺序，页面与证据按这个顺序展示
SPLIT_FIELDS = ("hall_col", "row_col", "col_col")

SCHEMA_REGISTRY = {
    SCHEMA_V1: {
        "version": SCHEMA_V1,
        "name": V1_NAME,
        "booth_mode": "whole",
        "required": V1_REQUIRED,
        "note": "展位号整列 booth_raw，馆/排/位由展位号语法解析，前导零保形",
    },
    SCHEMA_V2: {
        "version": SCHEMA_V2,
        "name": V2_NAME,
        "booth_mode": "split",
        "required": V2_REQUIRED,
        "note": "馆/排/位为独立拆分列，字符串保形；booth_raw 仅留原写法，两写冲突整行隔离",
    },
}


def detect_version(mapping: dict) -> str:
    """按表头映射结果判定页面契约版本。

    判定只认语义字段：三列拆分字段齐 → v2；否则回到 v1。
    认不出/缺列的页面仍给一个版本（v1），缺列由 service 的 blocker 拦截，
    保证每个页面都有冻结版本，不会出现 None。
    """
    fields = mapping.get("fields", {})
    if all(name in fields for name in SPLIT_FIELDS):
        return SCHEMA_V2
    return SCHEMA_V1


def required_for(version: str) -> tuple[str, ...]:
    return SCHEMA_REGISTRY[version]["required"]


def is_split(version: str | None) -> bool:
    return version == SCHEMA_V2


def missing_required(mapping: dict, version: str) -> list[str]:
    """该版本下缺失的必填语义字段。"""
    fields = mapping.get("fields", {})
    return [name for name in required_for(version) if name not in fields]


def schema_rows() -> list[dict]:
    """给 /api/contract 与页面用的契约版本目录。"""
    return [
        {"version": key, "name": meta["name"], "booth_mode": meta["booth_mode"],
         "required": list(meta["required"]), "note": meta["note"]}
        for key, meta in SCHEMA_REGISTRY.items()
    ]
