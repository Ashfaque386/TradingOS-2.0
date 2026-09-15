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
from src.models.audit_log import AuditLog
from src.models.base import Base
from src.models.refresh_token import RefreshToken
from src.models.user import User

__all__ = [
    "AgentBinding",
    "AgentConfigVersion",
    "AgentIdentity",
    "AgentToAgentPolicy",
    "AuditLog",
    "Base",
    "ConfigVersionStatus",
    "RefreshToken",
    "User",
]
