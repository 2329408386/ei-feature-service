#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""统一错误：业务代码抛 AppError，由 install() 注册的处理器转成 JSON。"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    def __init__(self, code: str, status: int, message: str, details: dict | None = None):
        super().__init__(message)
        self.code, self.status, self.message, self.details = code, status, message, details or {}


# 常用错误的快捷构造，保持调用处简短
def unauthenticated() -> AppError:
    """401：缺少 X-User 请求头。"""
    return AppError("UNAUTHENTICATED", 401, "缺少 X-User 请求头")


def forbidden(message: str) -> AppError:
    """403：无权限。"""
    return AppError("FORBIDDEN", 403, message)


def not_found(what: str) -> AppError:
    """404：资源不存在。"""
    return AppError("NOT_FOUND", 404, f"{what} 不存在")


def invalid_state(message: str, status: str) -> AppError:
    """409：当前状态不允许该操作，details 带上当前状态。"""
    return AppError("INVALID_STATE", 409, message, {"status": status})


def conflict(code: str, message: str) -> AppError:
    """409：与已有数据冲突，如 VERSION_EXISTS。"""
    return AppError(code, 409, message)


def invalid(code: str, message: str, errors: list[dict] | None = None) -> AppError:
    """422：内容校验失败，errors 为全部错误明细。"""
    return AppError(code, 422, message, {"errors": errors} if errors is not None else None)


def gde_unavailable(message: str) -> AppError:
    """502：GDE 调用失败。"""
    return AppError("GDE_UNAVAILABLE", 502, f"GDE 调用失败：{message}")


def error_list(e) -> list[dict]:
    """把 pydantic / FastAPI 的校验错误转成 [{path, message}]。"""
    return [{"path": ".".join(map(str, x["loc"])), "message": x["msg"]} for x in e.errors()]


def _body(code: str, message: str, details: dict) -> dict:
    return {"code": code, "message": message, "details": details}


def install(app: FastAPI) -> None:
    """给 app 注册 AppError 和参数校验错误的处理器。"""
    @app.exception_handler(AppError)
    async def _app_error(_: Request, e: AppError):
        return JSONResponse(_body(e.code, e.message, e.details), status_code=e.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError):
        return JSONResponse(_body("VALIDATION_ERROR", "参数错误", {"errors": error_list(e)}), status_code=422)
