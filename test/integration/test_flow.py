"""端到端：新增特征 → 评审 → 新增算子 → 试跑 → 评审 → 按算子绑定 → 实时计算 → 补算 / 重算 → 查询 → 下线。

使用 config/config.json 中的 PostgreSQL（连不上就跳过），Redis 用 fakeredis，GDE 用 FakeGdeClient。
"""

from datetime import timedelta

import fakeredis
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from main import app
from src.engine import batch_runner
from src.engine.live import LiveConsumer
from src.util import db
from src.util.gde import get_gde
from src.util.stream import RunDoneStream
from src.util.timeutil import today
from test.conftest import load_sample

TABLES = ("feature_value, feature_batch, recipe_subscription, binding_item, operator_binding, operator_meta, operator, "
          "feature_meta, feature")
F = "/api/features/gas_flow_diffmean/1.0.0"
O = "/api/operators/gas_flow_diffmean/diffmean"
B = "/api/bindings/gas_flow_diffmean/diffmean"
REF = "gas_flow_diffmean/diffmean@1"
ALICE, BOB = {"X-User": "alice"}, {"X-User": "bob"}
RUN = "RUN-CH-A01-202611240815"


def chambers(*pairs) -> list[dict]:
    return [{"tool": t, "chamber": c} for t, c in pairs]


BINDING = {"feature_id": "gas_flow_diffmean", "operator": "diffmean", "version": 1, "recipes": ["RCP-0432"]}


async def reset() -> None:
    db.configure()
    await db.init_schema()
    async with db.session() as s:
        await s.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))


@pytest.fixture(scope="module")
async def client():
    try:
        await reset()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"PostgreSQL 不可用：{e}")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await db.dispose()


def ok(resp, status: int = 200) -> dict:
    assert resp.status_code == status, resp.text
    return resp.json() if resp.content else {}


