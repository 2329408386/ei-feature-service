"""迁移往返：在临时库上 base → head → base → head，保证每个迁移的 upgrade / downgrade 都能执行。

使用 config/config.json 中的 PostgreSQL 服务器（连不上就跳过），临时库测试结束后删除，不影响开发库。
"""

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from src.util import db
from src.util.config import settings

TEMP_DB = "ei_feature_migration_test"
TABLES = {"feature", "operator", "feature_meta", "operator_meta", "operator_binding", "binding_item",
          "recipe_subscription", "feature_batch", "feature_value"}


@pytest.fixture
def temp_url():
    url = make_url(settings.database.url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {TEMP_DB}"))
            conn.execute(text(f"CREATE DATABASE {TEMP_DB}"))
    except Exception as e:  # noqa: BLE001
        admin.dispose()
        pytest.skip(f"PostgreSQL 不可用：{e}")
    yield url.set(database=TEMP_DB).render_as_string(hide_password=False)
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEMP_DB} WITH (FORCE)"))
    admin.dispose()


def state(url: str) -> tuple[set[str], str | None]:
    """(业务表集合, 当前版本)。"""
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            tables = set(inspect(conn).get_table_names()) - {"alembic_version"}
            version = None
            if inspect(conn).has_table("alembic_version"):
                version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        return tables, version
    finally:
        engine.dispose()


def test_upgrade_downgrade_roundtrip(temp_url):
    cfg = db.alembic_config()
    cfg.attributes["url"] = temp_url
    head = ScriptDirectory.from_config(cfg).get_current_head()
    for _ in range(2):
        command.upgrade(cfg, "head")
        tables, version = state(temp_url)
        assert TABLES <= tables and version == head
        command.downgrade(cfg, "base")
        assert state(temp_url) == (set(), None)
    command.upgrade(cfg, "head")
    assert state(temp_url)[1] == head
