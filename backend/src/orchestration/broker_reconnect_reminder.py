"""A morning nudge to sign the brokers in again before the market opens.

Broker access tokens are short-lived by design: a Zerodha token dies at about 06:00 IST every day
(Kite offers no way around it), and an Upstox one the same morning unless the extended duration was
chosen. Nothing in the app told the operator -- the Settings card says "Expired -- click to
reconnect", but only to someone who opens the page. Missing it means the tick feed has no
quotes at 09:15 and a live order has no broker to go to.

Twice each trading morning (08:30 and 09:00 IST) this checks every broker that has a stored token
and sends one message, through the existing notification channels, naming the ones that need a new
login. Nothing to report means nothing is sent: a reminder that cried wolf would be muted within a
week. A broker with no token at all (keys saved, never connected) is left alone -- that is a
choice, not an expiry.

It rides the `daily` alert level, the closest of the four fixed levels (Build Spec §18, the exact
set Settings > Notification Channels offers): a once-a-day operational message to the people who
already asked for the daily one. A fifth level would change that spec'd screen and every stored
channel's subscriptions, which is a decision for the operator, not a side effect of a reminder.

The message carries broker names, times and a link only -- never a token, key or secret.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime

import structlog
from apscheduler.triggers.cron import CronTrigger

from src.brokers import feed_health
from src.brokers.base import BrokerCredentials
from src.brokers.factory import KNOWN_BROKERS
from src.core.config import get_settings
from src.data.nse_calendar import is_nse_trading_day
from src.engine.paper_trading.market_hours import IST, MARKET_CLOSE_IST, MARKET_OPEN_IST
from src.notifications.dispatch import notify
from src.notifications.types import AlertLevel
from src.security.secrets_store import SecretsStoreError, get_secrets_store

logger = structlog.get_logger(__name__)

# (hour, minute) in IST: early enough to act in good time, then once more a quarter of an hour
# before the open, only if still needed.
REMINDER_TIMES_IST = ((8, 30), (9, 0))
REMINDER_JOB_ID_PREFIX = "notifications_broker_reconnect"


@dataclass(frozen=True, slots=True)
class ReconnectNeed:
    broker: str
    reason: str


def _ist_clock(moment: datetime, now: datetime) -> str:
    """ "06:00 IST" for today; with the day ("Sat 17 Oct 06:00 IST") for any other, so a Monday
    message about a token that died on Saturday does not read as though it died this morning."""
    moment_ist = moment.astimezone(IST)
    if moment_ist.date() == now.astimezone(IST).date():
        return moment_ist.strftime("%H:%M IST")
    return moment_ist.strftime("%a %d %b %H:%M IST")


def reconnect_needs(
    now: datetime,
    credentials: dict[str, BrokerCredentials | None],
    rejections: dict[str, feed_health.FeedRejection | None] | None = None,
) -> list[ReconnectNeed]:
    """The brokers that need a fresh login for today's session, in `KNOWN_BROKERS` order.

    Needs one: the stored token has expired; or it will expire before today's close (a login made
    yesterday afternoon on a short-lived token would otherwise be called "valid" at 08:30 and die
    mid-session); or the broker refused it when last used. A broker with no credentials, or only
    keys and no token, is not nagged about."""
    rejections = rejections or {}
    close = datetime.combine(now.astimezone(IST).date(), MARKET_CLOSE_IST, tzinfo=IST)
    needs: list[ReconnectNeed] = []
    for broker in KNOWN_BROKERS:
        creds = credentials.get(broker)
        if creds is None or not creds.access_token:
            continue
        rejection = rejections.get(broker)
        expires_at = _expiry(creds)
        if expires_at is not None and expires_at <= now:  # judged at `now`, not the wall clock
            when = f" at {_ist_clock(expires_at, now)}" if expires_at else ""
            needs.append(ReconnectNeed(broker, f"the token expired{when}"))
        elif rejection is not None:
            needs.append(
                ReconnectNeed(
                    broker, f"the broker rejected the stored token (HTTP {rejection.status_code})"
                )
            )
        elif expires_at is not None and expires_at <= close:
            needs.append(
                ReconnectNeed(
                    broker, f"the token expires at {_ist_clock(expires_at, now)}, before the close"
                )
            )
    return needs


def _expiry(creds: BrokerCredentials) -> datetime | None:
    if not creds.token_expires_at:
        return None
    try:
        expires_at = datetime.fromisoformat(creds.token_expires_at)
    except ValueError:
        return None
    return expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)


def build_message(
    needs: list[ReconnectNeed], now: datetime, *, settings_url: str | None
) -> tuple[str, str]:
    """(title, body) -- names, times and a link; never a credential."""
    now_ist = now.astimezone(IST)
    opens = datetime.combine(now_ist.date(), MARKET_OPEN_IST, tzinfo=IST)
    minutes = int((opens - now_ist).total_seconds() // 60)
    if minutes > 0:
        title = "Broker sign-in needed before the market opens"
        timing = f"The market opens at {MARKET_OPEN_IST.strftime('%H:%M')} IST ({minutes} min)."
    else:
        title = "Broker sign-in needed -- the market is open"
        timing = "The market is already open."
    lines = [f"{n.broker.capitalize()}: {n.reason}." for n in needs]
    where = settings_url or "Settings > Broker Config"
    return title, " ".join([*lines, timing, f"Reconnect: {where}"])


def _settings_url() -> str | None:
    base = get_settings().frontend_base_url
    return f"{base.rstrip('/')}/settings" if base else None


async def run_broker_reconnect_reminder_job(now: datetime | None = None) -> list[ReconnectNeed]:
    """One check; sends the message only if some broker needs a login. Returns what it found."""
    now = now or datetime.now(UTC)
    today: date = now.astimezone(IST).date()
    if not is_nse_trading_day(today):
        return []
    try:
        store = get_secrets_store()
    except SecretsStoreError:
        return []  # no encryption key configured: nothing stored, nothing to remind about
    credentials = {broker: store.get_credentials(broker) for broker in KNOWN_BROKERS}
    rejections = {broker: feed_health.get_rejection(broker) for broker in KNOWN_BROKERS}
    needs = reconnect_needs(now, credentials, rejections)
    if not needs:
        logger.info("notifications.broker_reconnect_reminder_nothing_needed")
        return []
    title, body = build_message(needs, now, settings_url=_settings_url())
    results = await notify(
        AlertLevel.DAILY,
        title=title,
        body=body,
        details={"brokers": ", ".join(n.broker for n in needs)},
    )
    logger.info(
        "notifications.broker_reconnect_reminder_sent",
        brokers=[n.broker for n in needs],
        channels_ok=sum(1 for r in results if r.ok),
        channels_failed=sum(1 for r in results if not r.ok),
    )
    return needs


def reminder_triggers() -> list[tuple[str, CronTrigger]]:
    """(job id, trigger) for each reminder time, Mon-Fri (holidays are checked when it runs)."""
    return [
        (
            f"{REMINDER_JOB_ID_PREFIX}_{hour:02d}{minute:02d}",
            CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone=IST),
        )
        for hour, minute in REMINDER_TIMES_IST
    ]
