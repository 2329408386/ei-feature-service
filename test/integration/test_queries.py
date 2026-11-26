"""查询与校验：特征值过滤、绑定列表过滤、导入的各种结果、批次参数校验。

与 test_flow.py 一样使用 config/config.json 中的 PostgreSQL（连不上就跳过），开始前清空业务表。
"""

from datetime import timedelta

import fakeredis
import pytest
from httpx import ASGITransport, AsyncClient

from main import app
from src.engine.live import LiveConsumer
from src.util import db
from src.util.config import BATCH_MAX_DAYS
from src.util.stream import RunDoneStream
from src.util.timeutil import today
from test.conftest import load_sample
from test.integration.test_flow import ALICE, BINDING, BOB, REF, B, F, O, err, ok, reset

RUNS = [  # (run_id, end_at, wafer, lot)
    ("RUN-CH-A01-202611200815", "2026-11-20T08:15:00+08:00", "W01", "L1"),
    ("RUN-CH-A01-202611220815", "2026-11-22T08:15:00+08:00", "W02", "L1"),
    ("RUN-CH-A01-202611240815", "2026-11-24T08:15:00+08:00", "W03", "L2"),
]
MAX = "gas_flow_diffmean/diffmax"
BINDING_MAX = {**BINDING, "operator": "diffmax", "recipes": ["RCP-1107"]}


@pytest.fixture(scope="module")
async def client():
    try:
        await reset()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"PostgreSQL 不可用：{e}")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await _seed(c)
        yield c
    await db.dispose()


async def _publish_operator(c: AsyncClient, spec: dict, code: str) -> None:
    out = ok(await c.post("/api/operators", json={"feature_id": "gas_flow_diffmean", "spec": spec, "code": code},
                          headers=ALICE), 201)
    path = f"/api/operators/{out['feature_id']}/{out['name']}/{out['version']}"
    ok(await c.post(path + "/submit", headers=ALICE))
    ok(await c.post(path + "/review", json={"decision": "approve", "trial_runs": [RUNS[0][0]]}, headers=BOB))


async def _seed(c: AsyncClient) -> None:
    """发布特征和两个算子 → diffmean 绑定生效（RCP-0432）→ diffmax 待审核（RCP-1107）→ 3 次实时计算。"""
    feature, spec, code = load_sample()
    ok(await c.post("/api/features", json=feature, headers=ALICE), 201)
    ok(await c.post(F + "/submit", headers=ALICE))
    ok(await c.post(F + "/review", json={"decision": "approve"}, headers=BOB))
    await _publish_operator(c, spec, code)
    await _publish_operator(c, {"name": "diffmax", "desc": "偏差最大值"}, code)

    rev = ok(await c.post(B, json=BINDING, headers=ALICE), 201)
    ok(await c.post(f"{B}/revisions/{rev['revision']}/review", json={"decision": "approve"}, headers=BOB))
    ok(await c.post(f"/api/bindings/{MAX}", json=BINDING_MAX, headers=ALICE), 201)

    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    consumer = LiveConsumer(RunDoneStream(redis))
    await consumer.stream.ensure_group()
    for run_id, end_at, wafer, lot in RUNS:
        await redis.xadd("gde:run:done", {"run_id": run_id, "tool": "ETCH-01", "chamber": "CH-A01",
                                          "recipe": "RCP-0432", "end_at": end_at, "wafer": wafer, "lot": lot})
    for msg_id, f in await consumer.stream.read(block_ms=10):
        await consumer.handle(msg_id, f)


async def values(c: AsyncClient, **params) -> dict:
    return ok(await c.get("/api/values", params={"operator": REF, **params}))


