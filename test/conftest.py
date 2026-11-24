"""公共夹具。集成测试需要 config/config.json 中的 PostgreSQL，连不上时自动跳过。"""

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_package() -> tuple[bytes, bytes]:
    d = FIXTURES / "gas_flow_diffmean"
    return (d / "feature.json").read_bytes(), (d / "compute.py").read_bytes()
