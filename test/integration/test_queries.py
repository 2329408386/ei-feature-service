"""查询与校验：特征值过滤、绑定列表过滤、导入的各种结果、批次参数校验。

与 test_flow.py 一样使用 config/config.json 中的 PostgreSQL（连不上就跳过），开始前清空业务表。
"""

from datetime import timedelta

import fakeredis
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from main import app
from src.engine.live import LiveConsumer
from src.util import db
from src.util.config import BATCH_MAX_DAYS
from src.util.stream import RunDoneStream
from src.util.timeutil import today
from test.integration.test_flow import ALICE, B, BINDING, BOB, F, TABLES, err, ok

FEATURE = "gas_flow_diffmean@1.0.0"
RUNS = [  # (run_id, end_at, wafer, lot)
    ("RUN-CH-A01-202611200815", "2026-11-20T08:15:00+08:00", "W01", "L1"),
    ("RUN-CH-A01-202611220815", "2026-11-22T08:15:00+08:00", "W02", "L1"),
    ("RUN-CH-A01-202611240815", "2026-11-24T08:15:00+08:00", "W03", "L2"),
]


@pytest.fixture(scope="module")
async def client(sample_package_module):
    db.configure()
    try:
        await db.init_schema()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"PostgreSQL 不可用：{e}")
    async with db.session() as s:
        await s.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await _seed(c, sample_package_module)
        yield c
    await db.dispose()


@pytest.fixture(scope="module")
def sample_package_module():
    from test.conftest import FIXTURES

    d = FIXTURES / "gas_flow_diffmean"
    return (d / "feature.json").read_bytes(), (d / "compute.py").read_bytes()


async def _seed(c: AsyncClient, package) -> None:
    """发布特征 → CH-A01 绑定生效 → CH-A02 待审核 → 3 次实时计算。"""
    spec, code = package
    ok(await c.post("/api/features", files={"spec": ("feature.json", spec), "code": ("compute.py", code)},
                    headers=ALICE), 201)
    ok(await c.post(F + "/submit", headers=ALICE))
    ok(await c.post(F + "/trial", json={"run_id": RUNS[0][0]}))
    ok(await c.post(F + "/review", json={"decision": "approve", "trial_runs": [RUNS[0][0]]}, headers=BOB))

    rev = ok(await c.post(f"{B}/CH-A01", json=BINDING, headers=ALICE), 201)
    ok(await c.post(f"{B}/CH-A01/revisions/{rev['revision']}/review", json={"decision": "approve"}, headers=BOB))
    ok(await c.post(f"{B}/CH-A02", json={**BINDING, "chamber": "CH-A02"}, headers=ALICE), 201)

    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    consumer = LiveConsumer(RunDoneStream(redis))
    await consumer.stream.ensure_group()
    for run_id, end_at, wafer, lot in RUNS:
        await redis.xadd("gde:run:done", {"run_id": run_id, "tool": "ETCH-01", "chamber": "CH-A01",
                                          "recipe": "RCP-0432", "end_at": end_at, "wafer": wafer, "lot": lot})
    for msg_id, f in await consumer.stream.read(block_ms=10):
        await consumer.handle(msg_id, f)


async def values(c: AsyncClient, **params) -> dict:
    return ok(await c.get("/api/values", params={"feature": FEATURE, **params}))


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


async def test_values_other_version_or_unknown_feature(client):
    assert ok(await client.get("/api/values", params={"feature": "gas_flow_diffmean@2.0.0"}))["total"] == 0
    assert ok(await client.get("/api/values", params={"feature": "nope@1.0.0"}))["items"] == []


@pytest.mark.parametrize(
    "params",
    [{"feature": "gas_flow_diffmean"}, {"feature": FEATURE, "page": 0}, {"feature": FEATURE, "page_size": 1001}, {}],
)
async def test_values_bad_params(client, params):
    err(await client.get("/api/values", params=params), 422, "VALIDATION_ERROR")


# ---------------------------------------------------------------- 绑定列表
async def chambers(c: AsyncClient, **params) -> list[str]:
    return [x["chamber"] for x in ok(await c.get(B, params=params))["items"]]


async def test_list_bindings_items(client):
    items = ok(await client.get(B))["items"]
    by = {x["chamber"]: x for x in items}
    assert list(by) == ["CH-A01", "CH-A02"]
    assert by["CH-A01"]["active"]["status"] == "active" and by["CH-A01"]["pending"] is None
    assert by["CH-A02"]["active"] is None and by["CH-A02"]["pending"]["status"] == "pending"
    assert all(x["tool"] == "ETCH-01" for x in items)


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"status": "active"}, ["CH-A01"]),
        ({"status": "pending"}, ["CH-A02"]),
        ({"tool": "ETCH-01"}, ["CH-A01", "CH-A02"]),
        ({"tool": "ETCH-02"}, []),
        ({"chamber": "CH-A02"}, ["CH-A02"]),
        # recipe / feature 只看生效修订：CH-A02 只有待审核，不命中
        ({"recipe": "RCP-0432"}, ["CH-A01"]),
        ({"recipe": "RCP-0433"}, []),
        ({"feature": "gas_flow_diffmean"}, ["CH-A01"]),
        ({"feature": FEATURE}, ["CH-A01"]),
        ({"feature": "gas_flow_diffmean@9.9.9"}, []),
        ({"feature": FEATURE, "recipe": "RCP-0432", "status": "active"}, ["CH-A01"]),
        ({"feature": FEATURE, "status": "pending"}, []),
    ],
)
async def test_list_bindings_filters(client, params, expected):
    assert await chambers(client, **params) == expected


