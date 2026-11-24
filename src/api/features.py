"""特征接口：/api/features"""

from typing import Annotated, Literal

from fastapi import APIRouter, File, Query, UploadFile

from src.api.schemas import FeatureDetail, FeatureOut, FeatureReviewIn, Page, RetireIn, RetireOut, TrialIn, TrialOut
from src.service import feature as svc
from src.util.auth import User

router = APIRouter(prefix="/features", tags=["特征"])
PATH = "/{feature_id}/{version}"


@router.post("", status_code=201, response_model=FeatureOut)
async def upload(user: User, spec: Annotated[UploadFile, File(description="feature.json")],
                 code: Annotated[UploadFile, File(description="compute.py")]):
    """上传特征包，校验通过后生成草稿。"""
    return await svc.upload(await spec.read(), await code.read(), user)


@router.get("", response_model=Page[FeatureOut])
async def list_features(
    status: Literal["draft", "review", "published", "retired"] | None = None,
    q: str | None = None,
    feature_id: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
):
    rows, total = await svc.list_features(status, q, feature_id, page, page_size)
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


@router.get(PATH, response_model=FeatureDetail)
async def get_feature(feature_id: str, version: str):
    row, code = await svc.get_feature(feature_id, version)
    return {**FeatureOut.model_validate(row).model_dump(), "code": code}


@router.delete(PATH, status_code=204)
async def delete_draft(feature_id: str, version: str, user: User):
    await svc.delete_draft(feature_id, version, user)


@router.post(PATH + "/submit", response_model=FeatureOut)
async def submit(feature_id: str, version: str, user: User):
    return await svc.submit(feature_id, version, user)


@router.post(PATH + "/review", response_model=FeatureOut)
async def review(feature_id: str, version: str, body: FeatureReviewIn, user: User):
    return await svc.review(feature_id, version, body.decision, body.comment, body.trial_runs, user)


@router.post(PATH + "/retire", response_model=RetireOut)
async def retire(feature_id: str, version: str, body: RetireIn, user: User):
    row, affected = await svc.retire(feature_id, version, body.reason, user)
    return {"feature": row, "affected": affected}


@router.post(PATH + "/trial", response_model=TrialOut)
async def trial(feature_id: str, version: str, body: TrialIn):
    """试跑：执行一次 fetch + compute，结果不写库。"""
    return await svc.trial(feature_id, version, body.run_id)
