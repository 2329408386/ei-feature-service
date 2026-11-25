"""公共夹具。集成测试需要 config/config.json 中的 PostgreSQL，连不上时自动跳过。"""

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_sample() -> tuple[dict, dict, str]:
    """示例特征：(feature.json, operator.json, 算子代码)。"""
    d = FIXTURES / "gas_flow_diffmean"
    return (json.loads((d / "feature.json").read_text()), json.loads((d / "operator.json").read_text()),
            (d / "compute.py").read_text())


@pytest.fixture
def sample() -> tuple[dict, dict, str]:
    return load_sample()
