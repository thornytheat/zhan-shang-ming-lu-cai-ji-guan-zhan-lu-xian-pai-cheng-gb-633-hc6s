"""v2 拆分列解析：馆 / 排 / 位 三个独立列 → 馆-排-位。

与 v1 的 parse.parse_booth（整列展位号语法）并列，不互相调用：
- 排/位是编码型字段，**一律字符串保形，前导零保留**（如 "03" 不变 3）；
- 馆列复用 parse._hall_of 的别名归一（单字母馆归 1 号子馆）；
- 排/位只接受 1–3 位数字，非数字判 Q_SPLIT_BAD_PART，不猜；
- 整列 booth_raw 若同时存在且与拆分三列拼出的展位不一致，判
  Q_SPLIT_CONFLICT，由 service 隔离整行并保留全部原值。
"""
from __future__ import annotations

import re

from ... import config
from . import parse
from .normalize import clean_text, is_sentinel, keep_leading_zero, to_halfwidth

DIGITS_RE = re.compile(r"^\d{1,3}$")


def parse_split(hall_value: str | None, row_value: str | None,
                col_value: str | None) -> dict:
    """拆分三列 → {kind, hall, row, col, booth_code, code, value, parts}。

    kind: grid（成功）/ none（有部件不合规）。parts 保留三列清洗后的原值，
    供冲突证据与页面回源展示。
    """
    parts = {
        "hall_col": clean_text(hall_value),
        "row_col": clean_text(row_value),
        "col_col": clean_text(col_value),
    }
    result = {"kind": "none", "hall": "", "row": "", "col": "",
              "booth_code": "", "code": None, "value": "", "parts": parts}

    if any(is_sentinel(value) for value in (hall_value, row_value, col_value)):
        result["code"] = "Q_SPLIT_EMPTY_PART"
        return result

    hall_token = to_halfwidth(parts["hall_col"]).upper().replace(" ", "")
    hall = parse._hall_of(hall_token)
    if hall is None:
        result["code"] = "Q_SPLIT_BAD_HALL"
        return result

    row_text = parts["row_col"]
    col_text = parts["col_col"]
    if not DIGITS_RE.match(to_halfwidth(row_text).replace(" ", "")):
        result["code"] = "Q_SPLIT_BAD_PART"
        return result
    if not DIGITS_RE.match(to_halfwidth(col_text).replace(" ", "")):
        result["code"] = "Q_SPLIT_BAD_PART"
        return result

    # 编码型字段：补零但绝不转 int，"03" 仍是 "03"，"12" 仍是 "12"
    row_no = keep_leading_zero(row_text, width=2)
    col_no = keep_leading_zero(col_text, width=2)
    result.update(kind="grid", hall=hall, row=row_no, col=col_no)
    result["booth_code"] = parse.booth_code(hall, row_no, col_no)
    return result


def whole_matches_split(booth_raw: str | None, split_code: str) -> bool:
    """整列展位号与拆分列归一结果是否指向同一展位。

    整列按 v1 语法解析；解析不出来（面积型/哨兵/无法解析）不算冲突，
    交给 v1 自身的原因码处理，避免一行挂两个主码。
    """
    parsed = parse.parse_booth(booth_raw)
    if parsed["kind"] != "grid":
        return True
    return parsed["booth_code"] == split_code


def hall_alias(token: str) -> str | None:
    return parse._hall_of(to_halfwidth(token).upper().replace(" ", ""))
