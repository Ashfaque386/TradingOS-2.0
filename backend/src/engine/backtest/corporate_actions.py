"""Corporate-action back-adjustment for OHLCV price series (Build Spec
§10): splits and bonus issues only.

**Dividends are explicitly out of scope for price adjustment here -- this
module must never be extended to approximate them.** A stock split or
bonus issue changes the number of shares outstanding and mechanically
divides the per-share price (multiplying volume) with no change in a
holder's total position value; the correct back-adjustment is a pure
ratio multiply applied uniformly to every bar before the action's ex-date,
which is exactly what `back_adjust_for_corporate_actions` below does. A
cash dividend is economically different: it's a cash distribution, not a
share-count change, and the "right" price adjustment for a total-return
calculation depends on reinvestment assumptions this engine does not
model. Silently back-adjusting for dividends the same way as a split
would understate historical prices and overstate this engine's own
price-return metrics without any caller having asked for that. A
total-return calculation that wants dividends included needs a separate,
explicit dividend-yield input -- not built here, and not something a
future change to this module should quietly bolt on.
"""

from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass(frozen=True, slots=True)
class CorporateAction:
    ex_date: date
    # ratio < 1 for a split/bonus that increases share count (e.g. a 1:2
    # split or a 1:1 bonus both halve the per-share price -> ratio=0.5).
    ratio: float

    def __post_init__(self) -> None:
        if self.ratio <= 0:
            raise ValueError(f"CorporateAction.ratio must be positive, got {self.ratio!r}")


def back_adjust_for_corporate_actions(
    prices: pd.DataFrame, actions: list[CorporateAction]
) -> pd.DataFrame:
    """`prices` must have a `DatetimeIndex` and OHLC + volume columns
    (`open`/`high`/`low`/`close`/`volume`; any subset present is
    adjusted). Every bar strictly before an action's `ex_date` has its
    price columns multiplied by that action's `ratio` and its volume
    divided by it; bars on/after `ex_date` are untouched by that
    particular action. Multiple actions compound in ex-date order (a bar
    before two splits gets both ratios applied), the standard
    back-adjustment methodology. Returns a new DataFrame -- `prices`
    itself is never mutated.
    """
    adjusted = prices.copy()
    price_cols = [c for c in ("open", "high", "low", "close") if c in adjusted.columns]

    for action in sorted(actions, key=lambda a: a.ex_date):
        cutoff = pd.Timestamp(action.ex_date)
        mask = adjusted.index < cutoff
        if price_cols:
            adjusted.loc[mask, price_cols] = adjusted.loc[mask, price_cols] * action.ratio
        if "volume" in adjusted.columns:
            adjusted.loc[mask, "volume"] = adjusted.loc[mask, "volume"] / action.ratio

    return adjusted
