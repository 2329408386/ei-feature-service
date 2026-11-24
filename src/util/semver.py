"""语义化版本号：只支持 MAJOR.MINOR.PATCH。"""

import re

PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


def parse(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(x) for x in version.split("."))
    return major, minor, patch


def is_valid(version: str) -> bool:
    return bool(PATTERN.match(version))
