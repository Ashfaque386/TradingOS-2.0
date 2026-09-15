# IMPORTANT: every model module MUST be imported here.
#
# Alembic autogenerate (and Base.metadata.create_all) only sees tables whose
# model class has actually been imported into the Python process. A model
# defined in src/models/some_new_table.py that is never imported below is
# invisible to migrations — this exact defect class broke autogenerate in a
# prior build. When you add a new model module, add its import to this file
# in the SAME change, not as a follow-up.

from src.models.agent_binding import AgentBinding
from src.models.agent_config_version import AgentConfigVersion, ConfigVersionStatus
from src.models.agent_identity import AgentIdentity
from src.models.agent_to_agent_policy import AgentToAgentPolicy
from src.models.approval_request import ApprovalRequest, ApprovalStatus
from src.models.audit_log import AuditLog
from src.models.base import Base
from src.models.heartbeat_log import HeartbeatLog, HeartbeatStatus
from src.models.organization_run import (
    NON_TERMINAL_STATUSES,
    TERMINAL_STATUSES,
    FailureClass,
    OrganizationRun,
    RunSource,
    RunStatus,
    RunType,
)
from src.models.organizational_decision import OrganizationalDecision
from src.models.organizational_event import OrganizationalEvent
from src.models.organizational_plan import OrganizationalPlan, PlanStatus
from src.models.prompt_version import PromptVersion, PromptVersionStatus
from src.models.refresh_token import RefreshToken
from src.models.result_artefact import ResultArtefact
from src.models.task import Task, TaskStatus
from src.models.task_dependency import TaskDependency
from src.models.user import User

__all__ = [
    "NON_TERMINAL_STATUSES",
    "TERMINAL_STATUSES",
    "AgentBinding",
    "AgentConfigVersion",
    "AgentIdentity",
    "AgentToAgentPolicy",
    "ApprovalRequest",
    "ApprovalStatus",
    "AuditLog",
    "Base",
    "ConfigVersionStatus",
    "FailureClass",
    "HeartbeatLog",
    "HeartbeatStatus",
    "OrganizationRun",
    "OrganizationalDecision",
    "OrganizationalEvent",
    "OrganizationalPlan",
    "PlanStatus",
    "PromptVersion",
    "PromptVersionStatus",
    "RefreshToken",
    "ResultArtefact",
    "RunSource",
    "RunStatus",
    "RunType",
    "Task",
    "TaskDependency",
    "TaskStatus",
    "User",
]
