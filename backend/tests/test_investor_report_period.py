"""Which periods an investor report may be asked for: the rule, and the route that applies it."""

from datetime import date, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from src.core.roles import Role
from src.engine.paper_trading.market_hours import IST
from src.models.investor_report import InvestorReport
from src.orchestration.investor_report_period import (
    MAX_PERIOD_DAYS,
    ReportPeriodError,
    check_report_period,
)
from src.orchestration.investor_reporting import generate_investor_report

TODAY = date(2026, 11, 2)  # a Monday


def test_a_week_ending_today_and_a_month_are_accepted_and_the_days_counted_inclusively():
    assert check_report_period(date(2026, 10, 27), TODAY, "weekly", today=TODAY) == 7
    assert check_report_period(date(2026, 10, 1), date(2026, 10, 31), "monthly", today=TODAY) == 31
    assert check_report_period(TODAY, TODAY, "weekly", today=TODAY) == 1


def test_the_longest_period_of_each_cadence_is_the_scheduler_week_and_the_longest_month():
    assert MAX_PERIOD_DAYS == {"weekly": 7, "monthly": 31}


def test_a_period_that_ends_before_it_starts_is_refused_and_says_both_dates():
    with pytest.raises(ReportPeriodError) as refused:
        check_report_period(date(2026, 10, 20), date(2026, 10, 19), "weekly", today=TODAY)
    assert str(refused.value) == "The period ends (2026-10-19) before it starts (2026-10-20)."


def test_a_period_that_has_not_happened_yet_is_refused():
    with pytest.raises(ReportPeriodError) as refused:
        check_report_period(date(2026, 10, 30), date(2026, 11, 3), "weekly", today=TODAY)
    assert "2026-11-03" in str(refused.value) and "after today (2026-11-02)" in str(refused.value)


def test_a_period_longer_than_its_cadence_is_refused_by_exactly_one_day():
    check_report_period(date(2026, 10, 27), TODAY, "weekly", today=TODAY)
    with pytest.raises(ReportPeriodError) as refused:
        check_report_period(date(2026, 10, 26), TODAY, "weekly", today=TODAY)
    assert str(refused.value) == (
        "A weekly report covers at most 7 days; 2026-10-26 to 2026-11-02 is 8."
    )
    check_report_period(date(2026, 10, 3), TODAY, "monthly", today=TODAY)
    with pytest.raises(ReportPeriodError):
        check_report_period(date(2026, 10, 2), TODAY, "monthly", today=TODAY)


def test_a_cadence_that_is_not_one_of_the_two_is_refused():
    with pytest.raises(ReportPeriodError) as refused:
        check_report_period(date(2026, 10, 27), TODAY, "daily", today=TODAY)
    assert str(refused.value) == "The cadence must be weekly or monthly, not daily."


def _today_ist() -> date:
    return datetime.now(IST).date()


async def _login(client: AsyncClient, make_user, email: str, role: Role) -> dict[str, str]:
    await make_user(email, "supersecret1", role)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _reports_on(db_session_factory, start: date, end: date) -> int:
    async with db_session_factory() as db:
        return (
            await db.execute(
                select(func.count(InvestorReport.id)).where(
                    InvestorReport.period_start == start, InvestorReport.period_end == end
                )
            )
        ).scalar_one()


async def test_the_route_refuses_each_unreportable_period_with_its_reason_and_writes_nothing(
    client: AsyncClient, make_user, db_session_factory
):
    headers = await _login(client, make_user, "rp-pm@example.com", Role.PORTFOLIO_MANAGER)
    today = _today_ist()

    backwards = await client.post(
        "/api/v1/investor-reports/generate",
        json={
            "period_start": "2026-10-20",
            "period_end": "2026-10-19",
            "cadence": "weekly",
        },
        headers=headers,
    )
    future = await client.post(
        "/api/v1/investor-reports/generate",
        json={
            "period_start": today.isoformat(),
            "period_end": (today + timedelta(days=2)).isoformat(),
            "cadence": "weekly",
        },
        headers=headers,
    )
    too_long = await client.post(
        "/api/v1/investor-reports/generate",
        json={"period_start": "2026-01-01", "period_end": "2026-03-31", "cadence": "weekly"},
        headers=headers,
    )
    unknown_cadence = await client.post(
        "/api/v1/investor-reports/generate",
        json={"period_start": "2026-10-20", "period_end": "2026-10-26", "cadence": "daily"},
        headers=headers,
    )

    assert backwards.status_code == 422
    assert (
        backwards.json()["detail"] == "The period ends (2026-10-19) before it starts (2026-10-20)."
    )
    assert (
        future.status_code == 422
        and "nothing has happened on those days yet" in future.json()["detail"]
    )
    assert too_long.status_code == 422
    assert too_long.json()["detail"] == (
        "A weekly report covers at most 7 days; 2026-01-01 to 2026-03-31 is 90."
    )
    assert unknown_cadence.status_code == 422
    async with db_session_factory() as db:
        assert (await db.execute(select(func.count(InvestorReport.id)))).scalar_one() == 0


async def test_a_reportable_period_is_written_and_asking_again_writes_a_second_report(
    client: AsyncClient, make_user, db_session_factory
):
    headers = await _login(client, make_user, "rp-admin@example.com", Role.SYSTEM_ADMINISTRATOR)
    body = {"period_start": "2026-10-19", "period_end": "2026-10-25", "cadence": "weekly"}

    first = await client.post("/api/v1/investor-reports/generate", json=body, headers=headers)
    second = await client.post("/api/v1/investor-reports/generate", json=body, headers=headers)

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["content_markdown"].startswith(
        "# Investor Report: 2026-10-19 to 2026-10-25"
    )
    assert first.json()["generated_by"] == "user:rp-admin@example.com"
    assert await _reports_on(db_session_factory, date(2026, 10, 19), date(2026, 10, 25)) == 2


async def test_a_report_the_scheduler_writes_is_attributed_to_the_agent_not_to_a_person(
    db_session_factory,
):
    async with db_session_factory() as db:
        report = await generate_investor_report(
            db, period_start=date(2026, 10, 19), period_end=date(2026, 10, 25), cadence="weekly"
        )
    assert report.generated_by == "system:investor-reporting-agent"


async def test_a_long_address_is_cut_to_the_column_not_refused(client: AsyncClient, make_user):
    email = "a-very-long-address-" + "x" * 60 + "@example.com"
    headers = await _login(client, make_user, email, Role.PORTFOLIO_MANAGER)
    resp = await client.post(
        "/api/v1/investor-reports/generate",
        json={"period_start": "2026-10-19", "period_end": "2026-10-25", "cadence": "weekly"},
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["generated_by"] == f"user:{email}"[:64]


async def test_only_the_operator_roles_may_generate_a_report_and_the_others_get_nothing_written(
    client: AsyncClient, make_user, db_session_factory
):
    body = {"period_start": "2026-10-19", "period_end": "2026-10-25", "cadence": "weekly"}
    for index, role in enumerate([Role.READ_ONLY_AUDITOR, Role.RISK_MANAGER]):
        headers = await _login(client, make_user, f"rp-denied-{index}@example.com", role)
        refused = await client.post("/api/v1/investor-reports/generate", json=body, headers=headers)
        assert refused.status_code == 403, role
    assert await _reports_on(db_session_factory, date(2026, 10, 19), date(2026, 10, 25)) == 0
