"""Screener scheduler wiring test (regression for a real bug Phase 22's
run-now feature surfaced): `run_screener_job` crashed on every real
firing -- `TypeError: Cannot compare tz-naive and tz-aware datetime-like
objects` -- because it passed a tz-AWARE `as_of` (`pd.Timestamp.now(tz=IST)`
with no `.tz_localize(None)`) into `screen_instruments` /
`FakeDailyPriceProvider.daily_bars`, whose own index (and the real data
lake's, `src/data/lake.py`) is tz-naive. `live_trading_scheduler.py` and
`paper_trading_scheduler.py` already got this right; `screener_scheduler.py`
was the one outlier. This test exercises the real `FakeDailyPriceProvider`
(not a test double that ignores `as_of`, the way
`tests/test_orchestration_screener.py`'s `_FakeProvider` does -- which is
exactly why unit tests never caught this) the same way
`test_orchestration_paper_trading_scheduler.py::test_daily_signal_job_runs_without_any_manual_trigger`
already does for its own scheduler.
"""

from src.engine.paper_trading.price_data import FakeDailyPriceProvider
from src.models.instrument import Instrument
from src.orchestration.screener import ScreenerFilters
from src.orchestration.screener_scheduler import run_screener_job


async def test_screener_job_runs_without_any_manual_trigger(db_session_factory):
    async with db_session_factory() as db:
        db.add(
            Instrument(
                symbol="SCREENSTOCK",
                exchange="NSE",
                instrument_type="equity",
                lot_size=1,
                tick_size=0.05,
                is_active=True,
            )
        )
        await db.commit()

    # The real fallback provider -- FakeDailyPriceProvider.daily_bars does
    # `self._full_series(symbol).loc[:as_of]` against a tz-naive index,
    # which is exactly what raised before the fix.
    price_provider = FakeDailyPriceProvider(seed_by_symbol={"SCREENSTOCK": 1})

    # Must complete without raising -- before the fix this always crashed
    # given the scheduler's own real `as_of` construction (a tz-aware
    # `pd.Timestamp.now(tz=IST).normalize()`, unchanged by this test).
    await run_screener_job(db_session_factory, price_provider, ScreenerFilters())
