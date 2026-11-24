"""端到端：上传 → 评审 → 绑定 → 实时计算 → 补算 / 重算 → 查询 → 下线。

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

TABLES = "gde_outbox, feature_value, feature_batch, binding_item, chamber_binding, feature"
F = "/api/features/gas_flow_diffmean/1.0.0"
B = "/api/chamber-bindings"
ALICE, BOB = {"X-User": "alice"}, {"X-User": "bob"}
BINDING = {"tool": "ETCH-01", "chamber": "CH-A01", "rules": [{"recipes": ["RCP-0432"], "features": {"gas_flow_diffmean": "1.0.0"}}]}


@pytest.fixture(scope="module")
async def client():
    db.configure()
    try:
        await db.init_schema()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"PostgreSQL 不可用：{e}")
    async with db.session() as s:
        await s.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
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


async def test_feature_lifecycle(client, sample_package):
    spec, code = sample_package
    files = {"spec": ("feature.json", spec), "code": ("compute.py", code)}
    err(await client.post("/api/features", files=files), 401, "UNAUTHENTICATED")
    err(await client.post("/api/features", files=files, headers={"X-User": "system"}), 403, "FORBIDDEN")
    assert ok(await client.post("/api/features", files=files, headers=ALICE), 201)["status"] == "draft"
    err(await client.post("/api/features", files=files, headers=ALICE), 409, "VERSION_EXISTS")
    bad = {"spec": ("feature.json", spec), "code": ("compute.py", b"import os\n")}
    assert err(await client.post("/api/features", files=bad, headers=ALICE), 422, "PACKAGE_INVALID")["details"]["errors"]

    detail = ok(await client.get(F))
    assert detail["code"].startswith('"""示例特征') and detail["unit"] == "sccm"

    err(await client.post(F + "/submit", headers=BOB), 403, "FORBIDDEN")
    assert ok(await client.post(F + "/submit", headers=ALICE))["status"] == "review"

    trial = ok(await client.post(F + "/trial", json={"run_id": "RUN-CH-A01-202611240815"}))
    assert trial["reason"] is None and trial["run"]["chamber"] == "CH-A01"
    err(await client.post(F + "/trial", json={"run_id": "RUN-MISSING-1"}), 404, "NOT_FOUND")

    err(await client.post(F + "/review", json={"decision": "approve"}, headers=ALICE), 403, "FORBIDDEN")
    err(await client.post(F + "/review", json={"decision": "approve"}, headers=BOB), 422, "TRIAL_REQUIRED")
    body = {"decision": "approve", "trial_runs": ["RUN-CH-A01-202611240815"]}
    assert ok(await client.post(F + "/review", json=body, headers=BOB))["status"] == "published"

    page = ok(await client.get("/api/features", params={"q": "气体"}))
    assert page["total"] == 1


async def test_binding_flow(client):
    bad = {**BINDING, "rules": [{"recipes": ["RCP-9999"], "features": {"nope": "1.0.0"}}]}
    errors = err(await client.post(f"{B}/CH-A01", json=bad, headers=ALICE), 422, "BINDING_INVALID")["details"]["errors"]
    assert {e["path"] for e in errors} == {"rules[0].recipes[0]", "rules[0].features.nope"}

    rev = ok(await client.post(f"{B}/CH-A01", json=BINDING, headers={**ALICE, "X-Client": "console"}), 201)
    assert rev["status"] == "pending" and rev["source"] == "console"
    err(await client.post(f"{B}/CH-A01", json=BINDING, headers=ALICE), 409, "PENDING_EXISTS")

    review = f"{B}/CH-A01/revisions/{rev['revision']}/review"
    err(await client.post(review, json={"decision": "approve"}, headers=ALICE), 403, "FORBIDDEN")
    active = ok(await client.post(review, json={"decision": "approve"}, headers=BOB))
    assert active["status"] == "active" and active["gde_sub_id"]

    items = ok(await client.get(B, params={"feature": "gas_flow_diffmean@1.0.0"}))["items"]
    assert [x["chamber"] for x in items] == ["CH-A01"]
    assert ok(await client.get(B, params={"recipe": "RCP-0500"}))["items"] == []

    doc = ok(await client.get(f"{B}/export"))
    result = ok(await client.post(f"{B}/import", json=doc, headers=ALICE))["items"]
    assert [x["action"] for x in result] == ["skip"]  # 与生效修订相同

    new = {**doc, "bindings": [{**BINDING, "chamber": "CH-A02"}, {**BINDING, "chamber": "CH-ZZZ"}]}
    result = ok(await client.post(f"{B}/import", params={"dry_run": "false"}, json=new, headers=ALICE))["items"]
    assert [x["action"] for x in result] == ["new", "error"] and result[0]["revision"] == 1
    withdraw = f"{B}/CH-A02/revisions/1/withdraw"
    assert ok(await client.post(withdraw, headers=ALICE))["status"] == "withdrawn"

    schedules = ok(await client.get("/api/schedules"))
    assert schedules[0]["status"] == "running" and schedules[0]["feature_count"] == 1
    ok(await client.post("/api/schedules/CH-A01/pause", headers=ALICE), 204)
    assert ok(await client.get("/api/schedules"))[0]["status"] == "paused"
    ok(await client.post("/api/schedules/CH-A01/resume", headers=ALICE), 204)


