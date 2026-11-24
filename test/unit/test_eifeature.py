import pytest

from src.eifeature import Series, Skip, Step, ops
from src.util import semver


def test_steps_and_window():
    s = Series([0, 10, 20, 30, 40], [1, 2, 3, 4, 5], (Step(1, 0, 15), Step(2, 15, 35)))
    assert s.steps(2).t == [5, 15] and s.steps(2).v == [3, 4]  # t 从所选步骤开始重新计
    assert s.steps(9).t == []
    assert s.window(10, 30).v == [2, 3, 4]


def test_ops():
    d = {"t": [0, 1, 1, 2, 3, 4], "v": [0, 1, 9, 2, 3, 4]}
    s = ops.dedup(d)
    assert s.t == [0, 1, 2, 3, 4] and s.v == [0, 1, 2, 3, 4]
    assert ops.resample(s, 2).t == [0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4]
    assert ops.mean(s) == 2 and ops.std(s) == pytest.approx(2 ** 0.5)
    assert ops.slope(s).v == [1, 1, 1, 1]
    spiky = ops.despike({"t": list(range(9)), "v": [1, 1, 1, 1, 50, 1, 1, 1, 1]}, window_s=4, sigma=3)
    assert spiky.v[4] == 1


def test_empty_series_skips():
    with pytest.raises(Skip) as e:
        ops.mean({"t": [], "v": []})
    assert e.value.reason == "EMPTY_SERIES"


def test_semver():
    assert semver.parse("1.10.0") > semver.parse("1.9.9")
    assert not semver.is_valid("1.0")
