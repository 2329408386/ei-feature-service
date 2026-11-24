"""特征包文件存储：MinIO（生产）或本地目录（endpoint 为 file:// 时，用于开发和测试）。

对象键：features/{feature_id}/{version}/feature.json、compute.py
"""

import hashlib
import io
import shutil
import uuid
from functools import cache
from pathlib import Path

from src.util.config import PACKAGE_CACHE_DIR, ROOT, MinioConfig, settings

SPEC_FILE, CODE_FILE = "feature.json", "compute.py"


def package_hash(spec: bytes, code: bytes) -> str:
    return hashlib.sha256(spec + b"\n" + code).hexdigest()


def _key(feature_id: str, version: str, name: str = "") -> str:
    return f"features/{feature_id}/{version}/{name}"


class LocalStore:
    def __init__(self, root: Path):
        self.root = root

    def put(self, feature_id: str, version: str, name: str, data: bytes) -> None:
        path = self.root / _key(feature_id, version, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, feature_id: str, version: str, name: str) -> bytes:
        return (self.root / _key(feature_id, version, name)).read_bytes()

    def delete(self, feature_id: str, version: str) -> None:
        shutil.rmtree(self.root / _key(feature_id, version), ignore_errors=True)


class MinioStore:
    def __init__(self, cfg: MinioConfig):
        from minio import Minio

        self.client = Minio(cfg.endpoint, cfg.access_key, cfg.secret_key, secure=cfg.secure)
        self.bucket = cfg.bucket
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)

    def put(self, feature_id, version, name, data):
        self.client.put_object(self.bucket, _key(feature_id, version, name), io.BytesIO(data), len(data))

    def get(self, feature_id, version, name):
        resp = self.client.get_object(self.bucket, _key(feature_id, version, name))
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def delete(self, feature_id, version):
        for name in (SPEC_FILE, CODE_FILE):
            self.client.remove_object(self.bucket, _key(feature_id, version, name))


def make_store(cfg: MinioConfig) -> LocalStore | MinioStore:
    if cfg.endpoint.startswith("file://"):
        path = Path(cfg.endpoint.removeprefix("file://"))
        return LocalStore(path if path.is_absolute() else ROOT / path)
    return MinioStore(cfg)


@cache
def get_store() -> LocalStore | MinioStore:
    return make_store(settings.minio)


def package_uri(feature_id: str, version: str) -> str:
    return f"{settings.minio.endpoint.rstrip('/')}/{settings.minio.bucket}/{_key(feature_id, version)}"


def local_package(feature_id: str, version: str, expected_hash: str) -> Path:
    """把特征包下载到本地缓存目录并校验 hash，返回目录（含 feature.json、compute.py）。"""
    target = PACKAGE_CACHE_DIR / feature_id / version / expected_hash[:12]
    if (target / CODE_FILE).exists():
        return target
    store = get_store()
    spec, code = store.get(feature_id, version, SPEC_FILE), store.get(feature_id, version, CODE_FILE)
    if package_hash(spec, code) != expected_hash:
        raise RuntimeError(f"特征包 {feature_id}@{version} hash 不一致，文件可能被改动")
    tmp = target.parent / f"{target.name}.{uuid.uuid4().hex}.tmp"
    tmp.mkdir(parents=True)
    (tmp / SPEC_FILE).write_bytes(spec)
    (tmp / CODE_FILE).write_bytes(code)
    try:
        tmp.rename(target)  # 原子落盘，避免并发时读到半个包
    except OSError:  # 别的进程已经放好了
        shutil.rmtree(tmp, ignore_errors=True)
    return target
