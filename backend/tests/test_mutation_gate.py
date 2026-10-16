"""The mutation gate (scripts/mutation_gate.py) must be able to fail.

Running mutmut in CI is only worth its minutes if a surviving mutant turns the
build red. These pin the parts that decide that -- reading mutmut's junit output,
and the verdict against the recorded baseline -- without running mutmut itself
(that is the CI job's work), plus the repository's own module list.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "mutation_gate", _BACKEND / "scripts" / "mutation_gate.py"
)
assert _spec and _spec.loader
mg = importlib.util.module_from_spec(_spec)
sys.modules["mutation_gate"] = mg  # dataclasses resolve their annotations through it
_spec.loader.exec_module(mg)

_SPEC = mg.ModuleSpec("src/engine/risk/kill_switch.py", ["tests/test_engine_kill_switch.py"], 4)


def _junit(*, killed: int, survived: int, kind: str = "failure") -> str:
    cases = "".join(f'<testcase name="Mutant #{i}" file="m.py" line="1"/>' for i in range(killed))
    bad = (
        '<testcase name="Mutant #{n}" file="m.py" line="2">'
        '<{k} message="bad_survived">d</{k}></testcase>'
    )
    cases += "".join(bad.format(n=killed + i, k=kind) for i in range(survived))
    suite = f'<testsuite name="mutmut">{cases}</testsuite>'
    return f'<?xml version="1.0" ?><testsuites>{suite}</testsuites>'


def test_junit_counts_every_mutant_and_every_one_the_tests_did_not_kill():
    outcome = mg.parse_junit(_junit(killed=19, survived=4))

    assert (outcome.total, outcome.survived) == (23, 4)


def test_timed_out_and_suspicious_mutants_count_as_not_killed():
    assert mg.parse_junit(_junit(killed=5, survived=2, kind="error")).survived == 2


def test_output_with_no_testsuite_is_an_error_not_a_pass():
    with pytest.raises(ValueError):
        mg.parse_junit("<testsuites/>")


def test_exactly_the_baseline_passes():
    ok, message = mg.verdict(_SPEC, mg.Outcome(total=23, survived=4))

    assert ok and "19/23" in message and "baseline 4" in message


def test_a_new_survivor_fails_and_says_why():
    ok, message = mg.verdict(_SPEC, mg.Outcome(total=23, survived=5))

    assert not ok and "NEW survivors" in message


def test_fewer_survivors_than_the_baseline_passes_and_asks_for_the_ratchet():
    ok, message = mg.verdict(_SPEC, mg.Outcome(total=23, survived=2))

    assert ok and "lower max_survivors to 2" in message


def test_no_mutants_at_all_fails_rather_than_passing_vacuously():
    ok, message = mg.verdict(_SPEC, mg.Outcome(total=0, survived=0))

    assert not ok and "no mutants" in message


def test_the_repositorys_own_module_list_is_well_formed():
    modules = mg.load_modules(_BACKEND / "pyproject.toml")

    assert modules, "no [[tool.tradingos.mutation.module]] entries"
    for m in modules:
        assert (_BACKEND / m.path).is_file(), f"{m.path} does not exist"
        assert m.tests and all(
            (_BACKEND / t).is_file() for t in m.tests
        ), f"missing tests for {m.path}"
        assert m.max_survivors >= 0


def test_the_ci_matrix_is_built_from_the_planner_which_reads_the_pyproject_list():
    """There is no second module list to keep in step: the workflow's matrix comes from the
    change plan, and the plan lists modules from pyproject.toml. Fail if someone hard-codes one."""
    text = (_BACKEND.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "module: ${{ fromJSON(needs.changes.outputs.mutation_modules) }}" in text
    assert "- src/" not in text, "a hard-coded module list would drift from pyproject.toml"
