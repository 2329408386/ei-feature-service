#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""接口的请求 / 响应模型。字段名即 JSON 字段名。"""

from datetime import date, datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, computed_field

T = TypeVar("T")
EXPORT_FORMAT = "ei-feature-service.bindings/v2"
Status = Literal["draft", "review", "published", "retired"]


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------- 特征与算子共用
class ReviewEntry(BaseModel):
    decision: Literal["approve", "reject"]
    reviewer: str
    comment: str | None = None
    trial_runs: list[str] = []
    at: datetime


class ReviewIn(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str | None = None
    trial_runs: list[str] = []  # 算子评审通过时必填；特征评审不需要


class RetireIn(BaseModel):
    reason: str = Field(min_length=1)


class _Reviewed(Out):
    status: Status
    spec: dict
    created_by: str
    created_at: datetime
    submitted_at: datetime | None
    published_by: str | None
    published_at: datetime | None
    retired_by: str | None
    retired_at: datetime | None
    retire_reason: str | None
    review_log: list[ReviewEntry]

    @computed_field
    def description(self) -> str:
        """描述，取自 spec.desc。"""
        return self.spec.get("desc", "")


# ---------------------------------------------------------------- 算子
class OperatorOut(_Reviewed):
    feature_id: str
    name: str
    version: int
    code_file: str
    code_hash: str

    @computed_field
    def ref(self) -> str:
        """算子引用 feature_id/name@version。"""
        return f"{self.feature_id}/{self.name}@{self.version}"


class OperatorDetail(OperatorOut):
    code: str


class OperatorIn(BaseModel):
    """新增算子（或同名算子的新版本）：直接填 operator.json 和代码。"""

    feature_id: str
    spec: dict  # operator.json：name、desc 必填
    code: str = Field(min_length=1)
    code_file: str | None = None  # 不填时为 {name}_v{version}.py


class OperatorRetired(BaseModel):
    feature_id: str
    operator: str
    version: int
    new_revision: int | None  # 生成的 system 修订；该算子没有生效绑定时为 null
    chambers: list[str]  # 不再计算该算子的腔室


class TrialIn(BaseModel):
    run_id: str = Field(min_length=1)


class TrialRun(BaseModel):
    run_id: str
    tool: str
    chamber: str
    recipe: str
    lot: str | None
    wafer: str | None
    end_at: datetime


class TrialOut(BaseModel):
    run: TrialRun
    data_summary: dict
    value: float | None
    reason: str | None
    reason_detail: str | None
    fetch_ms: int
    compute_ms: int


class OperatorRetireOut(BaseModel):
    operator: OperatorOut
    affected: list[OperatorRetired]


# ---------------------------------------------------------------- 特征
class FeatureOut(_Reviewed):
    feature_id: str
    version: str

    @computed_field
    def unit(self) -> str | None:
        """输出单位，取自 feature.json 的 output.unit。"""
        output = self.spec.get("output")
        return output.get("unit") if isinstance(output, dict) else None


class FeatureDetail(FeatureOut):
    operators: list[OperatorOut]  # 该 feature_id 下全部算子（各版本）


class FeatureRetireOut(BaseModel):
    feature: FeatureOut
    operators: list[str]  # 随特征一起下线的算子引用
    affected: list[OperatorRetired]


# ---------------------------------------------------------------- 算子绑定
class BindingChamber(BaseModel):
    tool: str
    chamber: str


class BindingRecipe(BaseModel):
    recipe: str
    chambers: list[BindingChamber]


class FleetScope(BaseModel):
    """选腔室时用的 fleet 和时间范围，只保存下来用于页面回显，不参与计算。"""

    name: str
    start: date
    end: date


class BindingSpec(BaseModel):
    feature_id: str
    operator: str
    version: int
    fleet: FleetScope | None = None
    recipes: list[BindingRecipe]


class BindingSubmitIn(BindingSpec):
    base_revision: int | None = None


class RevisionOut(Out):
    feature_id: str
    operator: str
    revision: int
    version: int
    spec: BindingSpec
    status: Literal["pending", "active", "rejected", "withdrawn", "superseded"]
    source: Literal["console", "api", "import", "system"]
    base_revision: int | None
    created_by: str
    created_at: datetime
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_note: str | None
    superseded_at: datetime | None


class OperatorBindingOut(BaseModel):
    feature_id: str
    operator: str
    active: RevisionOut | None
    pending: RevisionOut | None


class OperatorRevisionsOut(BaseModel):
    feature_id: str
    operator: str
    revisions: list[RevisionOut]


class RevisionReviewIn(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str | None = None


class ImportDoc(BaseModel):
    """导入时每个算子单独校验，所以 bindings 先按原样接收。"""

    format: Literal["ei-feature-service.bindings/v2"]
    exported_at: datetime | None = None
    bindings: list[dict]


class ImportItemOut(BaseModel):
    feature_id: str | None
    operator: str | None
    action: Literal["new", "skip", "conflict", "error"]
    message: str
    revision: int | None = None
    errors: list[dict] | None = None


class ImportOut(BaseModel):
    items: list[ImportItemOut]


# ---------------------------------------------------------------- GDE 元数据（转发）
class ToolChamberOut(BaseModel):
    tool: str
    chamber: str


# ---------------------------------------------------------------- 调度、批次、特征值
class ScheduleOut(BaseModel):
    tool: str
    chamber: str
    gde_sub_id: str | None
    recipes: list[str]
    operator_count: int
    synced: bool  # GDE 订阅范围是否已与 recipes 一致
    last_error: str | None
    status: Literal["running", "paused", "cancelled", "unknown"]
    progress: datetime | None


class BatchIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    chamber: str
    mode: Literal["fill", "overwrite"]
    operators: list[str] = Field(min_length=1)  # ["feature_id/operator@version"]
    from_: date = Field(alias="from")
    to: date
    reason: str = Field(min_length=1)


class BatchOut(Out):
    batch_id: int
    mode: Literal["live", "fill", "overwrite"]
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    chamber: str
    operators: list[str] | None
    plan: dict[str, list[str]] | None
    data_from: datetime
    data_to: datetime
    reason: str | None
    superseded_by: int | None
    run_total: int
    value_ok: int
    value_null: int
    error: str | None
    created_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ValueOut(Out):
    feature_id: str
    operator: str
    version: int
    run_id: str
    tool: str
    chamber: str
    recipe: str
    lot: str | None
    wafer: str | None
    run_end_at: datetime
    value: float | None
    reason: str | None
    reason_detail: str | None
    batch_id: int
    computed_at: datetime
    retired: bool
