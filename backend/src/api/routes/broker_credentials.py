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

from src.api.schemas import (
    BrokerCircuitBreakerStatusResponse,
    BrokerCredentialStatusResponse,
    WriteBrokerCredentialsRequest,
)
from src.brokers.base import BrokerCredentials
from src.brokers.breaker_registry import get_all_broker_circuit_breakers
from src.brokers.factory import KNOWN_BROKERS
from src.core.rbac import Role, register_policy, require_role
from src.engine.risk.circuit_breaker import DEFAULT_FAILURE_THRESHOLD, CircuitState
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
register_policy("GET", "/api/v1/broker-credentials/circuit-breaker", roles=list(Role))
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
    responses = []
    for broker in KNOWN_BROKERS:
        creds = store.get_credentials(broker)
        responses.append(
            BrokerCredentialStatusResponse(
                broker=broker,
                configured=creds is not None,
                token_status=creds.token_status if creds else "never-connected",
                token_expires_at=creds.token_expires_at if creds else None,
                redirect_uri=creds.redirect_uri if creds else None,
                token_duration=creds.token_duration if creds else None,
            )
        )
    return responses


@router.get("/circuit-breaker")
async def list_broker_circuit_breaker_status_endpoint(
    _current_user: User = Depends(require_role),
) -> list[BrokerCircuitBreakerStatusResponse]:
    """Real breaker state, not a fabricated placeholder: `src.brokers.factory
    .build_configured_adapter` now wraps every adapter it builds in the
    same per-broker singleton (src.brokers.breaker_registry) instead of a
    fresh, always-`CLOSED` instance on each call, so this reflects genuine
    consecutive-failure history across every real caller. A broker with no
    entry yet in the registry (no adapter has ever actually been built for
    it -- e.g. no credentials configured) is reported as `closed`/`0`
    rather than omitted, since that is the breaker's own true starting
    state and there is nothing dishonest about reporting it before the
    lazy singleton has been created."""
    breakers = get_all_broker_circuit_breakers()
    responses = []
    for broker in KNOWN_BROKERS:
        breaker = breakers.get(broker)
        if breaker is None:
            responses.append(
                BrokerCircuitBreakerStatusResponse(
                    broker=broker,
                    state=CircuitState.CLOSED.value,
                    consecutive_failures=0,
                    failure_threshold=DEFAULT_FAILURE_THRESHOLD,
                    cooldown_remaining_seconds=None,
                )
            )
            continue
        responses.append(
            BrokerCircuitBreakerStatusResponse(
                broker=broker,
                state=breaker.state.value,
                consecutive_failures=breaker.consecutive_failures,
                failure_threshold=breaker.failure_threshold,
                cooldown_remaining_seconds=breaker.cooldown_remaining_seconds(),
            )
        )
    return responses


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