# ---------------------------------------------------------------- 特征值查询
async def test_values_all_sorted_by_end_time(client):
    page = await values(client)
    assert page["total"] == 3 and page["page"] == 1 and page["page_size"] == 100
    assert [x["run_id"] for x in page["items"]] == [r[0] for r in RUNS]
    assert all(x["retired"] is False for x in page["items"])


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"wafer": "W02"}, [1]),
        ({"lot": "L1"}, [0, 1]),
        ({"lot": "L2", "wafer": "W01"}, []),
        ({"tool": "ETCH-01", "chamber": "CH-A01", "recipe": "RCP-0432"}, [0, 1, 2]),
        ({"chamber": "CH-A02"}, []),
        ({"recipe": "RCP-0500"}, []),
        ({"from": "2026-11-22"}, [1, 2]),
        ({"to": "2026-11-22"}, [0, 1]),  # to 当天包含在内
        ({"from": "2026-11-21", "to": "2026-11-23"}, [1]),
        ({"from": "2026-11-25"}, []),
    ],
)
async def test_values_filters(client, params, expected):
    page = await values(client, **params)
    assert [x["run_id"] for x in page["items"]] == [RUNS[i][0] for i in expected]
    assert page["total"] == len(expected)


async def test_values_only_null(client):
    page = await values(client, only_null="true")
    assert all(x["value"] is None for x in page["items"])
    assert page["total"] == sum(1 for x in (await values(client))["items"] if x["value"] is None)


async def test_values_paging(client):
    p1 = await values(client, page=1, page_size=2)
    p2 = await values(client, page=2, page_size=2)
    p3 = await values(client, page=3, page_size=2)
    assert p1["total"] == p2["total"] == p3["total"] == 3
    assert [x["run_id"] for x in p1["items"] + p2["items"]] == [r[0] for r in RUNS]
    assert p3["items"] == []


async def test_values_other_operator_or_version(client):
    assert ok(await client.get("/api/values", params={"operator": f"{MAX}@1"}))["total"] == 0  # 只在待审核里
    assert ok(await client.get("/api/values", params={"operator": "gas_flow_diffmean/diffmean@2"}))["total"] == 0
    assert ok(await client.get("/api/values", params={"operator": "nope_x/nope_y@1"}))["items"] == []


@pytest.mark.parametrize(
    "params",
    [{"operator": "gas_flow_diffmean@1.0.0"}, {"operator": REF, "page": 0}, {"operator": REF, "page_size": 1001}, {}],
)
async def test_values_bad_params(client, params):
    err(await client.get("/api/values", params=params), 422, "VALIDATION_ERROR")


# ---------------------------------------------------------------- 绑定列表
async def operators(c: AsyncClient, **params) -> list[str]:
    return [x["operator"] for x in ok(await c.get("/api/bindings", params=params))["items"]]


async def test_list_bindings_items(client):
    items = ok(await client.get("/api/bindings"))["items"]
    by = {x["operator"]: x for x in items}
    assert list(by) == ["diffmax", "diffmean"]
    assert by["diffmean"]["active"]["status"] == "active" and by["diffmean"]["pending"] is None
    assert by["diffmax"]["active"] is None and by["diffmax"]["pending"]["status"] == "pending"


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"status": "active"}, ["diffmean"]),
        ({"status": "pending"}, ["diffmax"]),
        ({"feature_id": "gas_flow_diffmean"}, ["diffmax", "diffmean"]),
        ({"feature_id": "other"}, []),
        ({"operator": "diffmax"}, ["diffmax"]),
        # recipe 只看生效修订：diffmax 只有待审核，不命中
        ({"recipe": "RCP-0432"}, ["diffmean"]),
        ({"recipe": "RCP-1107"}, []),
        ({"recipe": "RCP-0433"}, []),
        ({"recipe": "RCP-0432", "status": "active"}, ["diffmean"]),
        ({"recipe": "RCP-0432", "status": "pending"}, []),
    ],
)
async def test_list_bindings_filters(client, params, expected):
    assert await operators(client, **params) == expected


