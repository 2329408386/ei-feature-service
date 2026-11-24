"""特征包上传校验：feature.json 格式 + compute.py 静态检查。返回全部错误，而不是遇到第一个就停。"""

import ast
import json
import re

from src.util import semver

ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
CODE_MAX_BYTES = 256 * 1024
ALLOWED_IMPORTS = {"eifeature", "math", "statistics", "numpy", "typing", "dataclasses", "functools", "itertools", "collections"}
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "input", "breakpoint"}


def _err(file: str, message: str, line: int | None = None) -> dict:
    return {"file": file, "line": line, "message": message}


def check_spec(raw: bytes) -> tuple[dict | None, list[dict]]:
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
    f = "compute.py"
    if len(raw) > CODE_MAX_BYTES:
        return [_err(f, "文件超过 256 KB")]
    try:
        tree = ast.parse(raw.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError) as e:
        return [_err(f, f"无法解析：{e}", getattr(e, "lineno", None))]

    errors = []
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name, param in (("fetch", "run_id"), ("compute", "data")):
        fn = funcs.get(name)
        if fn is None:
            errors.append(_err(f, f"模块顶层缺少 def {name}({param})"))
        elif len(fn.args.posonlyargs) + len(fn.args.args) != 1 or fn.args.vararg or fn.args.kwonlyargs or fn.args.kwarg:
            errors.append(_err(f, f"{name} 必须恰好有一个位置参数", fn.lineno))

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for m in mods:
                if m.split(".")[0] not in ALLOWED_IMPORTS:
                    errors.append(_err(f, f"不允许导入 {m}", node.lineno))
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            errors.append(_err(f, f"不允许使用 {node.id}", node.lineno))
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            errors.append(_err(f, f"不允许访问双下划线属性 {node.attr}", node.lineno))
    return errors


def check_package(spec_raw: bytes, code_raw: bytes) -> tuple[dict | None, list[dict]]:
    """返回 (解析后的 feature.json, 错误列表)。"""
    spec, errors = check_spec(spec_raw)
    return spec, errors + check_code(code_raw)