def err(resp, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    assert resp.json()["code"] == code, resp.text
    return resp.json()


async def publish_feature(c: AsyncClient, spec: dict) -> None:
    ok(await c.post("/api/features", json=spec, headers=ALICE), 201)
    path = f"/api/features/{spec['id']}/{spec['version']}"
    ok(await c.post(path + "/submit", headers=ALICE))
    ok(await c.post(path + "/review", json={"decision": "approve"}, headers=BOB))


async def test_feature_lifecycle(client):
    feature, _, _ = load_sample()
    err(await client.post("/api/features", json=feature), 401, "UNAUTHENTICATED")
    err(await client.post("/api/features", json=feature, headers={"X-User": "system"}), 403, "FORBIDDEN")
    assert ok(await client.post("/api/features", json=feature, headers=ALICE), 201)["status"] == "draft"
    err(await client.post("/api/features", json=feature, headers=ALICE), 409, "VERSION_EXISTS")
    err(await client.post("/api/features", json={**feature, "version": "0.9.0"}, headers=ALICE),
        409, "VERSION_NOT_INCREASING")
    bad = err(await client.post("/api/features", json={"id": "X"}, headers=ALICE), 422, "SPEC_INVALID")
    assert len(bad["details"]["errors"]) == 3

    detail = ok(await client.get(F))
    assert detail["unit"] == "sccm" and detail["operators"] == []

    err(await client.post(F + "/submit", headers=BOB), 403, "FORBIDDEN")
    assert ok(await client.post(F + "/submit", headers=ALICE))["status"] == "review"
    err(await client.post(F + "/review", json={"decision": "approve"}, headers=ALICE), 403, "FORBIDDEN")
    err(await client.post(F + "/review", json={"decision": "reject"}, headers=BOB), 422, "VALIDATION_ERROR")
    assert ok(await client.post(F + "/review", json={"decision": "approve"}, headers=BOB))["status"] == "published"

    page = ok(await client.get("/api/features", params={"q": "气体"}))
    assert page["total"] == 1


async def test_operator_lifecycle(client):
    _, spec, code = load_sample()
    body = {"feature_id": "gas_flow_diffmean", "spec": spec, "code": code}
    err(await client.post("/api/operators", json={**body, "feature_id": "nope_feature"}, headers=ALICE),
        404, "NOT_FOUND")
    bad = {**body, "code": "import os\n", "code_file": "bad-name.py"}
    errors = err(await client.post("/api/operators", json=bad, headers=ALICE), 422, "SPEC_INVALID")["details"]["errors"]
    assert {e["file"] for e in errors} == {"code_file", "bad-name.py"}

    op = ok(await client.post("/api/operators", json=body, headers=ALICE), 201)
    assert (op["version"], op["code_file"], op["ref"], op["status"]) == (1, "diffmean_v1.py", REF, "draft")
    assert ok(await client.get(F))["version"] == "1.0.0"  # 新增算子不改变特征版本

    detail = ok(await client.get(O + "/1"))
    assert detail["code"].startswith('"""示例特征') and detail["description"].startswith("主刻蚀")

    err(await client.post(O + "/1/submit", headers=BOB), 403, "FORBIDDEN")
    assert ok(await client.post(O + "/1/submit", headers=ALICE))["status"] == "review"
    trial = ok(await client.post(O + "/1/trial", json={"run_id": RUN}))
    assert trial["reason"] is None and trial["run"]["chamber"] == "CH-A01"
    err(await client.post(O + "/1/trial", json={"run_id": "RUN-MISSING-1"}), 404, "NOT_FOUND")

    err(await client.post(O + "/1/review", json={"decision": "approve"}, headers=ALICE), 403, "FORBIDDEN")
    err(await client.post(O + "/1/review", json={"decision": "approve"}, headers=BOB), 422, "TRIAL_REQUIRED")
    approve = {"decision": "approve", "trial_runs": [RUN]}
    assert ok(await client.post(O + "/1/review", json=approve, headers=BOB))["status"] == "published"

    # 同名算子再次提交 → 版本 2；指定文件名；重复的文件名被拒绝
    v2 = ok(await client.post("/api/operators", json={**body, "code_file": "diffmean_fast.py"}, headers=ALICE), 201)
    assert (v2["version"], v2["code_file"]) == (2, "diffmean_fast.py")
    err(await client.post("/api/operators", json={**body, "code_file": "diffmean_fast.py"}, headers=ALICE),
        409, "CODE_FILE_EXISTS")
    ok(await client.delete(O + "/2", headers=ALICE), 204)

    # 同一特征下的第二个算子
    other = {**body, "spec": {"name": "diffmax", "desc": "偏差最大值"}}
    assert ok(await client.post("/api/operators", json=other, headers=ALICE), 201)["ref"] == "gas_flow_diffmean/diffmax@1"
    detail = ok(await client.get(F))
    assert [(o["name"], o["version"]) for o in detail["operators"]] == [("diffmax", 1), ("diffmean", 1)]
    listed = ok(await client.get("/api/operators", params={"feature_id": "gas_flow_diffmean", "status": "published"}))
    assert [o["ref"] for o in listed["items"]] == [REF]


async def test_operator_needs_published_feature(client):
    _, spec, code = load_sample()
    ok(await client.post("/api/features", json={"id": "draft_only", "version": "1.0.0", "desc": "x"}, headers=ALICE), 201)
    path = "/api/operators/draft_only/diffmean/1"
    ok(await client.post("/api/operators", json={"feature_id": "draft_only", "spec": spec, "code": code},
                         headers=ALICE), 201)
    ok(await client.post(path + "/submit", headers=ALICE))
    out = err(await client.post(path + "/review", json={"decision": "approve", "trial_runs": [RUN]}, headers=BOB),
              409, "INVALID_STATE")
    assert out["details"] == {"status": "unpublished"}


async def test_gde_metadata(client):
    assert ok(await client.get("/api/gde/fleets")) == ["FLEET-ETCH-A", "FLEET-ETCH-B"]
    assert ok(await client.get("/api/gde/fleets/FLEET-ETCH-A/chambers")) == chambers(("ETCH-01", "CH-A01"),
                                                                                     ("ETCH-01", "CH-A02"))
    err(await client.get("/api/gde/fleets/NOPE/chambers"), 404, "NOT_FOUND")
    assert ok(await client.get("/api/gde/recipes", params={"q": "rcp-04"})) == ["RCP-0432", "RCP-0433"]
    assert len(ok(await client.get("/api/gde/recipes", params={"limit": 2}))) == 2
    assert ok(await client.get("/api/gde/recipes/RCP-0432/chambers")) == chambers(("ETCH-01", "CH-A01"),
                                                                                  ("ETCH-01", "CH-A02"))
    err(await client.get("/api/gde/recipes/RCP-9999/chambers"), 404, "NOT_FOUND")


def sched(rows: list[dict]) -> dict[str, dict]:
    return {x["recipe"]: x for x in rows}


async def test_binding_flow(client):
    bad = {**BINDING, "version": 9, "recipes": ["RCP-9999", "RCP-0432", "RCP-0432"]}
    errors = err(await client.post(B, json=bad, headers=ALICE), 422, "BINDING_INVALID")["details"]["errors"]
    assert {e["path"] for e in errors} == {"version", "recipes[0]", "recipes[2]"}
    err(await client.post("/api/bindings/gas_flow_diffmean/diffmax", json=BINDING, headers=ALICE),
        422, "BINDING_INVALID")  # 路径与 body 不一致

    rev = ok(await client.post(B, json=BINDING, headers={**ALICE, "X-Client": "console"}), 201)
    assert rev["status"] == "pending" and rev["source"] == "console" and rev["spec"]["recipes"] == ["RCP-0432"]
    err(await client.post(B, json=BINDING, headers=ALICE), 409, "PENDING_EXISTS")

    review = f"{B}/revisions/{rev['revision']}/review"
    err(await client.post(review, json={"decision": "approve"}, headers=ALICE), 403, "FORBIDDEN")
    active = ok(await client.post(review, json={"decision": "approve"}, headers=BOB))
    assert active["status"] == "active"

    # 调度以 recipe 为键：一个 GDE 订阅覆盖跑过该 recipe 的全部机台 / 腔室
    rows = ok(await client.get("/api/schedules"))
    assert [(x["recipe"], x["synced"], x["status"], x["operators"]) for x in rows] == [
        ("RCP-0432", True, "running", [REF])]
    assert [(c["tool"], c["chamber"]) for c in rows[0]["chambers"]] == [("ETCH-01", "CH-A01"), ("ETCH-01", "CH-A02")]
    assert get_gde().subs[rows[0]["gde_sub_id"]]["recipe"] == "RCP-0432"
    assert ok(await client.get("/api/schedules", params={"chamber": "CH-A02"}))[0]["recipe"] == "RCP-0432"
    assert ok(await client.get("/api/schedules", params={"tool": "ETCH-02"})) == []

    items = ok(await client.get("/api/bindings", params={"recipe": "RCP-0432"}))["items"]
    assert [(x["feature_id"], x["operator"]) for x in items] == [("gas_flow_diffmean", "diffmean")]
    assert ok(await client.get("/api/bindings", params={"recipe": "RCP-0500"}))["items"] == []

    doc = ok(await client.get("/api/bindings/export"))
    assert doc["format"] == "ei-feature-service.bindings/v3"
    result = ok(await client.post("/api/bindings/import", json=doc, headers=ALICE))["items"]
    assert [x["action"] for x in result] == ["skip"]  # 与生效修订相同

    # 新修订：多加一个 recipe → 多一个 recipe 订阅
    wider = {**BINDING, "recipes": ["RCP-0433", "RCP-0432"], "base_revision": 1}
    rev2 = ok(await client.post(B, json=wider, headers=ALICE), 201)
    assert rev2["spec"]["recipes"] == ["RCP-0432", "RCP-0433"]  # 规范化排序
    ok(await client.post(f"{B}/revisions/{rev2['revision']}/review", json={"decision": "approve"}, headers=BOB))
    rows = sched(ok(await client.get("/api/schedules")))
    assert list(rows) == ["RCP-0432", "RCP-0433"] and rows["RCP-0433"]["synced"]
    history = ok(await client.get(B))["revisions"]
    assert [(r["revision"], r["status"]) for r in history] == [(2, "active"), (1, "superseded")]

    # 撤回
    rev3 = ok(await client.post(B, json=BINDING, headers=ALICE), 201)
    err(await client.post(f"{B}/revisions/{rev3['revision']}/withdraw", headers=BOB), 403, "FORBIDDEN")
    assert ok(await client.post(f"{B}/revisions/{rev3['revision']}/withdraw", headers=ALICE))["status"] == "withdrawn"

    ok(await client.post("/api/schedules/RCP-0432/pause", headers=ALICE), 204)
    assert sched(ok(await client.get("/api/schedules")))["RCP-0432"]["status"] == "paused"
    ok(await client.post("/api/schedules/RCP-0432/resume", headers=ALICE), 204)
    err(await client.post("/api/schedules/RCP-0500/pause", headers=ALICE), 404, "NOT_FOUND")


async def test_subscription_retry(client):
    """GDE 不可用时绑定照样生效，订阅留待后台任务同步。"""
    from src.service import subscription
    from src.util.gde import GdeUnavailable

    gde = get_gde()
    original = gde.cancel_subscription
    sub_0433 = sched(ok(await client.get("/api/schedules")))["RCP-0433"]["gde_sub_id"]

    def down(*_):
        raise GdeUnavailable("down")

    gde.cancel_subscription = down
    try:  # 回到只绑 RCP-0432：RCP-0433 需要取消订阅，但 GDE 不可用
        rev = ok(await client.post(B, json=BINDING, headers=ALICE), 201)
        ok(await client.post(f"{B}/revisions/{rev['revision']}/review", json={"decision": "approve"}, headers=BOB))
        s = sched(ok(await client.get("/api/schedules")))
        assert s["RCP-0433"]["synced"] is False and s["RCP-0433"]["wanted"] is False
        assert "down" in s["RCP-0433"]["last_error"]
    finally:
        gde.cancel_subscription = original
    await subscription.sync()
    s = sched(ok(await client.get("/api/schedules")))
    assert list(s) == ["RCP-0432"] and s["RCP-0432"]["synced"]  # RCP-0433 不再需要订阅
    assert gde.subscription_status(sub_0433) == "cancelled"


async def test_live(client):
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    consumer = LiveConsumer(RunDoneStream(redis))
    await consumer.stream.ensure_group()
    fields = {"run_id": "RUN-LIVE-1", "tool": "ETCH-01", "chamber": "CH-A01", "recipe": "RCP-0432",
              "end_at": "2026-11-24T08:15:00+08:00", "wafer": "W01"}
    for _ in range(2):  # 重复通知只算一次
        await redis.xadd("gde:run:done", fields)
    await redis.xadd("gde:run:done", {**fields, "run_id": "RUN-LIVE-2", "recipe": "RCP-0500"})  # 不在绑定内
    for msg_id, f in await consumer.stream.read(block_ms=10):
        await consumer.handle(msg_id, f)
    assert (await redis.xpending("gde:run:done", "ei-feature-service"))["pending"] == 0

    values = ok(await client.get("/api/values", params={"operator": REF}))
    assert values["total"] == 1 and values["items"][0]["value"] is not None
    assert values["items"][0]["operator"] == "diffmean" and values["items"][0]["version"] == 1
    batch = ok(await client.get("/api/batches", params={"mode": "live"}))["items"][0]
    assert batch["run_total"] == 1 and batch["value_ok"] == 1 and batch["operators"] is None
    assert batch["recipe"] == "RCP-0432"

    # 绑定里没有选腔室：同一 recipe 在另一个腔室（甚至新腔室）上的执行也会计算
    await redis.xadd("gde:run:done", {**fields, "run_id": "RUN-LIVE-3", "chamber": "CH-NEW", "tool": "ETCH-09"})
    for msg_id, f in await consumer.stream.read(block_ms=10):
        await consumer.handle(msg_id, f)
    values = ok(await client.get("/api/values", params={"operator": REF, "chamber": "CH-NEW"}))
    assert values["total"] == 1 and values["items"][0]["tool"] == "ETCH-09"
    sched_row = sched(ok(await client.get("/api/schedules")))["RCP-0432"]
    assert sched_row["progress"] is not None


async def test_fill_and_overwrite(client):
    day = str(today() - timedelta(days=1))
    body = {"recipe": "RCP-0432", "mode": "fill", "operators": [REF], "from": day, "to": day, "reason": "补历史"}
    err(await client.post("/api/batches", json={**body, "operators": ["x_x/y_y@1"]}, headers=ALICE),
        422, "VALIDATION_ERROR")
    fill = ok(await client.post("/api/batches", json=body, headers=ALICE), 201)
    assert fill["status"] == "queued" and fill["recipe"] == "RCP-0432" and fill["operators"] == [REF]

    await batch_runner.execute(await batch_runner.claim_next())
    fill = ok(await client.get(f"/api/batches/{fill['batch_id']}"))
    assert fill["status"] == "done" and fill["run_total"] > 0
    assert fill["value_ok"] + fill["value_null"] == fill["run_total"]
    # 补算覆盖该 recipe 在全部腔室上的执行
    chambers_done = {x["chamber"] for x in ok(await client.get(
        "/api/values", params={"operator": REF, "from": day, "to": day, "page_size": 1000}))["items"]}
    assert chambers_done == {"CH-A01", "CH-A02"}

    over = ok(await client.post("/api/batches", json={**body, "mode": "overwrite", "reason": "重算"}, headers=ALICE), 201)
    await batch_runner.execute(await batch_runner.claim_next())
    assert ok(await client.get(f"/api/batches/{over['batch_id']}"))["status"] == "done"
    assert ok(await client.get(f"/api/batches/{fill['batch_id']}"))["superseded_by"] == over["batch_id"]

    queued = ok(await client.post("/api/batches", json=body, headers=ALICE), 201)
    assert ok(await client.post(f"/api/batches/{queued['batch_id']}/cancel", headers=ALICE))["status"] == "cancelled"
    err(await client.post(f"/api/batches/{queued['batch_id']}/cancel", headers=ALICE), 409, "INVALID_STATE")


async def test_retire_operator(client):
    sub_id = sched(ok(await client.get("/api/schedules")))["RCP-0432"]["gde_sub_id"]
    out = ok(await client.post(O + "/1/retire", json={"reason": "算法升级"}, headers=BOB))
    assert out["operator"]["status"] == "retired"
    assert out["affected"] == [{"feature_id": "gas_flow_diffmean", "operator": "diffmean", "version": 1,
                                "new_revision": 5, "recipes": ["RCP-0432"]}]
    assert get_gde().subscription_status(sub_id) == "cancelled"  # recipe 没有算子要算了，订阅取消
    assert ok(await client.get("/api/schedules")) == []

    latest = ok(await client.get(B))["revisions"][0]
    assert latest["source"] == "system" and latest["status"] == "active" and latest["spec"]["recipes"] == []
    values = ok(await client.get("/api/values", params={"operator": REF}))
    assert values["items"][0]["retired"] is True


async def test_new_feature_version_and_retire_feature(client):
    feature, _, code = load_sample()
    # 新版本发布后旧版本自动下线，算子不受影响
    await publish_feature(client, {**feature, "version": "1.1.0", "desc": "新描述"})
    old = ok(await client.get(F))
    assert old["status"] == "retired" and old["retire_reason"] == "被 1.1.0 替代"
    assert [o["ref"] for o in old["operators"] if o["status"] == "published"] == []

    # 发布一个新算子并绑定，再整体下线特征：算子随之下线，绑定被清空
    body = {"feature_id": "gas_flow_diffmean", "spec": {"name": "diffmax", "desc": "偏差最大值"}, "code": code}
    ok(await client.post("/api/operators/gas_flow_diffmean/diffmax/1/submit", headers=ALICE))  # 已在前面创建
    ok(await client.post("/api/operators/gas_flow_diffmean/diffmax/1/review",
                         json={"decision": "approve", "trial_runs": [RUN]}, headers=BOB))
    b = "/api/bindings/gas_flow_diffmean/diffmax"
    rev = ok(await client.post(b, json={**BINDING, "operator": "diffmax"}, headers=ALICE), 201)
    ok(await client.post(f"{b}/revisions/{rev['revision']}/review", json={"decision": "approve"}, headers=BOB))
    out = ok(await client.post("/api/features/gas_flow_diffmean/1.1.0/retire", json={"reason": "停用"}, headers=BOB))
    assert out["feature"]["status"] == "retired" and out["operators"] == ["gas_flow_diffmean/diffmax@1"]
    assert [a["recipes"] for a in out["affected"]] == [["RCP-0432"]]
    err(await client.post("/api/operators", json=body, headers=ALICE), 409, "INVALID_STATE")
