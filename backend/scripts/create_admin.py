"""Create (or promote) a SystemAdministrator user directly against the
database. Useful for the rare case the bootstrap-first-user-is-admin flow
(see src/api/routes/auth.py::register) was skipped or a second admin is
needed. Run from backend/: python -m scripts.create_admin <email> <password>
"""

import asyncio
import sys

from sqlalchemy import select

from src.core.db import AsyncSessionLocal
from src.core.roles import Role
from src.core.security import hash_password
from src.models.user import User


async def create_admin(email: str, password: str) -> None:
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()

        if existing is not None:
            existing.role = Role.SYSTEM_ADMINISTRATOR
            existing.hashed_password = hash_password(password)
            print(f"Promoted existing user {email} to SystemAdministrator.")
        else:
            session.add(
                User(
                    email=email,
                    hashed_password=hash_password(password),
                    role=Role.SYSTEM_ADMINISTRATOR,
                )
            )
            print(f"Created SystemAdministrator user {email}.")

        await session.commit()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python -m scripts.create_admin <email> <password>", file=sys.stderr)
        raise SystemExit(1)

    asyncio.run(create_admin(sys.argv[1], sys.argv[2]))
