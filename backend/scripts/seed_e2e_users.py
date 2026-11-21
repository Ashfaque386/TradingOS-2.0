"""E2E test-fixture user seeding (Build Spec §21-22 hardening pass).

This is NOT a production credential-bootstrap mechanism -- the real,
supported path for a human operator is `POST /api/v1/auth/register`
(the first registration in an empty system auto-promotes to
SystemAdministrator; every later one is capped at ReadOnlyAuditor, see
src/api/routes/auth.py's own comment). There is deliberately no
self-service "grant another user a higher role" endpoint yet, so the
Playwright E2E suite (../e2e/) -- which needs PortfolioManager/
RiskManager/SystemAdministrator accounts to exercise strategy promotion,
kill-switch reset, and live-intent sign-off -- seeds its own fixture
accounts directly via the ORM, using the app's real `hash_password()`
(the same Argon2 hashing the /register endpoint itself uses), the same
way an earlier session's manual smoke-test accounts
(smoke.pm@example.com etc., still in this dev DB) were created. No
password is ever stored or printed in plaintext; only its hash is
persisted, and this script's own source is the single place the fixture
passwords are defined, imported by the E2E suite rather than duplicated.

Idempotent: safe to run before every E2E suite invocation.
"""

import asyncio
import os
import sys
from collections.abc import Mapping

from sqlalchemy import select

from src.core.db import get_session_factory
from src.core.roles import Role
from src.core.security import hash_password
from src.models.user import User

# Fixture-only credentials for the local E2E run -- never used against a
# real deployment, and never the same value as any production secret.
E2E_USERS: dict[str, tuple[str, Role]] = {
    # example.com (RFC 2606), not tradingos.test -- email-validator rejects
    # .test as a special-use TLD, confirmed live against /auth/login.
    "e2e.admin@example.com": ("E2eAdminPass!2026", Role.SYSTEM_ADMINISTRATOR),
    "e2e.pm@example.com": ("E2ePmPass!2026", Role.PORTFOLIO_MANAGER),
    "e2e.risk@example.com": ("E2eRiskPass!2026", Role.RISK_MANAGER),
    "e2e.auditor@example.com": ("E2eAuditorPass!2026", Role.READ_ONLY_AUDITOR),
}


ALLOW_ENV = "TRADINGOS_E2E_SEED"


class SeedRefusedError(RuntimeError):
    """The seed was not explicitly allowed for this database."""


def require_explicit_permission(env: Mapping[str, str] | None = None) -> None:
    """These accounts have published passwords, and the script activates them (again) wherever
    DATABASE_URL points -- inside the backend container that is the live database. On 2026-11-21
    four of them, one an administrator, were found active on the live stack. So it runs only
    where the caller has said this database is a test one: TRADINGOS_E2E_SEED=1 (set by CI's seed
    step and the e2e README)."""
    if (env if env is not None else os.environ).get(ALLOW_ENV) != "1":
        raise SeedRefusedError(
            f"refusing to create the e2e accounts: set {ALLOW_ENV}=1 if DATABASE_URL is a test "
            "database. Never run this against a real deployment."
        )


async def seed() -> None:
    require_explicit_permission()
    session_factory = get_session_factory()
    async with session_factory() as db:
        for email, (password, role) in E2E_USERS.items():
            existing = (
                await db.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()
            if existing is not None:
                existing.hashed_password = hash_password(password)
                existing.role = role
                existing.is_active = True
                print(f"updated  {email} ({role.value})")
                continue
            db.add(
                User(
                    email=email,
                    hashed_password=hash_password(password),
                    role=role,
                    is_active=True,
                )
            )
            print(f"created  {email} ({role.value})")
        await db.commit()


if __name__ == "__main__":
    try:
        asyncio.run(seed())
    except SeedRefusedError as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)
