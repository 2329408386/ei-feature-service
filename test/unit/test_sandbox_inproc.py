"""沙箱子进程函数：直接在当前进程调用 _fetch / _compute / _execute，覆盖各分支和耗时统计。"""

from types import SimpleNamespace

import pytest

from src.eifeature import Skip
from src.eifeature import gde as eif_gde
from src.engine import sandbox as sb
from src.engine.sandbox import DETAIL_MAX, RunResult
from src.util.config import settings
from src.util.gde import GdeUnavailable

GDE_JSON = settings.gde.model_dump_json()


def module(fetch=None, compute=None):
    return SimpleNamespace(fetch=fetch, compute=compute)


def raiser(exc):
    def f(*_):
        raise exc

    return f


def busy(*_):
    while True:
        pass


# ---------------------------------------------------------------- 小工具
def test_skip_valid_and_invalid_reason():
    r = RunResult()
    sb._skip(r, Skip("LOW_COVERAGE"), "COMPUTE_ERROR")
    assert (r.reason, r.reason_detail) == ("LOW_COVERAGE", None)

    for bad in ("bad reason", "AB", "x" * 40, 123):
        r = RunResult()
        sb._skip(r, Skip(bad), "FETCH_ERROR")
        assert r.reason == "FETCH_ERROR" and r.reason_detail == f"非法原因码：{bad!r}"


def test_summary():
    series = SimpleNamespace(t=[3, 1, 2], v=[0, 0, 0])
    data = {"a": {"t": [5, 6], "v": [1, 2]}, "b": series, "c": {"t": []}, "d": 1, "e": {"v": [1]}}
    assert sb._summary(data) == {
        "a": {"points": 2, "t_min": 5, "t_max": 6},
        "b": {"points": 3, "t_min": 1, "t_max": 3},
        "c": {"points": 0, "t_min": None, "t_max": None},
    }
    assert sb._summary([1, 2]) == {} and sb._summary(None) == {}


def test_detail_truncated():
    assert sb._detail(ValueError("boom")) == "ValueError: boom"
    assert len(sb._detail(ValueError("x" * 5000))) == DETAIL_MAX


# ---------------------------------------------------------------- fetch
def test_fetch_ok_and_gde_bound_only_during_fetch():
    seen = {}

    def fetch(run_id):
        seen["client"] = eif_gde._client
        return {"run": eif_gde.run_info(run_id).chamber}

    r = RunResult()
    assert sb._fetch(r, module(fetch), "RUN-CH-A01-202611240815", GDE_JSON, 5) == {"run": "CH-A01"}
    assert r.reason is None and r.fetch_ms >= 0
    assert seen["client"] is not None and eif_gde._client is None


@pytest.mark.parametrize(
    ("fetch", "run_id", "reason", "detail"),
    [
        (raiser(Skip("NO_DATA")), "RUN-0338", "NO_DATA", None),
        (raiser(Skip("bad")), "RUN-0338", "FETCH_ERROR", "非法原因码：'bad'"),
        (raiser(ValueError("boom")), "RUN-0338", "FETCH_ERROR", "ValueError: boom"),
        (lambda run_id: eif_gde.run_info(run_id), "RUN-MISSING-1", "MISSING_DATA", ...),
        (busy, "RUN-0338", "FETCH_TIMEOUT", None),
    ],
)
def test_fetch_failures(fetch, run_id, reason, detail):
    r = RunResult()
    assert sb._fetch(r, module(fetch), run_id, GDE_JSON, 0.2) is None
    assert r.reason == reason
    if detail is not ...:
        assert r.reason_detail == detail
    else:
        assert r.reason_detail
    assert eif_gde._client is None  # 无论成败都解绑


def test_fetch_gde_unavailable_propagates_and_unbinds():
    r = RunResult()
    with pytest.raises(GdeUnavailable):
        sb._fetch(r, module(raiser(GdeUnavailable("down"))), "RUN-0338", GDE_JSON, 5)
    assert r.reason is None and eif_gde._client is None and r.fetch_ms >= 0


# ---------------------------------------------------------------- compute
@pytest.mark.parametrize(("ret", "expected"), [(1, 1.0), (2.5, 2.5), (-0.0, 0.0)])
def test_compute_ok(ret, expected):
    r = RunResult()
    sb._compute(r, module(compute=lambda d: ret), {}, 5)
    assert r.reason is None and r.value == expected and isinstance(r.value, float) and r.compute_ms >= 0


