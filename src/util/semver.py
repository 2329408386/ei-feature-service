#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""语义化版本号：只支持 MAJOR.MINOR.PATCH。"""

import re

PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


def parse(version: str) -> tuple[int, int, int]:
    """解析为 (major, minor, patch)，用于比较版本大小。"""
    major, minor, patch = (int(x) for x in version.split("."))
    return major, minor, patch


def is_valid(version: str) -> bool:
    """是否为合法的 MAJOR.MINOR.PATCH。"""
    return bool(PATTERN.match(version))
