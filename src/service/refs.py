#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""算子引用：字符串 feature_id/operator@version，如 gas_flow_diffmean/diffmean@2。"""

import re

from src.model.tables import op_ref
from src.util.errors import invalid

REF = re.compile(r"^([a-z][a-z0-9_]{2,63})/([a-z][a-z0-9_]{2,63})@([1-9][0-9]{0,8})$")

__all__ = ["REF", "op_ref", "parse_ref", "require_ref"]


def parse_ref(ref: str) -> tuple[str, str, int] | None:
    """解析为 (feature_id, operator, version)；格式不对返回 None。"""
    m = REF.match(ref or "")
    return (m.group(1), m.group(2), int(m.group(3))) if m else None


def require_ref(ref: str, path: str = "operator") -> tuple[str, str, int]:
    """解析算子引用，格式不对抛 422。"""
    parsed = parse_ref(ref)
    if parsed is None:
        raise invalid("VALIDATION_ERROR", "算子引用必须形如 feature_id/operator@version",
                      [{"path": path, "message": f"{ref!r} 不是合法的算子引用"}])
    return parsed
