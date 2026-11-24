"""沙箱：真实子进程 + 假 GDE + 本地特征包。"""

import pytest

from src.engine.sandbox import Sandbox
from src.util import storage
from src.util.gde import GdeUnavailable

FETCH = "from eifeature import gde, Skip\n\ndef fetch(run_id):\n{fetch}\n\ndef compute(data):\n{compute}\n"


@pytest.fixture(scope="module")
def sandbox():
    sb = Sandbox(processes=2)
    yield sb
    sb.close()


@pytest.fixture
def package(tmp_path, monkeypatch):
    """把特征包写到临时目录，返回 (id, version, hash)。"""
    store = storage.LocalStore(tmp_path / "store")
    monkeypatch.setattr(storage, "get_store", lambda: store)
    monkeypatch.setattr(storage, "PACKAGE_CACHE_DIR", tmp_path / "cache")

    def make(fid: str, spec: bytes, code: bytes):
        store.put(fid, "1.0.0", storage.SPEC_FILE, spec)
        store.put(fid, "1.0.0", storage.CODE_FILE, code)
        return fid, "1.0.0", storage.package_hash(spec, code)

    return make


async def run(sandbox, package, fetch: str, compute: str, run_id: str = "RUN-0338"):
    code = FETCH.format(fetch="    " + fetch, compute="    " + compute).encode()
    fid = f"t_{abs(hash(code)) % 10**8}"
    return await sandbox.run(*package(fid, b"{}", code), run_id)


async def test_sample_feature(sandbox, package, sample_package):
    r = await sandbox.run(*package("gas_flow_diffmean", *sample_package), "RUN-0338")
    assert r.reason is None and isinstance(r.value, float)
    assert set(r.data_summary) == {"actual", "setpoint"}


@pytest.mark.parametrize(
    ("fetch", "compute", "run_id", "reason"),
    [
        ("return {}", "raise Skip('LOW_COVERAGE')", "RUN-0338", "LOW_COVERAGE"),
        ("return gde.trace(run_id, 'MISSING_X')", "return 1", "RUN-0338", "MISSING_SIGNAL"),
        ("return gde.run_info(run_id)", "return 1", "RUN-MISSING-1", "MISSING_DATA"),
        ("raise ValueError('boom')", "return 1", "RUN-0338", "FETCH_ERROR"),
        ("while True: pass", "return 1", "RUN-0338", "FETCH_TIMEOUT"),
        ("return {}", "return gde.run_info('RUN-0338')", "RUN-0338", "COMPUTE_ERROR"),  # compute 不能取数
        ("return {}", "return float('nan')", "RUN-0338", "COMPUTE_ERROR"),
        ("return {}", "raise Skip('bad reason')", "RUN-0338", "COMPUTE_ERROR"),
    ],
)
async def test_reasons(sandbox, package, monkeypatch, fetch, compute, run_id, reason):
    monkeypatch.setattr("src.engine.sandbox.FETCH_TIMEOUT_S", 1)
    r = await run(sandbox, package, fetch, compute, run_id)
    assert r.value is None and r.reason == reason


async def test_gde_unavailable_is_raised(sandbox, package):
    with pytest.raises(GdeUnavailable):
        await run(sandbox, package, "from src.util.gde import GdeUnavailable; raise GdeUnavailable('x')", "return 1")