async def test_list_bindings_bad_status(client):
    err(await client.get(B, params={"status": "rejected"}), 422, "VALIDATION_ERROR")


async def test_export_only_active(client):
    doc = ok(await client.get(f"{B}/export"))
    assert [(b["chamber"], b["revision"]) for b in doc["bindings"]] == [("CH-A01", 1)]
    assert ok(await client.get(f"{B}/export", params={"tool": "ETCH-02"}))["bindings"] == []


# ---------------------------------------------------------------- 导入
async def import_(c: AsyncClient, bindings: list[dict], dry_run: bool = True) -> list[dict]:
    doc = {"format": "ei-feature-service.bindings/v1", "bindings": bindings}
    return ok(await c.post(f"{B}/import", params={"dry_run": str(dry_run).lower()}, json=doc, headers=ALICE))["items"]


async def test_import_all_actions(client):
    changed = {**BINDING, "revision": 99, "rules": [{"recipes": ["RCP-0433"], "features": {"gas_flow_diffmean": "1.0.0"}}]}
    result = await import_(client, [
        {"tool": "ETCH-01"},                       # 格式错误
        changed,                                    # new，提示基于旧修订
        {**changed, "revision": None},              # 同一腔室重复出现
        {**BINDING, "chamber": "CH-A02"},           # 已有待审核 → conflict
        {**BINDING, "chamber": "CH-ZZZ"},           # 校验失败
    ])
    assert [x["action"] for x in result] == ["error", "new", "error", "conflict", "error"]
    assert result[0]["message"] == "格式错误" and result[0]["errors"]
    assert result[1]["message"] == "生成待审核修订（文件基于修订 99，当前生效修订 1）" and result[1].get("revision") is None
    assert result[2]["message"] == "文件中重复出现"
    assert result[4]["message"] == "校验失败" and result[4]["errors"]
    assert await chambers(client, status="pending") == ["CH-A02"]  # dry_run 不写库


async def test_import_skip_same_as_active(client):
    result = await import_(client, [{**BINDING, "revision": 1}])
    assert [x["action"] for x in result] == ["skip"] and result[0]["message"]


async def test_import_empty(client):
    assert await import_(client, []) == []


async def test_import_bad_format(client):
    doc = {"format": "other/v1", "bindings": []}
    err(await client.post(f"{B}/import", json=doc, headers=ALICE), 422, "VALIDATION_ERROR")


async def test_import_requires_user(client):
    doc = {"format": "ei-feature-service.bindings/v1", "bindings": []}
    err(await client.post(f"{B}/import", json=doc), 401, "UNAUTHENTICATED")


# ---------------------------------------------------------------- 批次参数校验
def batch_body(**kw) -> dict:
    day = str(today() - timedelta(days=1))
    return {"chamber": "CH-A01", "mode": "fill", "features": [FEATURE], "from": day, "to": day, "reason": "补", **kw}


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
                         "features": ["x@1.0.0", FEATURE]})
    assert await batch_errors(client, body) == {"to", "features"}


async def test_batch_feature_not_in_active_revision(client):
    out = err(await client.post("/api/batches", json=batch_body(features=["b@1", "a@1"]), headers=ALICE),
              422, "VALIDATION_ERROR")
    assert out["details"]["errors"] == [{"path": "features", "message": "不在生效修订 1 中：a@1、b@1"}]


async def test_batch_chamber_without_active_binding(client):
    out = err(await client.post("/api/batches", json=batch_body(chamber="CH-A02"), headers=ALICE), 409, "INVALID_STATE")
    assert out["details"] == {"status": "none"}


async def test_batch_created_fields(client):
    body = batch_body(features=[FEATURE, FEATURE], mode="overwrite", reason="重算")
    b = ok(await client.post("/api/batches", json=body, headers=ALICE), 201)
    assert b["status"] == "queued" and b["mode"] == "overwrite" and b["chamber"] == "CH-A01"
    assert b["features"] == [FEATURE] and b["binding_revision"] == 1 and b["created_by"] == "alice"
    assert ok(await client.get(f"/api/batches/{b['batch_id']}"))["batch_id"] == b["batch_id"]


@pytest.mark.parametrize("body", [batch_body(features=[]), batch_body(mode="live"), batch_body(reason="")])
async def test_batch_schema_errors(client, body):
    err(await client.post("/api/batches", json=body, headers=ALICE), 422, "VALIDATION_ERROR")


async def test_batch_not_found(client):
    err(await client.get("/api/batches/999999"), 404, "NOT_FOUND")
    err(await client.post("/api/batches/999999/cancel", headers=ALICE), 404, "NOT_FOUND")


async def test_batch_list_filters(client):
    page = ok(await client.get("/api/batches", params={"chamber": "CH-A01", "status": "queued", "page_size": 1}))
    assert page["total"] >= 2 and len(page["items"]) == 1
    assert ok(await client.get("/api/batches", params={"chamber": "CH-ZZZ"}))["total"] == 0