async def test_list_bindings_bad_status(client):
    err(await client.get("/api/bindings", params={"status": "rejected"}), 422, "VALIDATION_ERROR")


async def test_export_only_active(client):
    doc = ok(await client.get("/api/bindings/export"))
    assert [(b["operator"], b["revision"]) for b in doc["bindings"]] == [("diffmean", 1)]
    assert ok(await client.get("/api/bindings/export", params={"recipe": "RCP-1107"}))["bindings"] == []


async def test_get_single_revision(client):
    rev = ok(await client.get(B, params={"revision": 1}))
    assert rev["revision"] == 1 and rev["spec"]["recipes"] == ["RCP-0432"]
    err(await client.get(B, params={"revision": 99}), 404, "NOT_FOUND")


# ---------------------------------------------------------------- 导入
async def import_(c: AsyncClient, bindings: list[dict], dry_run: bool = True) -> list[dict]:
    doc = {"format": "ei-feature-service.bindings/v3", "bindings": bindings}
    return ok(await c.post("/api/bindings/import", params={"dry_run": str(dry_run).lower()}, json=doc,
                           headers=ALICE))["items"]


async def test_import_all_actions(client):
    changed = {**BINDING, "revision": 99, "recipes": ["RCP-0433"]}
    result = await import_(client, [
        {"feature_id": "gas_flow_diffmean"},       # 格式错误
        changed,                                    # new，提示基于旧修订
        {**changed, "revision": None},              # 同一算子重复出现
        BINDING_MAX,                                # 已有待审核 → conflict
        {**BINDING, "operator": "nope_op"},         # 校验失败（算子不存在）
    ])
    assert [x["action"] for x in result] == ["error", "new", "error", "conflict", "error"]
    assert result[0]["message"] == "格式错误" and result[0]["errors"]
    assert result[1]["message"] == "生成待审核修订（文件基于修订 99，当前生效修订 1）" and result[1].get("revision") is None
    assert result[2]["message"] == "文件中重复出现"
    assert result[4]["message"] == "校验失败" and result[4]["errors"][0]["path"] == "version"
    assert await operators(client, status="pending") == ["diffmax"]  # dry_run 不写库


async def test_import_skip_same_as_active(client):
    result = await import_(client, [{**BINDING, "revision": 1}])
    assert [x["action"] for x in result] == ["skip"] and result[0]["message"]


async def test_import_empty(client):
    assert await import_(client, []) == []


async def test_import_bad_format(client):
    for fmt in ("other/v1", "ei-feature-service.bindings/v2"):
        err(await client.post("/api/bindings/import", json={"format": fmt, "bindings": []}, headers=ALICE),
            422, "VALIDATION_ERROR")


async def test_import_requires_user(client):
    doc = {"format": "ei-feature-service.bindings/v3", "bindings": []}
    err(await client.post("/api/bindings/import", json=doc), 401, "UNAUTHENTICATED")


async def test_binding_review_rules(client):
    b = f"/api/bindings/{MAX}"
    rev = ok(await client.get(b))["revisions"][0]["revision"]
    err(await client.post(f"{b}/revisions/{rev}/review", json={"decision": "reject"}, headers=BOB),
        422, "VALIDATION_ERROR")
    out = ok(await client.post(f"{b}/revisions/{rev}/review", json={"decision": "reject", "comment": "不需要"},
                               headers=BOB))
    assert out["status"] == "rejected" and out["review_note"] == "不需要"
    err(await client.post(f"{b}/revisions/{rev}/review", json={"decision": "approve"}, headers=BOB),
        409, "INVALID_STATE")
    ok(await client.post(b, json=BINDING_MAX, headers=ALICE), 201)  # 退回后可以重新提交


# ---------------------------------------------------------------- 批次参数校验
def batch_body(**kw) -> dict:
    day = str(today() - timedelta(days=1))
    return {"recipe": "RCP-0432", "mode": "fill", "operators": [REF], "from": day, "to": day, "reason": "补", **kw}


