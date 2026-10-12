"""Per-module coverage gate (docs/phase20-old-vs-new-comparison.md item 28).

The suite already fails below a global 70%, but a global number hides exactly
what matters in a trading system: the safety layer (Kill Switch, Compliance
Checker, Naked-Options Scanner, Correlation Constraint, Go-Live Gate) can sit
at 40% while the average stays healthy. This reads the `coverage.json` that
`pytest --cov-report=json` writes and enforces two kinds of floor, both read
from `[tool.tradingos.coverage]` in pyproject.toml so they are reviewed like
any other change:

- every file in `critical_files` must reach `critical_file_floor` on its own;
- every package in `package_floors` must reach its floor, measured over all of
  its files (covered statements / statements), not as an average of averages.

A floor that names a file or package with no measured statements fails too:
that is a rename or a deletion the config has not caught up with, and a gate
that silently checks nothing is worse than none.

Floors are a ratchet. They sit a little below what is measured today, so they
fail on a real regression and not on noise; raise them when coverage rises
(`--suggest` prints the numbers), never lower one to make a build pass without
saying why next to it.

    python scripts/check_coverage.py [--coverage coverage.json] [--suggest]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Config:
    critical_files: list[str]
    critical_file_floor: float
    package_floors: dict[str, float]


@dataclass(frozen=True)
class Failure:
    target: str
    message: str


def load_config(path: Path) -> Config:
    table = tomllib.loads(path.read_text(encoding="utf-8"))["tool"]["tradingos"]["coverage"]
    return Config(
        critical_files=list(table["critical_files"]),
        critical_file_floor=float(table["critical_file_floor"]),
        package_floors={k: float(v) for k, v in table["package_floors"].items()},
    )


def _normalised(files: dict[str, Any]) -> dict[str, tuple[int, int]]:
    """path (forward slashes) -> (covered statements, statements)."""
    out: dict[str, tuple[int, int]] = {}
    for name, data in files.items():
        summary = data["summary"]
        out[name.replace("\\", "/")] = (summary["covered_lines"], summary["num_statements"])
    return out


def package_coverage(files: dict[str, tuple[int, int]], prefix: str) -> tuple[float, int] | None:
    """(percent, statements) over every file under `prefix`, None if there are no statements."""
    scoped = [v for k, v in files.items() if k.startswith(prefix.rstrip("/") + "/")]
    statements = sum(n for _, n in scoped)
    if statements == 0:
        return None
    return 100.0 * sum(c for c, _ in scoped) / statements, statements


def evaluate(coverage: dict[str, Any], cfg: Config) -> list[Failure]:
    files = _normalised(coverage["files"])
    failures: list[Failure] = []

    for name in cfg.critical_files:
        measured = files.get(name)
        if measured is None or measured[1] == 0:
            failures.append(
                Failure(name, "no measured statements -- renamed, deleted or never imported")
            )
            continue
        percent = 100.0 * measured[0] / measured[1]
        if percent < cfg.critical_file_floor:
            failures.append(
                Failure(
                    name,
                    f"{percent:.1f}% is below the {cfg.critical_file_floor:g}% critical-file floor",
                )
            )

    for prefix, floor in cfg.package_floors.items():
        result = package_coverage(files, prefix)
        if result is None:
            failures.append(
                Failure(prefix, "no measured statements -- renamed, deleted or never imported")
            )
        elif result[0] < floor:
            failures.append(Failure(prefix, f"{result[0]:.1f}% is below its {floor:g}% floor"))
    return failures


def suggest(coverage: dict[str, Any], cfg: Config, margin: float = 2.0) -> list[str]:
    files = _normalised(coverage["files"])
    lines = ["critical files (measured):"]
    for name in cfg.critical_files:
        c, n = files.get(name, (0, 0))
        lines.append(f"  {name}: {100.0 * c / n:.1f}%" if n else f"  {name}: not measured")
    lines.append(f"package floors (measured -> measured minus {margin:g}, rounded down):")
    for prefix in cfg.package_floors:
        result = package_coverage(files, prefix)
        if result:
            lines.append(f"  {prefix}: {result[0]:.1f}% -> {math.floor(result[0] - margin)}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--coverage", type=Path, default=Path("coverage.json"))
    parser.add_argument("--config", type=Path, default=Path("pyproject.toml"))
    parser.add_argument(
        "--suggest", action="store_true", help="print measured values and suggested floors"
    )
    args = parser.parse_args(argv)

    coverage = json.loads(args.coverage.read_text(encoding="utf-8"))
    cfg = load_config(args.config)
    if args.suggest:
        print("\n".join(suggest(coverage, cfg)))
        return 0

    failures = evaluate(coverage, cfg)
    for f in failures:
        print(f"FAIL {f.target}: {f.message}")
    checked = len(cfg.critical_files) + len(cfg.package_floors)
    if failures:
        print(f"\n{len(failures)} of {checked} coverage floors not met.")
        return 1
    print(f"All {checked} per-module coverage floors met.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
