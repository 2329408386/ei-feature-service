#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""特征 / 算子提交内容校验：feature.json、operator.json 格式 + 算子代码静态检查。

返回全部错误，而不是遇到第一个就停。错误项 {file, line, message}。
"""

import ast
import re

from src.util import semver

ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
NAME_MAX = 64  # 显示名称最长字数
CODE_FILE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}\.py$")
CODE_MAX_BYTES = 256 * 1024
ALLOWED_IMPORTS = {
    "eifeature", "math", "statistics", "numpy", "typing", "dataclasses", "functools", "itertools", "collections",
}
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "input", "breakpoint"}
FEATURE_FILE, OPERATOR_FILE = "feature.json", "operator.json"


def _err(file: str, message: str, line: int | None = None) -> dict:
    return {"file": file, "line": line, "message": message}


def _check_desc(file: str, spec: dict) -> list[dict]:
    desc = spec.get("desc")
    if not isinstance(desc, str) or not 1 <= len(desc.strip()) <= 200:
        return [_err(file, "desc 必填，1~200 字")]
    return []


def _check_id_name(file: str, spec: dict) -> list[dict]:
    """id 可省略（新增时后台生成），给了就要合规；name 是显示名称，是否必填由调用方按“是否新建”判断。"""
    errors = []
    if "id" in spec and not ID.match(str(spec["id"])):
        errors.append(_err(file, "id 必须是小写字母开头，由小写字母、数字、下划线组成，3~64 位（新增时不用填，后台自动生成）"))
    name = spec.get("name")
    if name is not None and (not isinstance(name, str) or not 1 <= len(name.strip()) <= NAME_MAX):
        errors.append(_err(file, f"name（名称）必须是 1~{NAME_MAX} 字的字符串"))
    return errors


def check_feature_spec(spec: dict) -> list[dict]:
    """校验 feature.json：id（可省略）、name、version、desc、output.unit。"""
    f = FEATURE_FILE
    errors = _check_id_name(f, spec)
    if not semver.is_valid(str(spec.get("version", ""))):
        errors.append(_err(f, "version 必须形如 1.0.0"))
    errors += _check_desc(f, spec)
    output = spec.get("output")
    if output is not None and not isinstance(output, dict):
        errors.append(_err(f, "output 必须是对象"))
    unit = output.get("unit") if isinstance(output, dict) else None
    if unit is not None and (not isinstance(unit, str) or len(unit) > 32):
        errors.append(_err(f, "output.unit 必须是不超过 32 字的字符串"))
    return errors


def check_operator_spec(spec: dict) -> list[dict]:
    """校验 operator.json：id（算子 ID，可省略，规则同特征 id）、name（名称）、desc。"""
    return _check_id_name(OPERATOR_FILE, spec) + _check_desc(OPERATOR_FILE, spec)


def name_required(file: str) -> dict:
    return _err(file, f"name 必填：新建时要填写名称（1~{NAME_MAX} 字），id 由后台自动生成")


def check_code_file(code_file: str) -> list[dict]:
    """用户指定的 py 文件名：字母或下划线开头，只含字母、数字、下划线，以 .py 结尾。"""
    if CODE_FILE.match(code_file):
        return []
    return [_err("code_file", "文件名必须形如 my_operator.py：字母或下划线开头，只含字母、数字、下划线，最长 64 位")]


def check_code(code: str, file: str = "compute.py") -> list[dict]:
    """静态检查算子代码：顶层 fetch / compute 签名、导入白名单、禁用的内置函数和双下划线属性。"""
    if len(code.encode("utf-8")) > CODE_MAX_BYTES:
        return [_err(file, "文件超过 256 KB")]
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [_err(file, f"无法解析：{e}", e.lineno)]
    errors = _check_entrypoints(tree, file)
    for node in ast.walk(tree):
        errors += _check_node(node, file)
    return errors


def _single_positional(fn: ast.FunctionDef) -> bool:
    """恰好一个位置参数，且没有 *args、仅限关键字参数、**kwargs。"""
    a = fn.args
    if len(a.posonlyargs) + len(a.args) != 1:
        return False
    return not (a.vararg or a.kwonlyargs or a.kwarg)


def _check_entrypoints(tree: ast.Module, file: str) -> list[dict]:
    """模块顶层必须有 fetch(run_id) 和 compute(data)。"""
    errors = []
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name, param in (("fetch", "run_id"), ("compute", "data")):
        fn = funcs.get(name)
        if fn is None:
            errors.append(_err(file, f"模块顶层缺少 def {name}({param})"))
        elif not _single_positional(fn):
            errors.append(_err(file, f"{name} 必须恰好有一个位置参数", fn.lineno))
    return errors


def _check_node(node: ast.AST, file: str) -> list[dict]:
    """单个语法节点：导入白名单、禁用名称、双下划线属性。"""
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
        return [_err(file, f"不允许导入 {m}", node.lineno) for m in mods if m.split(".")[0] not in ALLOWED_IMPORTS]
    if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
        return [_err(file, f"不允许使用 {node.id}", node.lineno)]
    if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
        return [_err(file, f"不允许访问双下划线属性 {node.attr}", node.lineno)]
    return []


def check_operator(spec: dict, code: str, code_file: str | None) -> list[dict]:
    """算子提交内容的全部错误：operator.json、文件名（指定时）、代码。"""
    errors = check_operator_spec(spec)
    if code_file is not None:
        errors += check_code_file(code_file)
    return errors + check_code(code, code_file or "compute.py")
