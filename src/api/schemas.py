#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Sicarrier Technologies Co., Ltd. 2026. All rights reserved.
"""接口的请求 / 响应模型。字段名即 JSON 字段名。"""

from datetime import date, datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, computed_field

T = TypeVar("T")
EXPORT_FORMAT = "ei-feature-service.bindings/v3"
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
    decision: Literal["approve", "reject", "restore"]  # restore：重新上线（reviewer 为操作人）
    reviewer: str
    comment: str | None = None
    trial_runs: list[str] = []
    at: datetime
    retire_reason: str | None = None  # 仅 restore：重新上线前的下线原因


class ReviewIn(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str | None = None
    trial_runs: list[str] = []  # 算子评审通过时必填；特征评审不需要


class RetireIn(BaseModel):
    reason: str = Field(min_length=1)


class RestoreIn(BaseModel):
    """重新上线：已下线 -> 已发布，不再评审。"""

    comment: str | None = None
    operators: bool = True  # 仅特征：随该特征下线的算子是否一并重新上线


class DeleteImpact(BaseModel):
    """删除的影响范围（DELETE ?dry_run=true 返回，供前端弹窗警告；实际删除返回 204）。"""

    features: list[str] = []  # 删除的特征版本
    last_version: bool | None = None  # 仅特征：是否该特征的最后一个版本（是则连同全部算子删除）
    operators: list[str]  # 删除的算子版本引用
    values: int  # 删除的特征值条数
    bindings: int  # 删除的绑定修订数（算子一个版本都不剩时）
    removed_operators: list[str]  # 一个版本都不剩、整个删掉的算子 feature_id/算子名


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
    name: str  # 算子名（ID 的一部分，不可修改）
    version: int
    code_file: str
    code_hash: str
    display_name: str | None = None  # 可修改的显示名称（operator_meta），未设置为 null

    @computed_field
    def ref(self) -> str:
        """算子引用 feature_id/name@version。"""
        return f"{self.feature_id}/{self.name}@{self.version}"


class OperatorDetail(OperatorOut):
    code: str


class OperatorIn(BaseModel):
    """新增算子（或已有算子的新版本）：直接填 operator.json 和代码。"""

    feature_id: str
    spec: dict  # operator.json：name（名称，新建时必填）、desc 必填；id 不填则后台生成，填已有算子的 id 即提交新版本
    code: str = Field(min_length=1)
    code_file: str | None = None  # 不填时为 {name}_v{version}.py


class OperatorRetired(BaseModel):
    feature_id: str
    operator: str
    version: int
    new_revision: int | None  # 生成的 system 修订；该算子没有生效绑定时为 null
    recipes: list[str]  # 不再计算该算子的 recipe


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
    display_name: str | None = None  # 可修改的显示名称（feature_meta），未设置为 null
    tools: list[str] = []  # 关联的机台：该特征生效绑定的 recipe 经 GDE 反查到的机台（去重排序）

    @computed_field
    def unit(self) -> str | None:
        """输出单位，取自 feature.json 的 output.unit。"""
        output = self.spec.get("output")
        return output.get("unit") if isinstance(output, dict) else None


class FeatureDetail(FeatureOut):
    operators: list[OperatorOut]  # 该 feature_id 下全部算子（各版本）


class FeatureMetaIn(BaseModel):
    """修改特征的显示名称；传 null 或空串表示清空。ID 不变，不需要评审。"""

    name: str | None = Field(default=None, max_length=64)


class FeatureMetaOut(BaseModel):
    feature_id: str
    name: str | None


class OperatorMetaIn(BaseModel):
    """修改算子的显示名称；传 null 或空串表示清空。算子名不变，不需要评审。"""

    name: str | None = Field(default=None, max_length=64)


class OperatorMetaOut(BaseModel):
    feature_id: str
    operator: str
    name: str | None


class FeatureRestoreOut(BaseModel):
    feature: FeatureOut
    operators: list[str]  # 一并重新上线的算子引用


class FeatureRetireOut(BaseModel):
    feature: FeatureOut
    operators: list[str]  # 随特征一起下线的算子引用
    affected: list[OperatorRetired]


# ---------------------------------------------------------------- 算子绑定
class BindingSpec(BaseModel):
    feature_id: str
    operator: str
    version: int
    recipes: list[str]  # GDE 按 recipe 推送全部机台 / 腔室的执行，不再选腔室


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

    format: Literal["ei-feature-service.bindings/v3"]
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


class RunOut(BaseModel):
    run_id: str
    tool: str
    chamber: str
    recipe: str
    lot: str | None
    wafer: str | None
    start_at: datetime
    end_at: datetime


# ---------------------------------------------------------------- 调度、批次、特征值
class ScheduleChamber(BaseModel):
    tool: str
    chamber: str
    progress: datetime | None  # 该腔室实时计算到的最新执行结束时间


class ScheduleRecipe(BaseModel):
    """一个 recipe 的订阅状态及其下的机台 / 腔室。"""

    recipe: str
    gde_sub_id: str | None
    wanted: bool  # 是否有生效绑定需要订阅
    synced: bool  # GDE 订阅是否与 wanted 一致
    last_error: str | None
    status: Literal["running", "paused", "cancelled", "unknown"]
    chambers: list[ScheduleChamber] | None  # 跑过该 recipe 的机台 / 腔室（GDE 反查）；GDE 不可用时为 null
    progress: datetime | None


class ScheduleOut(ScheduleRecipe):
    """以 recipe 为键的一行，附带绑定了它的算子。"""

    operators: list[str]  # 生效绑定中的算子引用


class ScheduleOperator(BaseModel):
    operator: str
    version: int  # 生效绑定中的算子版本
    ref: str
    name: str | None  # 算子显示名称
    recipes: list[ScheduleRecipe]


class ScheduleTreeOut(BaseModel):
    """调度页树形视图：特征 → 算子 → recipe → 机台 / 腔室。"""

    feature_id: str
    name: str | None  # 特征显示名称
    operators: list[ScheduleOperator]


class BatchIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    recipe: str
    mode: Literal["fill", "overwrite"]
    operators: list[str] = Field(min_length=1)  # ["feature_id/operator@version"]
    from_: date = Field(alias="from")
    to: date
    reason: str = Field(min_length=1)


class BatchOut(Out):
    batch_id: int
    mode: Literal["live", "fill", "overwrite"]
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    recipe: str
    operators: list[str] | None
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
