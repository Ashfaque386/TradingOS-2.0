"""Screener Agent tests (Phase 19, docs/phase19-audit.md Part 2.2): a real
filter against the real instrument master + real OHLCV bars, feeding
straight into the existing `run_strategy_pipeline` entrypoint.
"""

import pandas as pd
from sqlalchemy import select

from src.models.instrument import Instrument
from src.models.strategy import Strategy
from src.orchestration.screener import (
    ScreenerFilters,
    run_screener_and_generate_strategies,
    screen_instruments,
)


def _bars(*, rising: bool, days: int = 20, high_volume: bool = True) -> pd.DataFrame:
    idx = pd.date_range(end=pd.Timestamp.now().normalize(), periods=days, freq="D")
    if rising:
        closes = [100.0 + i for i in range(days)]
    else:
        closes = [100.0 - i * 0.1 for i in range(days)]
    volume = [50_000] * days if high_volume else [100] * days
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": volume},
        index=idx,
    )


class _FakeProvider:
    def __init__(self, bars_by_symbol: dict[str, pd.DataFrame]):
        self._bars = bars_by_symbol

    def daily_bars(self, symbol: str, *, as_of: pd.Timestamp, lookback_days: int) -> pd.DataFrame:
        return self._bars.get(symbol, pd.DataFrame())


async def _seed_instrument(db, symbol: str, *, instrument_type: str = "equity") -> None:
    db.add(
        Instrument(
            symbol=symbol,
            exchange="NSE",
            instrument_type=instrument_type,
            lot_size=1,
            tick_size=0.05,
            is_active=True,
        )
    )
    await db.flush()


async def test_screen_instruments_reports_real_reasons_for_every_candidate(db_session_factory):
    async with db_session_factory() as db:
        await _seed_instrument(db, "RISER")
        await _seed_instrument(db, "FALLER")
        await _seed_instrument(db, "ILLIQUID")
        await db.commit()

    provider = _FakeProvider(
        {
            "RISER": _bars(rising=True),
            "FALLER": _bars(rising=False),
            "ILLIQUID": _bars(rising=True, high_volume=False),
        }
    )

    async with db_session_factory() as db:
        results = await screen_instruments(
            db, provider, as_of=pd.Timestamp.now(), filters=ScreenerFilters()
        )

    by_symbol = {r.symbol: r for r in results}
    assert by_symbol["RISER"].passed is True
    assert by_symbol["RISER"].reasons == ()
    assert by_symbol["FALLER"].passed is False
    assert "SMA" in by_symbol["FALLER"].reasons[0]
    assert by_symbol["ILLIQUID"].passed is False
    assert "volume" in by_symbol["ILLIQUID"].reasons[0]


async def test_screener_never_filters_by_sector_or_market_cap():
    """Documents the audit's explicit finding: neither field exists in
    this codebase, so ScreenerFilters must never grow one without a real
    backing data source."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(ScreenerFilters)}
    assert "sector" not in field_names
    assert "market_cap" not in field_names
    assert "market_cap_proxy" not in field_names


async def test_run_screener_and_generate_strategies_creates_real_strategy_for_passing_candidate(
    db_session_factory,
):
    async with db_session_factory() as db:
        await _seed_instrument(db, "RISER")
        await db.commit()

    provider = _FakeProvider({"RISER": _bars(rising=True)})

    async with db_session_factory() as db:
        created = await run_screener_and_generate_strategies(
            db, provider, as_of=pd.Timestamp.now(), filters=ScreenerFilters()
        )
        assert len(created) == 1
        assert "RISER" in created[0]

        result = await db.execute(select(Strategy).where(Strategy.name == created[0]))
        strategy = result.scalars().first()
        assert strategy is not None
        assert "RISER" in strategy.objective
        assert strategy.created_by == "system:screener-agent"


async def test_run_screener_is_idempotent_for_the_same_as_of_date(db_session_factory):
    async with db_session_factory() as db:
        await _seed_instrument(db, "RISER")
        await db.commit()

    provider = _FakeProvider({"RISER": _bars(rising=True)})
    as_of = pd.Timestamp.now()

    async with db_session_factory() as db:
        first = await run_screener_and_generate_strategies(db, provider, as_of=as_of)
        second = await run_screener_and_generate_strategies(db, provider, as_of=as_of)

    assert len(first) == 1
    assert len(second) == 0  # already created for this as_of date, not duplicated


async def test_run_screener_produces_no_candidates_when_nothing_passes(db_session_factory):
    async with db_session_factory() as db:
        await _seed_instrument(db, "FALLER")
        await db.commit()

    provider = _FakeProvider({"FALLER": _bars(rising=False)})

    async with db_session_factory() as db:
        created = await run_screener_and_generate_strategies(db, provider, as_of=pd.Timestamp.now())

    assert created == []
