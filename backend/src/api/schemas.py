import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from src.core.roles import Role
from src.models.organization_run import FailureClass, RunSource, RunStatus, RunType
from src.models.task import TaskStatus


class UserRegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    role: Role
    is_active: bool


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class CreateRunRequest(BaseModel):
    objective: str = Field(min_length=1, max_length=4000)


class TaskSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    plan_key: str
    capability: str
    name: str
    status: TaskStatus
    failure_class: FailureClass | None
    last_error: str | None


class RunResponse(BaseModel):
    id: uuid.UUID
    objective: str
    source: RunSource
    run_type: RunType
    status: RunStatus
    failure_class: FailureClass | None
    source_run_id: uuid.UUID | None
    error: str | None
    tasks: list[TaskSummaryResponse] = []


class AgentSummaryResponse(BaseModel):
    agent_id: str
    display_name: str
    department: str
    can_disable: bool
    enabled: bool
    capabilities: list[str]
    skills: list[str]
    heartbeat_enabled: bool


class RunPipelineRequest(BaseModel):
    objective: str = Field(min_length=1, max_length=4000)


class RunPipelineResponse(BaseModel):
    objective: str
    node_log: list[str]
    deployment_result: dict | None
    evaluation_verdict: dict | None
    rejection_count: int
