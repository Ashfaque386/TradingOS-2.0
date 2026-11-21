"""What the 2026-11-21 review of the running console found in the scheduled jobs and the data
they keep, fixed: the weekly report a sleeping machine skipped, the heartbeat log nothing
pruned, a config version written on every restart, and the e2e seed script that would run
against any database."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

import src.agents.heartbeat as heartbeat_module
from scripts import seed_e2e_users
from src.agents.heartbeat import run_heartbeat
from src.gateway.apply import apply_config_text
from src.gateway.state import get_state
from src.models.agent_config_version import AgentConfigVersion
from src.models.agent_identity import AgentIdentity
from src.models.heartbeat_log import HeartbeatLog
from src.orchestration.investor_reporting_scheduler import (
    INVESTOR_REPORT_JOB_ID,
    _prior_week,
    start_investor_reporting_scheduler,
)
from src.orchestration.job_schedule import RUN_LATE_GRACE_SECONDS
from src.orchestration.notification_scheduler import (
    DAILY_SUMMARY_JOB_ID,
    start_notification_scheduler,
)
from src.orchestration.post_trade_review_scheduler import (
    POST_TRADE_REVIEW_JOB_ID,
    start_post_trade_review_scheduler,
)

# --- the weekly investor report ------------------------------------------------------------------

LAST_WEEK = (date(2026, 11, 16), date(2026, 11, 22))  # Mon..Sun before Monday 2026-11-23


@pytest.mark.parametrize(
    "run_on",
    [
        date(2026, 11, 23),  # Monday: the scheduled day
        date(2026, 11, 24),  # Tuesday: the Monday run made on waking, a day late
        date(2026, 11, 29),  # Sunday: the latest day still in the same week
    ],
)
def test_the_report_covers_the_week_before_this_weeks_monday_however_late_it_runs(run_on):
    assert _prior_week(run_on) == LAST_WEEK


def _options(job) -> tuple[float | None, bool]:
    return job.misfire_grace_time, job.coalesce


async def test_the_weekly_report_runs_late_and_the_daily_today_readers_do_not(db_session_factory):
    started = [
        (start_investor_reporting_scheduler(db_session_factory), INVESTOR_REPORT_JOB_ID),
        (start_post_trade_review_scheduler(db_session_factory), POST_TRADE_REVIEW_JOB_ID),
        (start_notification_scheduler(db_session_factory), DAILY_SUMMARY_JOB_ID),
    ]
    try:
        weekly, review, summary = (scheduler.get_job(job_id) for scheduler, job_id in started)
        assert _options(weekly) == (RUN_LATE_GRACE_SECONDS, True)
        # The daily review and summary read "today": run after midnight they would describe the
        # wrong day, so they keep APScheduler's default and are skipped when missed.
        assert review.misfire_grace_time == 1
        assert summary.misfire_grace_time == 1
    finally:
        for scheduler, _ in started:
            scheduler.shutdown(wait=False)


# --- the heartbeat log ---------------------------------------------------------------------------


async def _count(db_session_factory, agent_id: str) -> int:
    async with db_session_factory() as db:
        return await db.scalar(
            select(func.count()).select_from(HeartbeatLog).where(HeartbeatLog.agent_id == agent_id)
        )


async def test_each_agent_keeps_only_its_newest_heartbeats(db_session_factory, monkeypatch):
    monkeypatch.setattr(heartbeat_module, "HEARTBEAT_LOGS_KEPT_PER_AGENT", 3)
    async with db_session_factory() as db:
        db.add(AgentIdentity(agent_id="ceo-agent", name="CEO Agent"))
        db.add(AgentIdentity(agent_id="risk-manager", name="Risk Manager"))
        await db.commit()

    made = []
    for _ in range(5):
        async with db_session_factory() as db:
            made.append((await run_heartbeat(db, "ceo-agent")).id)
    for _ in range(2):
        async with db_session_factory() as db:
            await run_heartbeat(db, "risk-manager")

    assert await _count(db_session_factory, "ceo-agent") == 3
    assert await _count(db_session_factory, "risk-manager") == 2  # another agent's are not touched
    async with db_session_factory() as db:
        kept = set(
            (await db.execute(select(HeartbeatLog.id).where(HeartbeatLog.agent_id == "ceo-agent")))
            .scalars()
            .all()
        )
    assert kept == set(made[-3:])


def test_a_week_of_heartbeats_is_kept_at_the_default_interval():
    assert heartbeat_module.HEARTBEAT_LOGS_KEPT_PER_AGENT == 7 * 24 * 12


# --- config versions on restart ------------------------------------------------------------------

CONFIG = """
{
  version: 1,
  infra: {
    llmProviders: { order: ['anthropic'] },
    brokerFailover: { primary: 'zerodha', fallback: 'upstox' },
    riskThresholdRefs: { maxDrawdownPct: 15, wsLatencyMs: 100 },
  },
}
"""


async def _versions(db_session_factory) -> list[AgentConfigVersion]:
    async with db_session_factory() as db:
        return list(
            (await db.execute(select(AgentConfigVersion).order_by(AgentConfigVersion.id)))
            .scalars()
            .all()
        )


async def test_a_restart_with_the_same_file_adds_no_version(db_session_factory):
    async with db_session_factory() as db:
        first = await apply_config_text(db, CONFIG, source="startup")
    async with db_session_factory() as db:
        again = await apply_config_text(db, CONFIG, source="startup")

    assert again.version_id == first.version_id
    assert [v.id for v in await _versions(db_session_factory)] == [first.version_id]
    assert get_state().get_version_id() == first.version_id


async def test_a_restart_with_a_changed_file_and_a_console_save_each_add_one(db_session_factory):
    async with db_session_factory() as db:
        first = await apply_config_text(db, CONFIG, source="startup")
    changed = CONFIG.replace("['anthropic']", "['anthropic', 'openai']")
    async with db_session_factory() as db:
        second = await apply_config_text(db, changed, source="startup")
    # A save from the console is a decision someone made, so it is recorded even when the text
    # did not change; only a restart re-reading the file is folded into the version in force.
    async with db_session_factory() as db:
        third = await apply_config_text(db, changed, source="api", actor="someone@example.com")

    assert len({first.version_id, second.version_id, third.version_id}) == 3
    assert len(await _versions(db_session_factory)) == 3
    assert get_state().get_version_id() == third.version_id


# --- the e2e seed script -------------------------------------------------------------------------


def test_the_seed_needs_an_explicit_yes_for_this_database():
    with pytest.raises(seed_e2e_users.SeedRefusedError):
        seed_e2e_users.require_explicit_permission({})
    with pytest.raises(seed_e2e_users.SeedRefusedError):
        seed_e2e_users.require_explicit_permission({"TRADINGOS_E2E_SEED": "yes"})
    seed_e2e_users.require_explicit_permission({"TRADINGOS_E2E_SEED": "1"})


async def test_a_refused_seed_never_opens_the_database(monkeypatch):
    monkeypatch.delenv("TRADINGOS_E2E_SEED", raising=False)

    def _must_not_connect():
        raise AssertionError("the database was opened before the refusal")

    monkeypatch.setattr(seed_e2e_users, "get_session_factory", _must_not_connect)
    with pytest.raises(seed_e2e_users.SeedRefusedError):
        await seed_e2e_users.seed()


# --- the engine-alert watcher's first look --------------------------------------------------------


async def test_the_watchers_first_look_comes_within_seconds_of_a_start(db_session_factory):
    from src.orchestration.engine_alert_watcher import (
        FIRST_LOOK_DELAY_SECONDS,
        start_engine_alert_watcher,
    )

    scheduler = start_engine_alert_watcher(db_session_factory, redis=None, price_provider=None)  # type: ignore[arg-type]
    try:
        [job] = scheduler.get_jobs()
        wait = (job.next_run_time - datetime.now(UTC)).total_seconds()
        assert 0 <= wait <= FIRST_LOOK_DELAY_SECONDS <= 10
    finally:
        scheduler.shutdown(wait=False)
