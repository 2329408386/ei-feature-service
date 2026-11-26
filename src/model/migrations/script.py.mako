"""${message}

修订号：${up_revision}
上一版：${down_revision | comma,n}
创建于：${create_date}
"""

from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    """升级：写原生 SQL，如 op.execute("ALTER TABLE ... ADD COLUMN ...")；多条语句或带冒号的注释用 op.get_bind().exec_driver_sql(sql)。"""
    raise NotImplementedError


def downgrade() -> None:
    """降级：撤销 upgrade 的全部改动。"""
    raise NotImplementedError
