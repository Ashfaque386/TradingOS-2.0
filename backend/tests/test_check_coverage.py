"""The per-module coverage gate (scripts/check_coverage.py) must be able to fail.

A coverage gate that cannot go red -- because a floor names a file that no
longer exists, or because "below the floor" is compared the wrong way round --
checks nothing and looks like it checks everything. These pin the behaviour
that makes it a gate: below-floor fails, exactly-at-floor passes, a missing
file or package fails, and the repository's own configuration is well-formed.
"""

import importlib.util
import json
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_coverage", _BACKEND / "scripts" / "check_coverage.py"
)
assert _spec and _spec.loader
cc = importlib.util.module_from_spec(_spec)
sys.modules["check_coverage"] = cc  # dataclasses resolve their annotations through it
_spec.loader.exec_module(cc)


def _cov(**files: tuple[int, int]) -> dict:
    return {
        "files": {
            name.replace("__", "/") + ".py": {"summary": {"covered_lines": c, "num_statements": n}}
            for name, (c, n) in files.items()
        }
    }


_CFG = cc.Config(
    critical_files=["src/engine/risk/kill_switch.py"],
    critical_file_floor=90.0,
    package_floors={"src/engine": 80.0, "src/api": 60.0},
)


def test_everything_above_its_floor_passes():
    coverage = _cov(
        src__engine__risk__kill_switch=(95, 100),
        src__engine__other=(90, 100),
        src__api__routes=(70, 100),
    )
    assert cc.evaluate(coverage, _CFG) == []


def test_a_critical_file_below_its_floor_fails_even_when_its_package_is_fine():
    coverage = _cov(
        src__engine__risk__kill_switch=(80, 100),
        src__engine__other=(900, 900),
        src__api__routes=(70, 100),
    )

    failures = cc.evaluate(coverage, _CFG)

    assert [f.target for f in failures] == ["src/engine/risk/kill_switch.py"]
    assert "80.0%" in failures[0].message


def test_a_floor_is_inclusive_and_just_under_fails():
    at = _cov(
        src__engine__risk__kill_switch=(90, 100), src__engine__x=(80, 100), src__api__a=(60, 100)
    )
    under = _cov(
        src__engine__risk__kill_switch=(89, 100), src__engine__x=(80, 100), src__api__a=(60, 100)
    )

    assert cc.evaluate(at, _CFG) == []
    assert [f.target for f in cc.evaluate(under, _CFG)] == ["src/engine/risk/kill_switch.py"]


def test_a_package_is_measured_over_its_statements_not_as_an_average_of_files():
    # One big untested file drags the package down although the small file is perfect.
    coverage = _cov(
        src__engine__risk__kill_switch=(100, 100),
        src__engine__big=(0, 1000),
        src__api__a=(100, 100),
    )

    failures = cc.evaluate(coverage, _CFG)

    assert [f.target for f in failures] == ["src/engine"]


def test_a_missing_critical_file_or_package_fails_instead_of_passing_silently():
    coverage = _cov(src__api__a=(100, 100))  # the engine is gone entirely

    targets = {f.target for f in cc.evaluate(coverage, _CFG)}

    assert targets == {"src/engine/risk/kill_switch.py", "src/engine"}


def test_a_file_with_no_statements_counts_as_not_measured():
    coverage = _cov(
        src__engine__risk__kill_switch=(0, 0), src__engine__x=(10, 10), src__api__a=(10, 10)
    )

    assert "src/engine/risk/kill_switch.py" in {f.target for f in cc.evaluate(coverage, _CFG)}


def test_windows_style_paths_are_normalised():
    coverage = {
        "files": {
            "src\\engine\\risk\\kill_switch.py": {
                "summary": {"covered_lines": 100, "num_statements": 100}
            },
            "src\\engine\\x.py": {"summary": {"covered_lines": 100, "num_statements": 100}},
            "src\\api\\a.py": {"summary": {"covered_lines": 100, "num_statements": 100}},
        }
    }
    assert cc.evaluate(coverage, _CFG) == []


def test_a_prefix_does_not_match_a_sibling_with_the_same_start():
    files = {"src/engine_extra/x.py": (0, 100), "src/engine/y.py": (100, 100)}

    assert cc.package_coverage(files, "src/engine") == (100.0, 100)


def test_main_exits_nonzero_when_a_floor_is_missed(tmp_path, capsys):
    cov = tmp_path / "coverage.json"
    cov.write_text(
        json.dumps(
            _cov(
                src__engine__risk__kill_switch=(1, 100),
                src__engine__x=(1, 100),
                src__api__a=(1, 100),
            )
        )
    )
    cfg = tmp_path / "pyproject.toml"
    cfg.write_text(
        '[tool.tradingos.coverage]\ncritical_files = ["src/engine/risk/kill_switch.py"]\n'
        'critical_file_floor = 90\n[tool.tradingos.coverage.package_floors]\n"src/engine" = 80\n'
    )

    assert cc.main(["--coverage", str(cov), "--config", str(cfg)]) == 1
    assert "FAIL src/engine/risk/kill_switch.py" in capsys.readouterr().out


def test_the_repositorys_own_floors_are_well_formed():
    cfg = cc.load_config(_BACKEND / "pyproject.toml")

    assert cfg.critical_files and 0 < cfg.critical_file_floor <= 100
    for name in cfg.critical_files:
        assert (_BACKEND / name).is_file(), f"critical file {name} does not exist"
    for prefix, floor in cfg.package_floors.items():
        assert (_BACKEND / prefix).is_dir(), f"package {prefix} does not exist"
        assert 0 < floor <= 100