async def test_live(client):
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    consumer = LiveConsumer(RunDoneStream(redis))
    await consumer.stream.ensure_group()
    fields = {"run_id": "RUN-LIVE-1", "tool": "ETCH-01", "chamber": "CH-A01", "recipe": "RCP-0432",
              "end_at": "2026-11-24T08:15:00+08:00", "wafer": "W01"}
    for _ in range(2):  # 重复通知只算一次
        await redis.xadd("gde:run:done", fields)
    for msg_id, f in await consumer.stream.read(block_ms=10):
        await consumer.handle(msg_id, f)
    assert (await redis.xpending("gde:run:done", "ei-feature-service"))["pending"] == 0

    values = ok(await client.get("/api/values", params={"feature": "gas_flow_diffmean@1.0.0"}))
    assert values["total"] == 1 and values["items"][0]["value"] is not None
    batch = ok(await client.get("/api/batches", params={"mode": "live"}))["items"][0]
    assert batch["run_total"] == 1 and batch["value_ok"] == 1


async def test_fill_and_overwrite(client):
    day = str(today() - timedelta(days=1))
    body = {"chamber": "CH-A01", "mode": "fill", "features": ["gas_flow_diffmean@1.0.0"], "from": day, "to": day, "reason": "补历史"}
    err(await client.post("/api/batches", json={**body, "features": ["x@1.0.0"]}, headers=ALICE), 422, "VALIDATION_ERROR")
    fill = ok(await client.post("/api/batches", json=body, headers=ALICE), 201)
    assert fill["status"] == "queued"

    await batch_runner.execute(await batch_runner.claim_next())
    fill = ok(await client.get(f"/api/batches/{fill['batch_id']}"))
    assert fill["status"] == "done" and fill["run_total"] > 0
    assert fill["value_ok"] + fill["value_null"] == fill["run_total"]

    over = ok(await client.post("/api/batches", json={**body, "mode": "overwrite", "reason": "重算"}, headers=ALICE), 201)
    await batch_runner.execute(await batch_runner.claim_next())
    assert ok(await client.get(f"/api/batches/{over['batch_id']}"))["status"] == "done"
    assert ok(await client.get(f"/api/batches/{fill['batch_id']}"))["superseded_by"] == over["batch_id"]

    queued = ok(await client.post("/api/batches", json=body, headers=ALICE), 201)
    assert ok(await client.post(f"/api/batches/{queued['batch_id']}/cancel", headers=ALICE))["status"] == "cancelled"
    err(await client.post(f"/api/batches/{queued['batch_id']}/cancel", headers=ALICE), 409, "INVALID_STATE")


async def test_retire(client):
    sub_id = ok(await client.get("/api/schedules"))[0]["gde_sub_id"]
    out = ok(await client.post(F + "/retire", json={"reason": "算法升级"}, headers=BOB))
    assert out["feature"]["status"] == "retired"
    assert out["affected"] == [{"chamber": "CH-A01", "new_revision": 2, "subscription": "cancelled"}]
    assert get_gde().subscription_status(sub_id) == "cancelled"  # outbox 已执行

    latest = ok(await client.get(f"{B}/CH-A01"))["revisions"][0]
    assert latest["source"] == "system" and latest["status"] == "active" and latest["spec"]["rules"] == []
    values = ok(await client.get("/api/values", params={"feature": "gas_flow_diffmean@1.0.0"}))
    assert values["items"][0]["retired"] is True