async def batch_errors(c: AsyncClient, body: dict) -> set[str]:
    out = err(await c.post("/api/batches", json=body, headers=ALICE), 422, "VALIDATION_ERROR")
    return {e["path"] for e in out["details"]["errors"]}


async def test_batch_from_after_to(client):
    t = today() - timedelta(days=1)
    body = batch_body(**{"from": str(t), "to": str(t - timedelta(days=1))})
    assert await batch_errors(client, body) == {"from"}


async def test_batch_to_in_future(client):
    assert await batch_errors(client, batch_body(to=str(today() + timedelta(days=1)))) == {"to"}


async def test_batch_too_many_days(client):
    t = today()
    body = batch_body(**{"from": str(t - timedelta(days=BATCH_MAX_DAYS)), "to": str(t)})
    assert await batch_errors(client, body) == {"to"}
    body = batch_body(**{"from": str(t - timedelta(days=BATCH_MAX_DAYS - 1)), "to": str(t)})
    ok(await client.post("/api/batches", json=body, headers=ALICE), 201)  # 恰好上限可以


async def test_batch_all_errors_together(client):
    t = today()
    body = batch_body(**{"from": str(t + timedelta(days=1)), "to": str(t + timedelta(days=1)),
                         "operators": ["bad", REF]})
    assert await batch_errors(client, body) == {"to", "operators"}


async def test_batch_operator_not_bound(client):
    out = err(await client.post("/api/batches", json=batch_body(operators=[f"{MAX}@1", "b_b/a_a@1"]), headers=ALICE),
              422, "VALIDATION_ERROR")
    assert out["details"]["errors"] == [
        {"path": "operators", "message": f"不在 recipe RCP-0432 的生效绑定中：b_b/a_a@1、{MAX}@1"}]


async def test_batch_recipe_without_active_binding(client):
    out = err(await client.post("/api/batches", json=batch_body(recipe="RCP-1107"), headers=ALICE), 409, "INVALID_STATE")
    assert out["details"] == {"status": "none"}


async def test_batch_created_fields(client):
    body = batch_body(operators=[REF, REF], mode="overwrite", reason="重算")
    b = ok(await client.post("/api/batches", json=body, headers=ALICE), 201)
    assert b["status"] == "queued" and b["mode"] == "overwrite" and b["recipe"] == "RCP-0432"
    assert b["operators"] == [REF] and b["created_by"] == "alice"
    assert ok(await client.get(f"/api/batches/{b['batch_id']}"))["batch_id"] == b["batch_id"]


@pytest.mark.parametrize("body", [batch_body(operators=[]), batch_body(mode="live"), batch_body(reason="")])
async def test_batch_schema_errors(client, body):
    err(await client.post("/api/batches", json=body, headers=ALICE), 422, "VALIDATION_ERROR")


async def test_batch_not_found(client):
    err(await client.get("/api/batches/999999"), 404, "NOT_FOUND")
    err(await client.post("/api/batches/999999/cancel", headers=ALICE), 404, "NOT_FOUND")


async def test_batch_list_filters(client):
    page = ok(await client.get("/api/batches", params={"recipe": "RCP-0432", "status": "queued", "page_size": 1}))
    assert page["total"] >= 2 and len(page["items"]) == 1
    assert ok(await client.get("/api/batches", params={"recipe": "RCP-ZZZ"}))["total"] == 0


# ---------------------------------------------------------------- 特征 / 算子查询
async def test_operator_list_and_detail(client):
    page = ok(await client.get("/api/operators", params={"q": "最大"}))
    assert [o["ref"] for o in page["items"]] == [f"{MAX}@1"]
    assert ok(await client.get("/api/operators", params={"name": "diffmean"}))["total"] == 1
    err(await client.get(O + "/9"), 404, "NOT_FOUND")
    err(await client.get(O + "/x"), 422, "VALIDATION_ERROR")


