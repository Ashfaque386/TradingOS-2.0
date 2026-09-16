"""Broker credentials API (Build Spec §3, §20): write-only by design --
every endpoint here either writes/deletes a credential or reports only
whether a broker is *configured*, never the credential values
themselves. There is no `GET` that returns an `api_key`/`api_secret`/
`access_token`; that is not an oversight, it is the whole point of
`WriteBrokerCredentialsRequest` having no response counterpart that
echoes it back (src.api.schemas). Nothing in this module ever passes a
`BrokerCredentials` value to `logger.*` -- only the broker name and
which user acted, same "never logged" posture as
src.security.secrets_store itself.
"""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status

from src.api.schemas import BrokerCredentialStatusResponse, WriteBrokerCredentialsRequest
from src.brokers.base import BrokerCredentials
from src.brokers.factory import KNOWN_BROKERS
from src.core.rbac import Role, register_policy, require_role
from src.models.user import User
from src.security.secrets_store import SecretsStore, SecretsStoreError, get_secrets_store

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/broker-credentials", tags=["broker-credentials"])

# Credentials are the most sensitive configuration surface in this system
# -- writing or deleting them is SystemAdministrator-only, stricter than
# the paper-trading/risk-limit operator pairs used elsewhere. Read-only
# configured/not-configured status is harmless (a boolean, never a
# value) and open to every role, same as other read surfaces in this
# codebase.
_WRITE_ROLES = [Role.SYSTEM_ADMINISTRATOR]

register_policy("GET", "/api/v1/broker-credentials", roles=list(Role))
register_policy("POST", "/api/v1/broker-credentials/{broker}", roles=_WRITE_ROLES)
register_policy("DELETE", "/api/v1/broker-credentials/{broker}", roles=_WRITE_ROLES)


def get_broker_credentials_store() -> SecretsStore:
    try:
        return get_secrets_store()
    except SecretsStoreError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


def _require_known_broker(broker: str) -> None:
    if broker not in KNOWN_BROKERS:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"unknown broker: {broker!r} (known: {', '.join(KNOWN_BROKERS)})",
        )


@router.get("")
async def list_broker_credential_status_endpoint(
    _current_user: User = Depends(require_role),
    store: SecretsStore = Depends(get_broker_credentials_store),
) -> list[BrokerCredentialStatusResponse]:
    configured = set(store.list_configured_brokers())
    return [
        BrokerCredentialStatusResponse(broker=broker, configured=broker in configured)
        for broker in KNOWN_BROKERS
    ]


@router.post("/{broker}", status_code=status.HTTP_204_NO_CONTENT)
async def write_broker_credentials_endpoint(
    broker: str,
    body: WriteBrokerCredentialsRequest,
    current_user: User = Depends(require_role),
    store: SecretsStore = Depends(get_broker_credentials_store),
) -> None:
    _require_known_broker(broker)
    store.set_credentials(
        broker,
        BrokerCredentials(
            api_key=body.api_key, api_secret=body.api_secret, access_token=body.access_token
        ),
    )
    logger.info("broker_credentials.updated", broker=broker, updated_by=str(current_user.id))


@router.delete("/{broker}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_broker_credentials_endpoint(
    broker: str,
    current_user: User = Depends(require_role),
    store: SecretsStore = Depends(get_broker_credentials_store),
) -> None:
    _require_known_broker(broker)
    store.delete_credentials(broker)
    logger.info("broker_credentials.deleted", broker=broker, deleted_by=str(current_user.id))
