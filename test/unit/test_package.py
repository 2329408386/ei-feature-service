from src.service.package import check_code, check_feature_spec, check_operator

FEATURE = {"id": "demo_feature", "version": "1.0.0", "desc": "示例"}
OPERATOR = {"name": "demo_op", "desc": "示例算子"}
CODE = "from eifeature import gde\n\ndef fetch(run_id):\n    return {}\n\ndef compute(data):\n    return 1.0\n"


def messages(spec=OPERATOR, code=CODE, code_file=None) -> list[str]:
    return [e["message"] for e in check_operator(spec, code, code_file)]


def test_sample_is_valid(sample):
    feature, operator, code = sample
    assert check_feature_spec(feature) == []
    assert messages(operator, code) == []


def test_minimal_is_valid():
    assert check_feature_spec(FEATURE) == [] and messages() == []


def test_bad_feature_spec_reports_all_errors():
    spec = {"id": "Bad", "version": "1.0", "desc": "", "output": {"unit": "x" * 40}}
    assert len(check_feature_spec(spec)) == 4
    assert [e["message"] for e in check_feature_spec({**FEATURE, "output": 1})] == ["output 必须是对象"]


def test_bad_operator_spec():
    errs = messages({"name": "X", "desc": " "})
    assert len(errs) == 2 and all(e for e in errs)


def test_code_file_name():
    assert messages(code_file="my_op.py") == []
    for bad in ("my-op.py", "1op.py", "op.txt", "../x.py", "a" * 70 + ".py"):
        errs = check_operator(OPERATOR, CODE, bad)
        assert [e["file"] for e in errs] == ["code_file"], bad


def test_errors_name_the_code_file():
    errs = check_operator(OPERATOR, "import os\n", "calc.py")
    assert errs and all(e["file"] == "calc.py" for e in errs)


def test_missing_functions_and_wrong_arity():
    errs = messages(code="def fetch(a, b):\n    pass\n")
    assert any("fetch 必须恰好有一个位置参数" in m for m in errs)
    assert any("compute" in m for m in errs)


def test_forbidden_imports_names_and_dunders():
    code = CODE + "import os\nx = open\ny = (1).__class__\n"
    errs = messages(code=code)
    assert any("不允许导入 os" in m for m in errs)
    assert any("不允许使用 open" in m for m in errs)
    assert any("__class__" in m for m in errs)


def test_syntax_error_has_line():
    errors = check_code("def fetch(run_id):\n  return (\n")
    assert errors[0]["file"] == "compute.py" and errors[0]["line"]


def test_code_too_large():
    assert check_code("#" * (256 * 1024 + 1))[0]["message"] == "文件超过 256 KB"
