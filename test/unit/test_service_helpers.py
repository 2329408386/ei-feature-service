"""服务层中不依赖数据库的部分：查询条件组装、参数校验、算子引用、绑定规范化、导入的前置校验。"""

from datetime import date

import pytest
from sqlalchemy.dialects import postgresql

from src.api.schemas import BindingSpec
from src.service import binding, refs, value
from src.util.errors import AppError
from src.util.timeutil import day_range

BINDING = {
    "feature_id": "gas_flow_diffmean", "operator": "diffmean", "version": 1,
    "recipes": [{"recipe": "RCP-0432", "chambers": [{"tool": "ETCH-01", "chamber": "CH-A01"}]}],
}
KEY = ("f_x", "op_y", 2)


def compiled(clauses) -> list[tuple[str, dict]]:
    out = []
    for c in clauses:
        cc = c.compile(dialect=postgresql.dialect())
        out.append((str(cc), cc.params))
    return out


# ---------------------------------------------------------------- 算子引用
@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("gas_flow/diffmean@2", ("gas_flow", "diffmean", 2)),
        ("gas_flow/diffmean@0", None),
        ("gas_flow/diffmean@1.0.0", None),
        ("gas_flow@1", None),
        ("Gas/diffmean@1", None),
        ("", None),
    ],
)
def test_parse_ref(ref, expected):
    assert refs.parse_ref(ref) == expected
    if expected:
        assert refs.op_ref(*expected) == ref


# ---------------------------------------------------------------- value
def test_conditions_minimal():
    cond = compiled(value._conditions(KEY, {"tool": None, "chamber": ""}, (None, None), False))
    assert len(cond) == 3
    for (sql, params), (col, val) in zip(cond, [("feature_id", "f_x"), ("operator", "op_y"), ("version", 2)]):
        assert f"feature_value.{col}" in sql and list(params.values()) == [val]


def test_conditions_all_filters():
    filters = {"tool": "T", "chamber": "C", "recipe": "R", "wafer": "W", "lot": "L"}
    start, end = date(2026, 11, 1), date(2026, 11, 3)
    cond = compiled(value._conditions(KEY, filters, (start, end), True))
    assert len(cond) == 3 + 5 + 3
    for (sql, params), (col, val) in zip(cond[3:8], filters.items(), strict=True):
        assert f"feature_value.{col} = " in sql and list(params.values()) == [val]
    assert ">=" in cond[8][0] and list(cond[8][1].values()) == [day_range(start, start)[0]]
    assert "<" in cond[9][0] and list(cond[9][1].values()) == [day_range(end, end)[1]]  # end 当天包含在内
    assert cond[10][0] == "feature_value.value IS NULL"


@pytest.mark.parametrize("operator", ["gas_flow", "gas_flow@1", "gas_flow/diffmean", "gas_flow/diffmean@x", ""])
async def test_query_requires_ref(operator):
    with pytest.raises(AppError) as e:
        await value.query(operator)
    assert e.value.code == "VALIDATION_ERROR" and e.value.status == 422


# ---------------------------------------------------------------- binding
def spec(**kw) -> BindingSpec:
    return BindingSpec.model_validate({**BINDING, **kw})


def test_normalize_sorts_and_dedups():
    s = spec(fleet={"name": "F", "start": "2026-10-01", "end": "2026-11-01"}, recipes=[
        {"recipe": "B", "chambers": [{"tool": "T2", "chamber": "C2"}, {"tool": "T1", "chamber": "C1"},
                                     {"tool": "T1", "chamber": "C1"}]},
        {"recipe": "A", "chambers": [{"tool": "T1", "chamber": "C1"}]},
    ])
    n = binding.normalize(s)
    assert n["fleet"] == {"name": "F", "start": "2026-10-01", "end": "2026-11-01"}
    assert [r["recipe"] for r in n["recipes"]] == ["A", "B"]
    assert n["recipes"][1]["chambers"] == [{"tool": "T1", "chamber": "C1"}, {"tool": "T2", "chamber": "C2"}]
    assert binding.chambers_of(n) == {"C1", "C2"}
    assert binding.recipes_on(n, "C1") == ["A", "B"] and binding.recipes_on(n, "C2") == ["B"]


def test_check_structure():
    assert binding._check_structure(spec()) == []
    errs = binding._check_structure(spec(
        fleet={"name": "F", "start": "2026-11-02", "end": "2026-11-01"},
        recipes=[{"recipe": "A", "chambers": []}, {"recipe": "A", "chambers": [{"tool": "T", "chamber": "C"}]}],
    ))
    assert [e["path"] for e in errs] == ["fleet.start", "recipes[0].chambers", "recipes[1].recipe"]
    assert [e["path"] for e in binding._check_structure(spec(recipes=[]))] == ["recipes"]


async def test_check_chambers_against_gde():
    s = spec(recipes=[
        {"recipe": "RCP-0432", "chambers": [{"tool": "ETCH-01", "chamber": "CH-A01"},
                                            {"tool": "ETCH-02", "chamber": "CH-A01"},
                                            {"tool": "ETCH-02", "chamber": "CH-B02"}]},
        {"recipe": "RCP-9999", "chambers": [{"tool": "ETCH-01", "chamber": "CH-A01"}]},
    ])
    errs = await binding._check_chambers(s)
    assert [e["path"] for e in errs] == ["recipes[0].chambers[1]", "recipes[0].chambers[2]", "recipes[1].recipe"]


async def test_import_one_format_error_does_not_touch_db():
    seen = set()
    res = await binding._import_one(None, {"feature_id": "f", "operator": "o"}, seen, True, "alice")
    assert res["action"] == "error" and res["message"] == "格式错误"
    assert res["feature_id"] == "f" and res["operator"] == "o"
    assert any(e["path"] == "recipes" for e in res["errors"])
    assert seen == set()  # 格式错误的不计入已出现


async def test_import_one_duplicate_does_not_touch_db():
    res = await binding._import_one(None, BINDING, {("gas_flow_diffmean", "diffmean")}, True, "alice")
    assert res == {"feature_id": "gas_flow_diffmean", "operator": "diffmean", "action": "error",
                   "message": "文件中重复出现"}


async def test_import_one_non_list_recipes():
    res = await binding._import_one(None, {**BINDING, "recipes": "x"}, set(), True, "alice")
    assert res["action"] == "error" and res["errors"]
