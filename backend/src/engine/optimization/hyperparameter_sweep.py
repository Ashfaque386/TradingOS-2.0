"""Optuna hyperparameter sweep with fANOVA parameter importance (Build
Spec §10). Each trial is a **real, complete run of the vectorized
backtest engine** against that trial's sampled parameters -- no
shortcutting (evaluating on a subsample of bars, or a cheap proxy metric
standing in for an actual backtest). fANOVA importance
(`optuna.importance.FanovaImportanceEvaluator`) is computed once, after
the sweep, over the real completed trials -- it explains how much of the
objective's variance each parameter accounts for, not a guessed ranking.
"""

from collections.abc import Callable
from dataclasses import dataclass

import optuna
import pandas as pd
from optuna.importance import FanovaImportanceEvaluator

from src.engine.backtest.engine import run_vectorized_backtest
from src.engine.backtest.friction import FrictionModel

optuna.logging.set_verbosity(optuna.logging.WARNING)

# Suggests and returns this trial's sampled parameters (calls
# trial.suggest_*, does not run anything itself).
ParamSpaceFn = Callable[[optuna.Trial], dict]
# Builds a position-signal series from price data + one set of sampled
# parameters -- the thing being optimized.
SignalFromParamsFn = Callable[[pd.DataFrame, dict], pd.Series]


@dataclass(frozen=True, slots=True)
class SweepResult:
    n_trials: int
    best_params: dict
    best_value: float | None
    param_importance: dict[str, float]


def run_hyperparameter_sweep(
    prices: pd.DataFrame,
    param_space_fn: ParamSpaceFn,
    signal_from_params_fn: SignalFromParamsFn,
    *,
    n_trials: int = 50,
    initial_capital: float = 100_000.0,
    is_delivery: bool = True,
    friction_model: FrictionModel = FrictionModel(),
    objective_metric: str = "sharpe",
    seed: int | None = None,
) -> SweepResult:
    sampler = optuna.samplers.TPESampler(seed=seed)
    study = optuna.create_study(direction="maximize", sampler=sampler)

    def objective(trial: optuna.Trial) -> float:
        params = param_space_fn(trial)
        signals = signal_from_params_fn(prices, params)
        result = run_vectorized_backtest(
            prices,
            signals,
            initial_capital=initial_capital,
            is_delivery=is_delivery,
            friction_model=friction_model,
        )
        value = getattr(result.metrics, objective_metric)
        if value is None:
            # Optuna's objective must return a real float -- a trial whose
            # metric was undefined (e.g. zero trades, zero-variance
            # returns) is scored as the worst possible outcome so the
            # sampler learns to avoid that region, rather than silently
            # reading "undefined" as "exactly 0".
            return float("-inf")
        return value

    study.optimize(objective, n_trials=n_trials, catch=())

    try:
        importance = dict(
            optuna.importance.get_param_importances(
                study, evaluator=FanovaImportanceEvaluator(seed=seed)
            )
        )
    except (ValueError, RuntimeError):
        # fANOVA needs a handful of completed trials with actual parameter
        # variation to fit; too few (or all-identical) trials raise rather
        # than this function inventing an importance ranking.
        importance = {}

    best_value = study.best_value
    if best_value == float("-inf"):
        best_value = None

    return SweepResult(
        n_trials=len(study.trials),
        best_params=study.best_params,
        best_value=best_value,
        param_importance=importance,
    )