@pytest.mark.parametrize("ret", [True, None, "1", float("nan"), float("inf"), [1]])
def test_compute_non_finite(ret):
    r = RunResult()
    sb._compute(r, module(compute=lambda d: ret), {}, 5)
    assert r.value is None and r.reason == "COMPUTE_ERROR"
    assert r.reason_detail.startswith("compute 返回了非有限数值")


def test_compute_long_repr_truncated():
    r = RunResult()
    sb._compute(r, module(compute=lambda d: "x" * 5000), {}, 5)
    assert len(r.reason_detail) == DETAIL_MAX


@pytest.mark.parametrize(
    ("compute", "reason", "detail"),
    [
        (raiser(Skip("LOW_COVERAGE")), "LOW_COVERAGE", None),
        (raiser(Skip("bad")), "COMPUTE_ERROR", "非法原因码：'bad'"),
        (raiser(ZeroDivisionError("div")), "COMPUTE_ERROR", "ZeroDivisionError: div"),
        (busy, "COMPUTE_ERROR", "compute 超过 0.2 秒"),
        (lambda d: eif_gde.run_info("RUN-0338"), "COMPUTE_ERROR", "RuntimeError: gde 只能在 fetch 中调用，compute 不能取数"),
    ],
)
def test_compute_failures(compute, reason, detail):
    r = RunResult()
    sb._compute(r, module(compute=compute), {}, 0.2)
    assert r.value is None and (r.reason, r.reason_detail) == (reason, detail)


def test_compute_receives_data():
    r = RunResult()
    sb._compute(r, module(compute=lambda d: d["x"] * 2), {"x": 21}, 5)
    assert r.value == 42.0


# ---------------------------------------------------------------- _execute 全流程
CODE = """from eifeature import gde, Skip

def fetch(run_id):
{fetch}

def compute(data):
{compute}
"""


@pytest.fixture
def execute(tmp_path):
    n = {"i": 0}

    def run(fetch: str, compute: str, run_id: str = "RUN-0338", timeouts=(2, 2)):
        n["i"] += 1
        d = tmp_path / f"pkg{n['i']}"
        d.mkdir()
        (d / "calc.py").write_text(CODE.format(fetch="    " + fetch, compute="    " + compute))
        return sb._execute(str(d / "calc.py"), f"eif_test_inproc_{id(tmp_path)}_{n['i']}", run_id, GDE_JSON, timeouts)

    return run


def test_execute_ok(execute):
    out = execute("return {'s': {'t': [1, 2], 'v': [3, 4]}}", "return sum(data['s']['v'])")
    assert out["value"] == 7.0 and out["reason"] is None
    assert out["data_summary"] == {"s": {"points": 2, "t_min": 1, "t_max": 2}}
    assert set(out) == set(RunResult().__dict__)


def test_execute_fetch_failure_skips_compute(execute):
    out = execute("raise Skip('NO_DATA')", "raise RuntimeError('不应执行')")
    assert out["reason"] == "NO_DATA" and out["compute_ms"] == 0 and out["data_summary"] == {}


def test_execute_compute_failure(execute):
    out = execute("return {'s': {'t': [1], 'v': [1]}}", "raise Skip('LOW_COVERAGE')")
    assert out["reason"] == "LOW_COVERAGE" and out["value"] is None
    assert out["data_summary"] == {"s": {"points": 1, "t_min": 1, "t_max": 1}}  # compute 失败也保留数据摘要


def test_execute_gde_unavailable(execute):
    out = execute("from src.util.gde import GdeUnavailable; raise GdeUnavailable('down')", "return 1")
    assert out == {"unavailable": "down"}
    assert eif_gde._client is None


def test_execute_uses_separate_timeouts(execute):
    out = execute("return {}", "while True: pass", timeouts=(2, 0.2))
    assert out["reason"] == "COMPUTE_ERROR" and out["reason_detail"] == "compute 超过 0.2 秒"
    out = execute("while True: pass", "return 1", timeouts=(0.2, 2))
    assert out["reason"] == "FETCH_TIMEOUT"


def test_execute_reuses_loaded_module(tmp_path):
    d = tmp_path / "pkg"
    d.mkdir()
    (d / "calc.py").write_text(CODE.format(fetch="    return {}", compute="    return 1"))
    key = f"eif_test_reuse_{id(tmp_path)}"
    assert sb._execute(str(d / "calc.py"), key, "RUN-0338", GDE_JSON, (2, 2))["value"] == 1.0
    (d / "calc.py").write_text(CODE.format(fetch="    return {}", compute="    return 2"))
    assert sb._execute(str(d / "calc.py"), key, "RUN-0338", GDE_JSON, (2, 2))["value"] == 1.0  # 同 key 不重新加载
