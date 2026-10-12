"""Mutation-testing gate for the safety layer (docs/phase20-old-vs-new-comparison.md item 28).

Line coverage says a test *ran* a line, not that it would notice the line being
wrong. Mutation testing rewrites the code one small way at a time (`<` to `<=`,
`and` to `or`, `* 100` to `* 101`) and runs the tests; a mutant nobody notices is
a real hole. Phase 14 did this by hand for the Kill Switch, the Compliance
Checker and the Correlation Constraint and found real inversions each time. This
runs the same thing in CI so the next regression of that kind cannot land quietly.

For one module named in `[[tool.tradingos.mutation.module]]` in pyproject.toml it
runs mutmut against only that module's own tests, then fails if more mutants
survived than `max_survivors`. The baseline is a ratchet: the survivors recorded
there are the ones reviewed and left alone on purpose (dataclass flags, message
text). A new survivor fails the build and prints its diff; lower `max_survivors`
when real survivors are killed, never raise it without a reason beside it.

    python scripts/mutation_gate.py src/engine/risk/kill_switch.py
    python scripts/mutation_gate.py --list          # the module paths, one per line (CI matrix)

Pinned to mutmut 2.x (see pyproject.toml: 3.x asserts against a package named `src`).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModuleSpec:
    path: str
    tests: list[str]
    max_survivors: int


@dataclass(frozen=True)
class Outcome:
    total: int
    survived: int


def load_modules(pyproject: Path) -> list[ModuleSpec]:
    table = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["tradingos"]["mutation"]
    return [
        ModuleSpec(m["path"], list(m["tests"]), int(m["max_survivors"])) for m in table["module"]
    ]


def parse_junit(xml_text: str) -> Outcome:
    """mutmut's junit output: one testcase per mutant, a <failure>/<error> child on each
    mutant that survived, timed out or was suspicious -- i.e. each one the tests did not kill."""
    suite = ET.fromstring(xml_text).find("testsuite")
    if suite is None:
        raise ValueError("no <testsuite> in mutmut's junit output")
    cases = suite.findall("testcase")
    bad = [c for c in cases if c.find("failure") is not None or c.find("error") is not None]
    return Outcome(total=len(cases), survived=len(bad))


def verdict(spec: ModuleSpec, outcome: Outcome) -> tuple[bool, str]:
    killed = outcome.total - outcome.survived
    rate = 100.0 * killed / outcome.total if outcome.total else 0.0
    head = f"{spec.path}: {killed}/{outcome.total} mutants killed ({rate:.1f}%), "
    head += f"{outcome.survived} survived (baseline {spec.max_survivors})"
    if outcome.total == 0:
        return False, head + " -- no mutants were generated; the module or its config is wrong"
    if outcome.survived > spec.max_survivors:
        return False, head + " -- NEW survivors: the tests no longer notice a change they used to"
    if outcome.survived < spec.max_survivors:
        return (
            True,
            head + f" -- better than the baseline; lower max_survivors to {outcome.survived}",
        )
    return True, head


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("module", nargs="?", help="module path as written in pyproject.toml")
    parser.add_argument("--config", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--list", action="store_true", help="print the configured module paths")
    args = parser.parse_args(argv)

    modules = load_modules(args.config)
    if args.list:
        print("\n".join(m.path for m in modules))
        return 0
    spec = next((m for m in modules if m.path == args.module), None)
    if spec is None:
        print(f"unknown module {args.module!r}; configured: {[m.path for m in modules]}")
        return 2

    runner = f"{sys.executable} -m pytest -x -q -p no:cacheprovider {' '.join(spec.tests)}"
    run = _run(
        ["mutmut", "run", "--no-progress", "--paths-to-mutate", spec.path, "--runner", runner]
    )
    junit = _run(["mutmut", "junitxml"])
    try:
        outcome = parse_junit(junit.stdout)
    except (ET.ParseError, ValueError) as exc:
        tail = run.stdout[-2000:]
        print(f"could not read mutmut's results: {exc}\n--- mutmut run output ---\n{tail}")
        return 2

    ok, message = verdict(spec, outcome)
    print(message)
    if outcome.survived:
        for case in ET.fromstring(junit.stdout).iter("testcase"):
            problem = case.find("failure")
            if problem is None:
                problem = case.find("error")
            if problem is not None:
                print(
                    f"\n--- {case.get('name')} (line {case.get('line')}) ---\n{problem.text or ''}"
                )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
