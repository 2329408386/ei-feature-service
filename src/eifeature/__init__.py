#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""算子运行库。算子代码这样使用：

    from eifeature import gde, ops, Skip

    def fetch(run_id: str) -> dict: ...      # 只在这里通过 gde 取数
    def compute(data: dict) -> float: ...    # 纯计算，数据不合格时 raise Skip("原因码")
"""

import re

from src.eifeature import gde, ops
from src.eifeature.series import Series, Step

REASON = re.compile(r"^[A-Z][A-Z0-9_]{2,31}$")


class Skip(Exception):
    """数据不能算：结果记为空值并带上原因码（大写字母、数字、下划线，3~32 位）。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


__all__ = ["Series", "Skip", "Step", "gde", "ops"]