# ---------------------------------------------------------------- 名称与机台类型
async def test_feature_meta(client):
    err(await client.patch("/api/features/gas_flow_diffmean", json={"name": "气体偏差"}), 401, "UNAUTHENTICATED")
    err(await client.patch("/api/features/nope_feature", json={"name": "x"}, headers=ALICE), 404, "NOT_FOUND")
    out = ok(await client.patch("/api/features/gas_flow_diffmean", json={"name": " 气体偏差 ", "tool_type": "ETCH"},
                                headers=ALICE))
    assert out == {"feature_id": "gas_flow_diffmean", "name": "气体偏差", "tool_type": "ETCH"}
    # 只改传入的字段
    out = ok(await client.patch("/api/features/gas_flow_diffmean", json={"name": "气体流量偏差"}, headers=BOB))
    assert out["tool_type"] == "ETCH"
    detail = ok(await client.get(F))
    assert (detail["feature_id"], detail["display_name"], detail["tool_type"]) == (
        "gas_flow_diffmean", "气体流量偏差", "ETCH")
    assert ok(await client.get("/api/features/tool-types")) == ["ETCH"]
    assert ok(await client.get("/api/features", params={"tool_type": "ETCH"}))["total"] == 1
    assert ok(await client.get("/api/features", params={"tool_type": "CVD"}))["total"] == 0
    assert ok(await client.get("/api/features", params={"q": "流量偏差"}))["total"] == 1
    err(await client.patch("/api/features/gas_flow_diffmean", json={"name": "x" * 65}, headers=ALICE),
        422, "VALIDATION_ERROR")


async def test_operator_meta(client):
    err(await client.patch("/api/operators/gas_flow_diffmean/nope_op", json={"name": "x"}, headers=ALICE),
        404, "NOT_FOUND")
    out = ok(await client.patch("/api/operators/gas_flow_diffmean/diffmax", json={"name": "偏差峰值"}, headers=ALICE))
    assert out == {"feature_id": "gas_flow_diffmean", "operator": "diffmax", "name": "偏差峰值"}
    page = ok(await client.get("/api/operators", params={"q": "峰值"}))
    assert [(o["ref"], o["display_name"]) for o in page["items"]] == [(f"{MAX}@1", "偏差峰值")]
    assert ok(await client.get("/api/operators/gas_flow_diffmean/diffmax/1"))["display_name"] == "偏差峰值"
    assert ok(await client.get(O + "/1"))["display_name"] is None
    ok(await client.patch("/api/operators/gas_flow_diffmean/diffmax", json={"name": ""}, headers=ALICE))
    assert ok(await client.get("/api/operators/gas_flow_diffmean/diffmax/1"))["display_name"] is None


# ---------------------------------------------------------------- GDE：试跑选 run_id
async def test_gde_trial_pickers(client):
    assert ok(await client.get("/api/gde/chambers/CH-A01/recipes")) == ["RCP-0432", "RCP-0433", "RCP-0500"]
    err(await client.get("/api/gde/chambers/CH-ZZZ/recipes"), 404, "NOT_FOUND")
    day = str(today() - timedelta(days=1))
    runs = ok(await client.get("/api/gde/runs", params={"recipe": "RCP-0432", "chamber": "CH-A01",
                                                        "start": day, "end": day}))
    assert runs and all((r["chamber"], r["recipe"]) == ("CH-A01", "RCP-0432") for r in runs)
    assert [r["end_at"] for r in runs] == sorted((r["end_at"] for r in runs), reverse=True)
    both = ok(await client.get("/api/gde/runs", params={"recipe": "RCP-0432", "start": day, "end": day}))
    assert {r["chamber"] for r in both} <= {"CH-A01", "CH-A02"} and len(both) >= len(runs)
    assert len(ok(await client.get("/api/gde/runs", params={"recipe": "RCP-0432", "limit": 3}))) == 3
