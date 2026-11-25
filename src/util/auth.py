#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""当前用户：网关注入 X-User 请求头（工号）。"""

from typing import Annotated

from fastapi import Depends, Header

from src.util.errors import forbidden, unauthenticated

SYSTEM = "system"  # 服务自己执行的操作用这个用户名，外部请求不能使用


def _current_user(x_user: Annotated[str | None, Header()] = None) -> str:
    user = (x_user or "").strip()
    if not user:
        raise unauthenticated()
    if user == SYSTEM:
        raise forbidden("用户名 system 为系统保留")
    return user


User = Annotated[str, Depends(_current_user)]
