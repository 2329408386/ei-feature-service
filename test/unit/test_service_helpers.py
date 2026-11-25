"""服务层中不依赖数据库的部分：查询条件组装、参数校验、导入的前置校验。"""

from datetime import date

import pytest
from sqlalchemy.dialects import postgresql

from src.service import binding, value
from src.util.errors import AppError
from src.util.timeutil import day_range

BINDING = {"tool": "ETCH-01", "chamber": "CH-A01", "rules": [{"recipes": ["RCP-0432"], "features": {"f": "1.0.0"}}]}


def compiled(clauses) -> list[tuple[str, dict]]:
    out = []
    for c in clauses:
        cc = c.compile(dialect=postgresql.dialect())
        out.append((str(cc), cc.params))
    return out


# ---------------------------------------------------------------- value
def test_conditions_minimal():
    cond = compiled(value._conditions("f", "1.0.0", {"tool": None, "chamber": ""}, (None, None), False))
    assert len(cond) == 2
    assert "feature_value.feature_id" in cond[0][0] and list(cond[0][1].values()) == ["f"]
    assert "feature_value.version" in cond[1][0] and list(cond[1][1].values()) == ["1.0.0"]


def test_conditions_all_filters():
    filters = {"tool": "T", "chamber": "C", "recipe": "R", "wafer": "W", "lot": "L"}
    start, end = date(2026, 11, 1), date(2026, 11, 3)
    cond = compiled(value._conditions("f", "1.0.0", filters, (start, end), True))
    assert len(cond) == 2 + 5 + 3
    for (sql, params), (col, val) in zip(cond[2:7], filters.items(), strict=True):
        assert f"feature_value.{col} = " in sql and list(params.values()) == [val]
    assert ">=" in cond[7][0] and list(cond[7][1].values()) == [day_range(start, start)[0]]
    assert "<" in cond[8][0] and list(cond[8][1].values()) == [day_range(end, end)[1]]  # end 当天包含在内
    assert cond[9][0] == "feature_value.value IS NULL"


def test_conditions_only_some_filters():
    cond = compiled(value._conditions("f", "1", {"tool": None, "wafer": "W01"}, (None, date(2026, 1, 1)), False))
    assert len(cond) == 4
    assert "feature_value.wafer" in cond[2][0] and "feature_value.run_end_at <" in cond[3][0]


@pytest.mark.parametrize("feature", ["gas_flow", "gas_flow@", ""])
async def test_query_requires_version(feature):
    with pytest.raises(AppError) as e:
        await value.query(feature)
    assert e.value.code == "VALIDATION_ERROR" and e.value.status == 422


# ---------------------------------------------------------------- binding
def test_recipes_and_features_of():
    spec = {"rules": [{"recipes": ["B", "A"], "features": {"f": "1.0.0"}},
                      {"recipes": ["A", "C"], "features": {"g": "2.0.0"}}]}
    assert binding.recipes_of(spec) == ["A", "B", "C"]
    assert binding.features_of(spec) == {"f@1.0.0", "g@2.0.0"}


class CaptureSession:
    """只记录 execute 的语句，返回给定的腔室列表。"""

    def __init__(self, chambers):
        self.chambers, self.stmt = chambers, None

    async def execute(self, stmt):
        self.stmt = stmt
        chambers = self.chambers

        class Result:
            def scalars(self):
                return self

            def all(self):
                return chambers

        return Result()

    def sql(self) -> tuple[str, list]:
        c = self.stmt.compile(dialect=postgresql.dialect())
        return str(c), list(c.params.values())


@pytest.mark.parametrize(
    ("recipe", "feature", "where", "params"),
    [
        ("RCP-1", None, ["binding_item.recipe ="], ["RCP-1"]),
        (None, "f@1.0.0", ["binding_item.feature_id =", "binding_item.version ="], ["f", "1.0.0"]),
        (None, "f", ["binding_item.feature_id ="], ["f"]),  # 只给 id 时不限版本
        ("RCP-1", "f@2", ["binding_item.recipe =", "binding_item.feature_id =", "binding_item.version ="],
         ["RCP-1", "f", "2"]),
    ],
)
async def test_matched_chambers(recipe, feature, where, params):
    s = CaptureSession(["CH-1", "CH-2", "CH-1"])
    assert await binding._matched_chambers(s, recipe, feature) == {"CH-1", "CH-2"}
    sql, values = s.sql()
    assert "JOIN chamber_binding" in sql and "chamber_binding.status =" in sql
    for w in where:
        assert w in sql
    if not feature or "@" not in feature:
        assert "binding_item.version" not in sql
    assert values == ["active", *params]


async def test_import_one_format_error_does_not_touch_db():
    seen = set()
    res = await binding._import_one(None, {"tool": "T", "chamber": "C"}, seen, True, "alice")
    assert res["action"] == "error" and res["message"] == "格式错误"
    assert res["tool"] == "T" and res["chamber"] == "C"
    assert any(e["path"] == "rules" for e in res["errors"])
    assert seen == set()  # 格式错误的不计入已出现


async def test_import_one_duplicate_does_not_touch_db():
    res = await binding._import_one(None, BINDING, {"CH-A01"}, True, "alice")
    assert res == {"tool": "ETCH-01", "chamber": "CH-A01", "action": "error", "message": "文件中重复出现"}


async def test_import_one_non_dict_rules():
    res = await binding._import_one(None, {**BINDING, "rules": "x"}, set(), True, "alice")
    assert res["action"] == "error" and res["errors"]
