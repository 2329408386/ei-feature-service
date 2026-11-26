#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""${message}

修订号：${up_revision}
上一版：${down_revision | comma,n}
创建于：${create_date}
"""

from alembic import op

REVISION = ${repr(up_revision)}
DOWN_REVISION = ${repr(down_revision)}

# Alembic 按固定的小写模块属性（revision / down_revision / branch_labels / depends_on）读取修订信息
globals().update(
    revision=REVISION, down_revision=DOWN_REVISION, branch_labels=${repr(branch_labels)}, depends_on=${repr(depends_on)}
)


def upgrade() -> None:
    """升级：写原生 SQL，如 op.execute("ALTER TABLE ... ADD COLUMN ...")；多条语句或带冒号的注释用 op.get_bind().exec_driver_sql(sql)。"""
    raise NotImplementedError


def downgrade() -> None:
    """降级：撤销 upgrade 的全部改动。"""
    raise NotImplementedError
