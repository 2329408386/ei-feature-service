#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""特征包上传校验：feature.json 格式 + compute.py 静态检查。返回全部错误，而不是遇到第一个就停。"""

import ast
import json
import re

from src.util import semver

ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
CODE_MAX_BYTES = 256 * 1024
ALLOWED_IMPORTS = {
    "eifeature", "math", "statistics", "numpy", "typing", "dataclasses", "functools", "itertools", "collections",
}
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "input", "breakpoint"}


def _err(file: str, message: str, line: int | None = None) -> dict:
    return {"file": file, "line": line, "message": message}


def check_spec(raw: bytes) -> tuple[dict | None, list[dict]]:
    """校验 feature.json，返回 (解析结果, 错误列表)；无法解析时解析结果为 None。"""
    f = "feature.json"
    try:
        spec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return None, [_err(f, f"不是合法的 UTF-8 JSON：{e}")]
    if not isinstance(spec, dict):
        return None, [_err(f, "顶层必须是对象")]

    errors = []
    if not ID.match(str(spec.get("id", ""))):
        errors.append(_err(f, "id 必须是小写字母开头，由小写字母、数字、下划线组成，3~64 位"))
    if not semver.is_valid(str(spec.get("version", ""))):
        errors.append(_err(f, "version 必须形如 1.0.0"))
    desc = spec.get("desc")
    if not isinstance(desc, str) or not 1 <= len(desc.strip()) <= 200:
        errors.append(_err(f, "desc 必填，1~200 字"))
    unit = (spec.get("output") or {}).get("unit") if isinstance(spec.get("output"), dict) else None
    if unit is not None and (not isinstance(unit, str) or len(unit) > 32):
        errors.append(_err(f, "output.unit 必须是不超过 32 字的字符串"))
    return spec, errors


def check_code(raw: bytes) -> list[dict]:
    """静态检查 compute.py：顶层 fetch / compute 签名、导入白名单、禁用的内置函数和双下划线属性。"""
    f = "compute.py"
    if len(raw) > CODE_MAX_BYTES:
        return [_err(f, "文件超过 256 KB")]
    try:
        tree = ast.parse(raw.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError) as e:
        return [_err(f, f"无法解析：{e}", getattr(e, "lineno", None))]
    errors = _check_entrypoints(tree)
    for node in ast.walk(tree):
        errors += _check_node(node)
    return errors


def _single_positional(fn: ast.FunctionDef) -> bool:
    """恰好一个位置参数，且没有 *args、仅限关键字参数、**kwargs。"""
    a = fn.args
    if len(a.posonlyargs) + len(a.args) != 1:
        return False
    return not (a.vararg or a.kwonlyargs or a.kwarg)


def _check_entrypoints(tree: ast.Module) -> list[dict]:
    """模块顶层必须有 fetch(run_id) 和 compute(data)。"""
    f, errors = "compute.py", []
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name, param in (("fetch", "run_id"), ("compute", "data")):
        fn = funcs.get(name)
        if fn is None:
            errors.append(_err(f, f"模块顶层缺少 def {name}({param})"))
        elif not _single_positional(fn):
            errors.append(_err(f, f"{name} 必须恰好有一个位置参数", fn.lineno))
    return errors


def _check_node(node: ast.AST) -> list[dict]:
    """单个语法节点：导入白名单、禁用名称、双下划线属性。"""
    f = "compute.py"
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
        return [_err(f, f"不允许导入 {m}", node.lineno) for m in mods if m.split(".")[0] not in ALLOWED_IMPORTS]
    if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
        return [_err(f, f"不允许使用 {node.id}", node.lineno)]
    if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
        return [_err(f, f"不允许访问双下划线属性 {node.attr}", node.lineno)]
    return []


def check_package(spec_raw: bytes, code_raw: bytes) -> tuple[dict | None, list[dict]]:
    """返回 (解析后的 feature.json, 错误列表)。"""
    spec, errors = check_spec(spec_raw)
    return spec, errors + check_code(code_raw)
