#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""算子文件存储：MinIO（生产）或本地目录（endpoint 为 file:// 时，用于开发和测试）。

对象键：features/{feature_id}/operators/{name}/{version}/operator.json、{code_file}
"""

import hashlib
import io
import shutil
import uuid
from functools import cache
from pathlib import Path

from src.util.config import PACKAGE_CACHE_DIR, ROOT, MinioConfig, settings

SPEC_FILE = "operator.json"


def code_hash(spec: bytes, code: bytes) -> str:
    """算子内容 hash：operator.json 和代码一起计算。"""
    return hashlib.sha256(spec + b"\n" + code).hexdigest()


def operator_prefix(feature_id: str, name: str, version: int) -> str:
    """算子某个版本的对象键前缀（以 / 结尾）。"""
    return f"features/{feature_id}/operators/{name}/{version}/"


class LocalStore:
    def __init__(self, root: Path):
        self.root = root

    def put(self, key: str, data: bytes) -> None:
        """写入一个对象。"""
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        """读取一个对象。"""
        return (self.root / key).read_bytes()

    def delete_prefix(self, prefix: str) -> None:
        """删除前缀下的全部对象。"""
        shutil.rmtree(self.root / prefix, ignore_errors=True)


class MinioStore:
    def __init__(self, cfg: MinioConfig):
        from minio import Minio

        self.client = Minio(cfg.endpoint, cfg.access_key, cfg.secret_key, secure=cfg.secure)
        self.bucket = cfg.bucket
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)

    def put(self, key, data):
        """写入一个对象。"""
        self.client.put_object(self.bucket, key, io.BytesIO(data), len(data))

    def get(self, key):
        """读取一个对象。"""
        resp = self.client.get_object(self.bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def delete_prefix(self, prefix):
        """删除前缀下的全部对象。"""
        for obj in self.client.list_objects(self.bucket, prefix=prefix, recursive=True):
            self.client.remove_object(self.bucket, obj.object_name)


def make_store(cfg: MinioConfig) -> LocalStore | MinioStore:
    """按配置创建存储：endpoint 以 file:// 开头用本地目录，否则用 MinIO。"""
    if cfg.endpoint.startswith("file://"):
        path = Path(cfg.endpoint.removeprefix("file://"))
        return LocalStore(path if path.is_absolute() else ROOT / path)
    return MinioStore(cfg)


@cache
def get_store() -> LocalStore | MinioStore:
    """进程内单例。"""
    return make_store(settings.minio)


def operator_uri(feature_id: str, name: str, version: int) -> str:
    """算子文件所在位置，用于展示。"""
    return f"{settings.minio.endpoint.rstrip('/')}/{settings.minio.bucket}/{operator_prefix(feature_id, name, version)}"


def local_operator(feature_id: str, name: str, version: int, code_file: str, expected_hash: str) -> Path:
    """把算子文件下载到本地缓存目录并校验 hash，返回代码文件路径。"""
    target = PACKAGE_CACHE_DIR / feature_id / name / str(version) / expected_hash[:12]
    if (target / code_file).exists():
        return target / code_file
    store, prefix = get_store(), operator_prefix(feature_id, name, version)
    spec, code = store.get(prefix + SPEC_FILE), store.get(prefix + code_file)
    if code_hash(spec, code) != expected_hash:
        raise RuntimeError(f"算子 {feature_id}/{name}@{version} hash 不一致，文件可能被改动")
    tmp = target.parent / f"{target.name}.{uuid.uuid4().hex}.tmp"
    tmp.mkdir(parents=True)
    (tmp / SPEC_FILE).write_bytes(spec)
    (tmp / code_file).write_bytes(code)
    try:
        tmp.rename(target)  # 原子落盘，避免并发时读到半个目录
    except OSError:  # 别的进程已经放好了
        shutil.rmtree(tmp, ignore_errors=True)
    return target / code_file
