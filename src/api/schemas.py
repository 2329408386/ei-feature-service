"""接口的请求 / 响应模型。字段名即 JSON 字段名。"""

from datetime import date, datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, computed_field

T = TypeVar("T")
EXPORT_FORMAT = "ei-feature-service.bindings/v1"


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------- 特征
class ReviewEntry(BaseModel):
    decision: Literal["approve", "reject"]
    reviewer: str
    comment: str | None = None
    trial_runs: list[str] = []
    at: datetime


class FeatureOut(Out):
    feature_id: str
    version: str
    status: Literal["draft", "review", "published", "retired"]
    spec: dict
    package_hash: str
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
        return self.spec.get("desc", "")

    @computed_field
    def unit(self) -> str | None:
        output = self.spec.get("output")
        return output.get("unit") if isinstance(output, dict) else None


class FeatureDetail(FeatureOut):
    code: str


class FeatureReviewIn(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str | None = None
    trial_runs: list[str] = []


class RetireIn(BaseModel):
    reason: str = Field(min_length=1)


class RetireAffected(BaseModel):
    chamber: str
    new_revision: int
    subscription: Literal["kept", "cancelled"]


class RetireOut(BaseModel):
    feature: FeatureOut
    affected: list[RetireAffected]


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


# ---------------------------------------------------------------- 腔室绑定
class Rule(BaseModel):
    recipes: list[str]
    features: dict[str, str]  # feature_id -> version


class BindingSpec(BaseModel):
    tool: str
    chamber: str
    rules: list[Rule]


class BindingSubmitIn(BindingSpec):
    base_revision: int | None = None


class RevisionOut(Out):
    chamber: str
    revision: int
    tool: str
    spec: BindingSpec
    status: Literal["pending", "active", "rejected", "withdrawn", "superseded"]
    source: Literal["console", "api", "import", "system"]
    base_revision: int | None
    gde_sub_id: str | None
    created_by: str
    created_at: datetime
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_note: str | None
    superseded_at: datetime | None


class ChamberBindingOut(BaseModel):
    tool: str
    chamber: str
    active: RevisionOut | None
    pending: RevisionOut | None


class ChamberRevisionsOut(BaseModel):
    chamber: str
    revisions: list[RevisionOut]


class RevisionReviewIn(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str | None = None


class ExportItem(BaseModel):
    tool: str
    chamber: str
    revision: int | None = None
    rules: list[Rule]


class ExportDoc(BaseModel):
    format: Literal["ei-feature-service.bindings/v1"] = EXPORT_FORMAT
    exported_at: datetime | None = None
    bindings: list[ExportItem]


class ImportDoc(BaseModel):
    """导入时每个腔室单独校验，所以 bindings 先按原样接收。"""

    format: Literal["ei-feature-service.bindings/v1"]
    exported_at: datetime | None = None
    bindings: list[dict]


class ImportItemOut(BaseModel):
    tool: str | None
    chamber: str | None
    action: Literal["new", "skip", "conflict", "error"]
    message: str
    revision: int | None = None
    errors: list[dict] | None = None


class ImportOut(BaseModel):
    items: list[ImportItemOut]


# ---------------------------------------------------------------- 调度、批次、特征值
class ScheduleOut(BaseModel):
    tool: str
    chamber: str
    gde_sub_id: str | None
    binding_revision: int
    feature_count: int
    status: Literal["running", "paused", "cancelled", "unknown"]
    progress: datetime | None


class BatchIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    chamber: str
    mode: Literal["fill", "overwrite"]
    features: list[str] = Field(min_length=1)  # ["id@ver"]
    from_: date = Field(alias="from")
    to: date
    reason: str = Field(min_length=1)


class BatchOut(Out):
    batch_id: int
    mode: Literal["live", "fill", "overwrite"]
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    chamber: str
    binding_revision: int
    features: list[str] | None
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
    version: str
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
