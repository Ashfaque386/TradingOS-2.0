"""The 4 fixed RBAC roles (Build Spec §20). Kept dependency-free so both the
ORM models and the RBAC policy engine can import it without a cycle.
"""

from enum import StrEnum


class Role(StrEnum):
    SYSTEM_ADMINISTRATOR = "SystemAdministrator"
    PORTFOLIO_MANAGER = "PortfolioManager"
    RISK_MANAGER = "RiskManager"
    READ_ONLY_AUDITOR = "ReadOnlyAuditor"
