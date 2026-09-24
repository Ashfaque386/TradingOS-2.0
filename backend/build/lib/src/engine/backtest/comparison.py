"""Multi-run comparison (Build Spec §10): a pairwise return-correlation
matrix across backtest runs. Any pair with fewer than 10 overlapping
trading days gets `None` -- **never** a fabricated `0` (uncorrelated) or
`1` (perfectly correlated) -- because a correlation computed from a
handful of overlapping points isn't a meaningful number, and reporting a
concrete value in its place would misrepresent "not enough data" as an
actual finding. The same `None` applies when either run's returns are
constant over the overlap (zero variance makes correlation undefined,
not zero).
"""

from dataclasses import dataclass, field

import pandas as pd

MIN_OVERLAPPING_DAYS = 10


@dataclass(frozen=True, slots=True)
class ComparisonMatrix:
    run_ids: list[str]
    # Keyed by an unordered pair (frozenset of the two run ids) -> the
    # correlation of their daily returns over the overlapping period, or
    # None per the module docstring.
    correlations: dict[frozenset[str], float | None] = field(default_factory=dict)

    def get(self, a: str, b: str) -> float | None:
        if a == b:
            return 1.0
        return self.correlations.get(frozenset((a, b)))


def compare_runs(daily_returns_by_run: dict[str, pd.Series]) -> ComparisonMatrix:
    run_ids = list(daily_returns_by_run)
    correlations: dict[frozenset[str], float | None] = {}

    for i, a in enumerate(run_ids):
        for b in run_ids[i + 1 :]:
            series_a = daily_returns_by_run[a]
            series_b = daily_returns_by_run[b]
            aligned = pd.concat([series_a, series_b], axis=1, join="inner").dropna()

            if len(aligned) < MIN_OVERLAPPING_DAYS:
                correlations[frozenset((a, b))] = None
                continue

            corr = aligned.iloc[:, 0].corr(aligned.iloc[:, 1])
            correlations[frozenset((a, b))] = float(corr) if pd.notna(corr) else None

    return ComparisonMatrix(run_ids=run_ids, correlations=correlations)
