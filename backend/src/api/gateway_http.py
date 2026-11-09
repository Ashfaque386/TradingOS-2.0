"""How a gateway config edit speaks HTTP: the version the editor read, and the refusals.

Every console edit of the gateway config carries the version the person read, in an `If-Match`
header. A request without one is refused (428), because a write that does not say what it was based
on can silently undo someone else's; one whose version has moved on is refused (409) with nothing
written.
"""

from fastapi import Header, HTTPException, status

from src.gateway.edits import InvalidEdit, NothingToChange, UnknownAgent
from src.gateway.loader import ConfigLoadError
from src.gateway.service import ServiceError, StaleConfigError


def config_version_from_if_match(if_match: str | None = Header(default=None)) -> int:
    """The config version named in `If-Match` (`"12"`, `12` or `W/"12"`)."""
    if if_match is None or not if_match.strip():
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            "Send the configuration version you read in an If-Match header, so a change made "
            "since cannot be overwritten without your knowing.",
        )
    text = if_match.strip()
    if text.startswith("W/"):
        text = text[2:]
    text = text.strip('"')
    if not text.isdigit():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"If-Match must name a configuration version number, not {if_match!r}.",
        )
    return int(text)


def api_source(email: str, kind: str = "api") -> tuple[str, str]:
    """`(label, actor)` for an edit a person made in the console. The label is what the version
    history shows and fits the 32 characters a version stores (`api:ada@example.com`, cut if it is
    longer); the actor is the person's address, which is what the audit trail names (it used to say
    `cli` for an agent edit made in the console)."""
    return f"{kind}:{email}"[:32], email[:64]


def gateway_edit_error(exc: Exception) -> HTTPException:
    """The HTTP answer for a refused config edit."""
    if isinstance(exc, StaleConfigError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, UnknownAgent):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, InvalidEdit):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    if isinstance(exc, NothingToChange):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, ConfigLoadError):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            "The configuration file on disk is not valid, so nothing can be changed on top of it: "
            + "; ".join(exc.errors)
            + ". Fix it in the raw editor first.",
        )
    if isinstance(exc, ServiceError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    raise exc
