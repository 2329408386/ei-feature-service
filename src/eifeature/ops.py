#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""常用的时间序列算子。参数接受 Series 或 {"t": [...], "v": [...]}。"""

import numpy as np

from src.eifeature.series import Series, as_series


def _skip_if_empty(s: Series) -> None:
    from src.eifeature import Skip

    if len(s) == 0:
        raise Skip("EMPTY_SERIES")


def dedup(s) -> Series:
    """去掉重复时间戳，保留第一个。"""
    s, seen, t, v = as_series(s), set(), [], []
    for ti, vi in zip(s.t, s.v):
        if ti not in seen:
            seen.add(ti)
            t.append(ti)
            v.append(vi)
    return Series(t, v, s.step_ranges)


def resample(s, hz: float) -> Series:
    """线性插值到 hz 赫兹的等间隔时间点。"""
    s = as_series(s)
    if len(s) < 2:
        return s
    t = np.arange(s.t[0], s.t[-1] + 1e-9, 1.0 / hz)
    return Series(t.tolist(), np.interp(t, s.t, s.v).tolist(), s.step_ranges)


def despike(s, window_s: float, sigma: float) -> Series:
    """滑动中位数 + MAD：偏离中位数超过 sigma 倍（MAD 换算的）标准差的点替换为中位数。"""
    s = as_series(s)
    t, v = np.asarray(s.t), np.asarray(s.v, dtype=float)
    out = v.copy()
    for i, ti in enumerate(t):
        win = v[(t >= ti - window_s / 2) & (t <= ti + window_s / 2)]
        med = np.median(win)
        mad = np.median(np.abs(win - med)) * 1.4826  # MAD → 正态分布标准差
        if abs(v[i] - med) > sigma * mad:  # mad 为 0（窗口内其余点全相同）时，任何偏离都算尖峰
            out[i] = med
    return Series(s.t, out.tolist(), s.step_ranges)


def slope(s) -> Series:
    """相邻点的变化率：Δv / Δt，时间取两点中间。"""
    s = as_series(s)
    t, v = np.asarray(s.t), np.asarray(s.v, dtype=float)
    if len(t) < 2:
        return Series([], [], s.step_ranges)
    return Series(((t[1:] + t[:-1]) / 2).tolist(), (np.diff(v) / np.diff(t)).tolist(), s.step_ranges)


def mean(s) -> float:
    """平均值。"""
    s = as_series(s)
    _skip_if_empty(s)
    return float(np.mean(s.v))


def std(s) -> float:
    """总体标准差（ddof=0）。"""
    s = as_series(s)
    _skip_if_empty(s)
    return float(np.std(s.v))
