"""持久化后台任务的公共 API 契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from movieclaw_db.models import JobStatus


class JobProgressView(BaseModel):
    mode: str = Field(description="determinate / indeterminate / waiting / paused")
    phase: str = Field(description="当前领域阶段的稳定标识")
    message: str = Field(description="面向用户的即时进展")
    current: int | None = Field(default=None, description="当前完成量")
    total: int | None = Field(default=None, description="总量；未知时为空")
    percent: float | None = Field(default=None, description="真实可计算时才返回百分比")
    phase_index: int | None = Field(default=None, description="当前阶段序号")
    phase_count: int | None = Field(default=None, description="总阶段数")
    details: dict[str, Any] = Field(default_factory=dict, description="领域进度明细")


class JobResourceView(BaseModel):
    resource_type: str
    resource_id: str
    relation: str


class JobView(BaseModel):
    id: str
    job_type: str
    subject: str | None
    definition_version: int
    handler_revision: str
    prompt_revision: str | None
    provider_ref: str | None
    status: JobStatus
    input_data: dict[str, Any] = Field(description="已确认的任务输入（永不包含密钥）")
    progress: JobProgressView
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    usage: dict[str, Any]
    resources: list[JobResourceView] = Field(default_factory=list)
    origin: str
    actor_kind: str | None
    actor_name: str | None
    parent_job_id: str | None
    root_job_id: str | None
    retry_of_job_id: str | None
    attempt: int
    max_attempts: int
    revision: int
    cancel_requested_at: datetime | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobListView(BaseModel):
    items: list[JobView]


class JobWaitView(BaseModel):
    changed: bool
    job: JobView


class JobCancelView(BaseModel):
    cancelled: bool
    job: JobView


class JobRetryView(BaseModel):
    created: bool
    job: JobView


class JobEventView(BaseModel):
    id: int
    job_id: str
    revision: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class JobEventListView(BaseModel):
    items: list[JobEventView]


class JobWorkerHealthView(BaseModel):
    running: bool
    owner: str | None
    active_workers: int
    counts: dict[str, int]
    oldest_queued_seconds: int | None
