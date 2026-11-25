#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""时间序列：t 为相对执行开始的秒数（升序），v 为对应的值。不可变，所有操作返回新对象。"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Step:
    id: int
    start_s: float
    end_s: float


@dataclass(frozen=True)
class Series:
    t: list[float]
    v: list[float]
    step_ranges: tuple[Step, ...] = field(default=(), repr=False)  # 所属执行的步骤，供 steps() 使用

    def __len__(self) -> int:
        return len(self.t)

    def _keep(self, keep, shift: float = 0.0) -> "Series":
        pairs = [(t - shift, v) for t, v in zip(self.t, self.v) if keep(t)]
        return Series([p[0] for p in pairs], [p[1] for p in pairs], self.step_ranges)

    def steps(self, *step_ids: int) -> "Series":
        """只保留这些步骤时间段内的点；t 改为相对第一个所选步骤的开始。"""
        chosen = [s for s in self.step_ranges if s.id in step_ids]
        if not chosen:
            return Series([], [], self.step_ranges)
        start = min(s.start_s for s in chosen)
        return self._keep(lambda t: any(s.start_s <= t <= s.end_s for s in chosen), shift=start)

    def window(self, t0: float, t1: float) -> "Series":
        """保留 t0 ≤ t ≤ t1 的点。"""
        return self._keep(lambda t: t0 <= t <= t1)


def as_series(s: "Series | dict") -> Series:
    """ops 的参数可以是 Series，也可以是 {"t": [...], "v": [...]}。"""
    return s if isinstance(s, Series) else Series(list(s["t"]), list(s["v"]))
