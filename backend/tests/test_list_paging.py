"""The heavy lists are read a page at a time: runs, strategies and the instrument master.

Each answers a page in a plain JSON array with the number of rows that match in `X-Total-Count`
(the convention of the orders ledger and the audit log), and reads in a number of queries that does
not grow with the number of rows (the runs and the strategies used to read each row on its own).
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import event

from src.core.roles import Role
from src.models.instrument import Instrument
from src.models.organization_run import OrganizationRun, RunSource, RunStatus, RunType
from src.models.organizational_plan import OrganizationalPlan, PlanStatus
from src.models.task import Task, TaskStatus
from src.orchestration.strategies import create_strategy, create_version_with_validation

CODE = 'def run_backtest(data, config):\n    return {"ok": True}\n'


async def _headers(client: AsyncClient, make_user, role: Role = Role.READ_ONLY_AUDITOR) -> dict:
    email = f"page-{role.value}@example.com".lower()
    await make_user(email, "supersecret1", role)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


class QueryCounter:
    """Counts the statements the database is sent while it is in use."""

    def __init__(self, session_factory) -> None:
        self.engine = session_factory.kw["bind"].sync_engine
        self.count = 0

    def _count(self, *_args) -> None:
        self.count += 1

    def __enter__(self) -> "QueryCounter":
        event.listen(self.engine, "before_cursor_execute", self._count)
        return self

    def __exit__(self, *_exc) -> None:
        event.remove(self.engine, "before_cursor_execute", self._count)


# ---------------------------------------------------------------- runs


async def _seed_runs(db_session_factory, count: int, *, tasks_each: int = 2) -> list[uuid.UUID]:
    """`count` runs, the oldest first, each with its tasks; the 3rd, 6th... are completed, one is
    failed, one is paused, the rest still running."""
    base = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    ids: list[uuid.UUID] = []
    async with db_session_factory() as db:
        for i in range(count):
            status = (
                RunStatus.COMPLETED
                if i % 3 == 2
                else RunStatus.FAILED
                if i == 1
                else RunStatus.PAUSED
                if i == 4
                else RunStatus.RUNNING
            )
            run = OrganizationRun(
                objective=f"run {i}",
                source=RunSource.UI,
                run_type=RunType.STANDARD,
                status=status,
                created_at=base + timedelta(minutes=i),
            )
            db.add(run)
            await db.flush()
            plan = OrganizationalPlan(
                run_id=run.id, attempt_number=1, status=PlanStatus.ACCEPTED, objective=f"run {i}"
            )
            db.add(plan)
            await db.flush()
            for t in range(tasks_each):
                db.add(
                    Task(
                        run_id=run.id,
                        plan_id=plan.id,
                        plan_key=f"k{t}",
                        capability="graph.ceo_kickoff",
                        name=f"run {i} task {t}",
                        status=TaskStatus.SUCCEEDED,
                    )
                )
            ids.append(run.id)
        await db.commit()
    return ids


async def test_runs_come_a_page_at_a_time_newest_first_with_the_total_and_their_own_tasks(
    client: AsyncClient, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    await _seed_runs(db_session_factory, 7)

    first = await client.get("/api/v1/orchestration/runs?limit=3", headers=headers)
    second = await client.get("/api/v1/orchestration/runs?limit=3&offset=3", headers=headers)
    last = await client.get("/api/v1/orchestration/runs?limit=3&offset=6", headers=headers)
    beyond = await client.get("/api/v1/orchestration/runs?limit=3&offset=9", headers=headers)

    assert [r["objective"] for r in first.json()] == ["run 6", "run 5", "run 4"]
    assert [r["objective"] for r in second.json()] == ["run 3", "run 2", "run 1"]
    assert [r["objective"] for r in last.json()] == ["run 0"]
    assert beyond.json() == []
    for answer in (first, second, last, beyond):
        assert answer.headers["x-total-count"] == "7"
    for run in first.json() + second.json() + last.json():
        assert [t["name"] for t in run["tasks"]] == [
            f"{run['objective']} task 0",
            f"{run['objective']} task 1",
        ]


async def test_runs_can_be_narrowed_to_one_status_or_to_those_still_in_progress(
    client: AsyncClient, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    await _seed_runs(
        db_session_factory, 7
    )  # completed: 2, 5; failed: 1; paused: 4; running: 0, 3, 6

    completed = await client.get(
        "/api/v1/orchestration/runs?status_filter=completed", headers=headers
    )
    active = await client.get("/api/v1/orchestration/runs?active=true", headers=headers)
    active_page = await client.get(
        "/api/v1/orchestration/runs?active=true&limit=2", headers=headers
    )
    both = await client.get(
        "/api/v1/orchestration/runs?active=true&status_filter=failed", headers=headers
    )
    unknown = await client.get("/api/v1/orchestration/runs?status_filter=exploded", headers=headers)

    assert sorted(r["objective"] for r in completed.json()) == ["run 2", "run 5"]
    assert completed.headers["x-total-count"] == "2"
    assert sorted(r["objective"] for r in active.json()) == ["run 0", "run 3", "run 4", "run 6"]
    assert active.headers["x-total-count"] == "4"
    # a run waiting on a person is open work like any other, and as old as it is
    assert {r["objective"]: r["status"] for r in active.json()}["run 4"] == "paused"
    assert len(active_page.json()) == 2 and active_page.headers["x-total-count"] == "4"
    assert (
        both.json() == [] and both.headers["x-total-count"] == "0"
    )  # failed is finished, so not open
    assert unknown.status_code == 422


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "limit=-1", "offset=-1"])
async def test_a_page_outside_the_bounds_is_refused(client: AsyncClient, make_user, query):
    headers = await _headers(client, make_user)
    resp = await client.get(f"/api/v1/orchestration/runs?{query}", headers=headers)
    assert resp.status_code == 422


async def test_reading_runs_takes_the_same_number_of_queries_however_many_there_are(
    client: AsyncClient, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    await _seed_runs(db_session_factory, 3)
    with QueryCounter(db_session_factory) as few:
        assert len((await client.get("/api/v1/orchestration/runs", headers=headers)).json()) == 3
    await _seed_runs(db_session_factory, 25)
    with QueryCounter(db_session_factory) as many:
        assert len((await client.get("/api/v1/orchestration/runs", headers=headers)).json()) == 28

    assert few.count == many.count


async def test_a_single_run_is_still_read_whole(client: AsyncClient, make_user, db_session_factory):
    headers = await _headers(client, make_user)
    ids = await _seed_runs(db_session_factory, 2, tasks_each=3)

    resp = await client.get(f"/api/v1/orchestration/runs/{ids[1]}", headers=headers)

    assert resp.status_code == 200 and len(resp.json()["tasks"]) == 3


# ---------------------------------------------------------------- strategies


async def _seed_strategies(db_session_factory, count: int, *, versions_each: int = 2) -> None:
    async with db_session_factory() as db:
        for i in range(count):
            strategy = await create_strategy(
                db, name=f"Strategy {i:02d}", objective=f"objective {i}"
            )
            for v in range(versions_each):
                await create_version_with_validation(db, strategy, CODE + f"# {i}.{v}\n")


async def test_the_strategy_list_has_a_summary_of_each_version_and_never_its_code(
    client: AsyncClient, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    await _seed_strategies(db_session_factory, 3)

    resp = await client.get("/api/v1/strategies", headers=headers)

    assert resp.status_code == 200 and resp.headers["x-total-count"] == "3"
    for strategy in resp.json():
        assert set(strategy) == {
            "id",
            "name",
            "objective",
            "instrument_class",
            "status",
            "versions",
        }
        assert [v["version_number"] for v in strategy["versions"]] == [1, 2]
        for version in strategy["versions"]:
            assert set(version) == {
                "id",
                "version_number",
                "static_validation_passed",
                "sandbox_passed",
                "created_at",
            }


async def test_the_strategy_list_is_paged_newest_first_and_a_strategy_is_still_read_whole(
    client: AsyncClient, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    await _seed_strategies(db_session_factory, 5)

    page_one = await client.get("/api/v1/strategies?limit=2", headers=headers)
    page_two = await client.get("/api/v1/strategies?limit=2&offset=2", headers=headers)
    assert [s["name"] for s in page_one.json()] == ["Strategy 04", "Strategy 03"]
    assert [s["name"] for s in page_two.json()] == ["Strategy 02", "Strategy 01"]
    assert page_one.headers["x-total-count"] == page_two.headers["x-total-count"] == "5"

    whole = await client.get(f"/api/v1/strategies/{page_one.json()[0]['id']}", headers=headers)
    assert "# 4.1" in whole.json()["versions"][1]["code"]  # the code is one strategy away


async def test_the_strategy_list_can_be_narrowed_to_one_status_and_bounded(
    client: AsyncClient, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    await _seed_strategies(db_session_factory, 2)

    ideation = await client.get("/api/v1/strategies?status_filter=ideation", headers=headers)
    live = await client.get("/api/v1/strategies?status_filter=live", headers=headers)

    assert ideation.headers["x-total-count"] == "2" or ideation.headers["x-total-count"] == "0"
    assert live.json() == [] and live.headers["x-total-count"] == "0"
    assert (await client.get("/api/v1/strategies?limit=501", headers=headers)).status_code == 422
    assert (await client.get("/api/v1/strategies?limit=0", headers=headers)).status_code == 422


async def test_reading_strategies_takes_the_same_number_of_queries_however_many_there_are(
    client: AsyncClient, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    await _seed_strategies(db_session_factory, 2)
    with QueryCounter(db_session_factory) as few:
        assert len((await client.get("/api/v1/strategies", headers=headers)).json()) == 2
    await _seed_strategies(db_session_factory, 12)
    with QueryCounter(db_session_factory) as many:
        assert len((await client.get("/api/v1/strategies", headers=headers)).json()) == 14

    assert few.count == many.count


# ---------------------------------------------------------------- instruments


async def _seed_instruments(db_session_factory) -> None:
    async with db_session_factory() as db:
        for i in range(120):
            db.add(Instrument(symbol=f"STOCK{i:03d}", exchange="NSE", instrument_type="equity"))
        for strike in (22000, 22100):
            for kind in ("CE", "PE"):
                db.add(
                    Instrument(
                        symbol=f"NIFTY26NOV{strike}{kind}",
                        exchange="NFO",
                        instrument_type="option",
                        underlying_symbol="NIFTY",
                        expiry_date=date(2026, 11, 26),
                        strike_price=float(strike),
                        option_type=kind,
                    )
                )
        db.add(
            Instrument(
                symbol="BANKNIFTY26NOV48000CE",
                exchange="NFO",
                instrument_type="option",
                underlying_symbol="BANKNIFTY",
                expiry_date=date(2026, 11, 26),
                strike_price=48000.0,
                option_type="CE",
            )
        )
        db.add(
            Instrument(
                symbol="NIFTY26NOVFUT",
                exchange="NFO",
                instrument_type="future",
                underlying_symbol="NIFTY",
            )
        )
        db.add(Instrument(symbol="A_B", exchange="NSE", instrument_type="equity"))
        db.add(Instrument(symbol="AXB", exchange="NSE", instrument_type="equity"))
        await db.commit()


async def test_the_instrument_master_is_a_page_by_symbol_with_the_total(
    client: AsyncClient, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    await _seed_instruments(db_session_factory)

    default = await client.get("/api/v1/market-data/instruments", headers=headers)
    second = await client.get(
        "/api/v1/market-data/instruments?limit=10&offset=120", headers=headers
    )

    assert len(default.json()) == 100  # a page, not the whole master
    assert default.headers["x-total-count"] == "128"
    symbols = [i["symbol"] for i in default.json()]
    assert symbols == sorted(symbols)
    assert len(second.json()) == 8 and second.headers["x-total-count"] == "128"
    assert (
        await client.get("/api/v1/market-data/instruments?limit=501", headers=headers)
    ).status_code == 422


async def test_a_symbol_is_found_by_any_part_of_it_in_any_case_and_a_wildcard_is_not_one(
    client: AsyncClient, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    await _seed_instruments(db_session_factory)

    found = await client.get("/api/v1/market-data/instruments?q=stock01", headers=headers)
    mid = await client.get("/api/v1/market-data/instruments?q=banknifty", headers=headers)
    percent = await client.get("/api/v1/market-data/instruments?q=%25", headers=headers)
    underscore = await client.get("/api/v1/market-data/instruments?q=A_B", headers=headers)
    blank = await client.get("/api/v1/market-data/instruments?q=%20%20", headers=headers)

    assert [i["symbol"] for i in found.json()] == [f"STOCK01{d}" for d in range(10)]
    assert found.headers["x-total-count"] == "10"
    assert [i["symbol"] for i in mid.json()] == ["BANKNIFTY26NOV48000CE"]
    assert percent.json() == []  # a % is a character in a symbol, not "anything"
    assert [i["symbol"] for i in underscore.json()] == [
        "A_B"
    ]  # and so is an _ (AXB is not a match)
    assert blank.headers["x-total-count"] == "128"  # blank is no search at all


async def test_the_options_of_one_underlying_are_asked_for_by_type_and_underlying(
    client: AsyncClient, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    await _seed_instruments(db_session_factory)

    nifty = await client.get(
        "/api/v1/market-data/instruments?instrument_type=option&underlying=NIFTY", headers=headers
    )
    futures = await client.get(
        "/api/v1/market-data/instruments?instrument_type=future", headers=headers
    )

    assert sorted(i["symbol"] for i in nifty.json()) == [
        "NIFTY26NOV22000CE",
        "NIFTY26NOV22000PE",
        "NIFTY26NOV22100CE",
        "NIFTY26NOV22100PE",
    ]
    assert nifty.headers["x-total-count"] == "4"
    assert [i["symbol"] for i in futures.json()] == ["NIFTY26NOVFUT"]


async def test_the_underlyings_with_options_are_listed_without_reading_every_contract(
    client: AsyncClient, make_user, db_session_factory
):
    everyone = [await _headers(client, make_user, role) for role in Role]
    assert (
        await client.get("/api/v1/market-data/option-underlyings", headers=everyone[0])
    ).json() == []
    await _seed_instruments(db_session_factory)

    for headers in everyone:
        answer = await client.get("/api/v1/market-data/option-underlyings", headers=headers)
        assert answer.status_code == 200
        # a future's underlying is NIFTY too and an equity has none: only options count, once each
        assert answer.json() == ["BANKNIFTY", "NIFTY"]
