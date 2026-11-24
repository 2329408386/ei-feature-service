import json

from src.service.package import check_package

SPEC = json.dumps({"id": "demo_feature", "version": "1.0.0", "desc": "示例"}).encode()
CODE = b"from eifeature import gde\n\ndef fetch(run_id):\n    return {}\n\ndef compute(data):\n    return 1.0\n"


def messages(spec=SPEC, code=CODE) -> list[str]:
    return [e["message"] for e in check_package(spec, code)[1]]


def test_sample_package_is_valid(sample_package):
    assert messages(*sample_package) == []


def test_minimal_package_is_valid():
    assert messages() == []


def test_bad_spec_reports_all_errors():
    spec = json.dumps({"id": "Bad", "version": "1.0", "desc": "", "output": {"unit": "x" * 40}}).encode()
    assert len(messages(spec=spec)) == 4


def test_missing_functions_and_wrong_arity():
    errs = messages(code=b"def fetch(a, b):\n    pass\n")
    assert any("fetch 必须恰好有一个位置参数" in m for m in errs)
    assert any("compute" in m for m in errs)


def test_forbidden_imports_names_and_dunders():
    code = CODE + b"import os\nx = open\ny = (1).__class__\n"
    errs = messages(code=code)
    assert any("不允许导入 os" in m for m in errs)
    assert any("不允许使用 open" in m for m in errs)
    assert any("__class__" in m for m in errs)


def test_syntax_error_has_line():
    _, errors = check_package(SPEC, b"def fetch(run_id):\n  return (\n")
    assert errors[0]["file"] == "compute.py" and errors[0]["line"]
