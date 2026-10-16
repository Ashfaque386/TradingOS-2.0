"""Which CI jobs a change needs (scripts/ci_plan.py).

The property that matters is the safe direction: a job may only be skipped when the diff was read
and nothing in it can affect that job. Anything else must run everything.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ci_plan", _BACKEND / "scripts" / "ci_plan.py")
cp = importlib.util.module_from_spec(_spec)
sys.modules["ci_plan"] = cp
_spec.loader.exec_module(cp)

MODULES = [
    cp.MutationModule("src/engine/risk/kill_switch.py", ("tests/test_engine_risk_kill_switch.py",)),
    cp.MutationModule("src/orchestration/risk_gate.py", ("tests/test_orchestration_risk_gate.py",)),
]
ALL = [m.path for m in MODULES]


def _jobs(plan: "cp.Plan") -> set[str]:
    out = plan.as_outputs()
    return {k for k in ("backend", "frontend", "e2e", "images") if out[k] == "true"}


@pytest.mark.parametrize(
    "files",
    [
        ["docs/CLAUDE.md"],
        ["README.md", "docs/phase1.md", "backend/README.md"],
        ["LICENSE", ".gitignore", ".github/dependabot.yml"],
    ],
)
def test_documentation_alone_runs_nothing(files):
    plan = cp.plan(files, MODULES)

    assert _jobs(plan) == set() and plan.mutation_modules == []


def test_an_empty_diff_runs_nothing():
    assert _jobs(cp.plan([], MODULES)) == set()


def test_a_backend_source_change_runs_the_backend_and_the_stack_but_not_tsc():
    plan = cp.plan(["backend/src/api/routes/chat.py"], MODULES)

    assert _jobs(plan) == {"backend", "e2e", "images"}
    assert plan.mutation_modules == []  # not a mutation-covered module


def test_a_frontend_change_skips_the_backend_and_the_mutation_jobs():
    plan = cp.plan(["components/mission-control/agent-pipeline-panel.tsx", "app/page.tsx"], MODULES)

    assert _jobs(plan) == {"frontend", "e2e", "images"}
    assert plan.mutation_modules == []


def test_a_test_only_change_does_not_build_images():
    plan = cp.plan(["backend/tests/test_chat.py"], MODULES)

    assert _jobs(plan) == {"backend", "e2e"}


def test_an_e2e_spec_change_runs_the_browser_suite_but_not_the_backend_or_images():
    plan = cp.plan(["e2e/chat.spec.ts"], MODULES)

    assert _jobs(plan) == {"frontend", "e2e"}


def test_a_gated_module_runs_only_its_own_mutation_job():
    plan = cp.plan(["backend/src/orchestration/risk_gate.py"], MODULES)

    assert plan.mutation_modules == ["src/orchestration/risk_gate.py"]


def test_changing_a_gated_modules_test_file_reruns_its_mutation_job():
    plan = cp.plan(["backend/tests/test_engine_risk_kill_switch.py"], MODULES)

    assert plan.mutation_modules == ["src/engine/risk/kill_switch.py"]


@pytest.mark.parametrize("path", ["backend/pyproject.toml", "backend/scripts/mutation_gate.py"])
def test_changing_the_baselines_or_the_gate_remeasures_every_module(path):
    assert cp.plan([path], MODULES).mutation_modules == ALL


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "backend/scripts/ci_plan.py"])
def test_changing_ci_itself_runs_everything(path):
    plan = cp.plan([path, "docs/CLAUDE.md"], MODULES)

    assert _jobs(plan) == {"backend", "frontend", "e2e", "images"}
    assert plan.mutation_modules == ALL


def test_docs_mixed_with_code_do_not_hide_the_code():
    plan = cp.plan(["docs/CLAUDE.md", "backend/src/main.py"], MODULES)

    assert "backend" in _jobs(plan)


def test_deploy_and_config_changes_count_as_backend_changes():
    assert "backend" in _jobs(cp.plan(["deploy/seccomp/backend.json"], MODULES))
    assert "backend" in _jobs(cp.plan(["config/tradingos.config.json"], MODULES))


def test_an_unknown_root_file_errs_towards_running_the_frontend_and_stack():
    plan = cp.plan(["package.json"], MODULES)

    assert {"frontend", "e2e", "images"} <= _jobs(plan)


def test_the_outputs_are_what_the_workflow_reads():
    out = cp.plan(["backend/src/orchestration/risk_gate.py"], MODULES).as_outputs()

    assert set(out) == {"backend", "frontend", "e2e", "images", "mutation_modules"}
    assert json.loads(out["mutation_modules"]) == ["src/orchestration/risk_gate.py"]
    assert cp.plan(["docs/x.md"], MODULES).as_outputs()["mutation_modules"] == "[]"


def test_the_summary_says_what_runs_and_what_is_skipped():
    text = cp.summary(cp.plan(["docs/x.md"], MODULES))

    assert "skipped" in text and "only documentation changed" in text


# ---- the event / diff handling: the "when in doubt, run everything" half ----------------------


@pytest.mark.parametrize("event", ["workflow_dispatch", "schedule"])
def test_a_manual_or_scheduled_run_runs_everything(event):
    plan = cp.decide({"EVENT_NAME": event}, MODULES)

    assert _jobs(plan) == {"backend", "frontend", "e2e", "images"}
    assert plan.mutation_modules == ALL


def test_a_tag_runs_everything():
    plan = cp.decide({"EVENT_NAME": "push", "REF_TYPE": "tag", "BASE_SHA": "0" * 40}, MODULES)

    assert plan.mutation_modules == ALL and _jobs(plan) == {"backend", "frontend", "e2e", "images"}


@pytest.mark.parametrize("base", ["", "0" * 40, "deadbeef" * 5])
def test_an_unreadable_diff_runs_everything_rather_than_nothing(tmp_path, base):
    if shutil.which(
        "git"
    ):  # an empty repo: no such commit (without git, the diff is unreadable too)
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    plan = cp.decide(
        {"EVENT_NAME": "push", "REF_TYPE": "branch", "BASE_SHA": base, "HEAD_SHA": "HEAD"},
        MODULES,
        cwd=tmp_path,
    )

    assert _jobs(plan) == {"backend", "frontend", "e2e", "images"}
    assert plan.mutation_modules == ALL


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_push_is_planned_from_the_files_between_before_and_after(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("1")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "one")
    before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    (tmp_path / "docs" / "a.md").write_text("2")
    _git(tmp_path, "commit", "-qam", "docs only")
    docs_after = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
    (tmp_path / "backend" / "src").mkdir(parents=True)
    (tmp_path / "backend" / "src" / "x.py").write_text("x = 1")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "code")

    def run(base_sha):
        return cp.decide(
            {
                "EVENT_NAME": "push",
                "BASE_SHA": base_sha,
                "HEAD_SHA": docs_after if base_sha == before else "HEAD",
            },
            MODULES,
            cwd=tmp_path,
        )

    assert _jobs(run(before)) == set()  # before..docs_after touched only docs
    assert "backend" in _jobs(run(docs_after))  # docs_after..HEAD added backend code


def test_the_repositorys_real_pyproject_loads_through_the_planner():
    modules = cp.load_modules(_BACKEND / "pyproject.toml")

    assert modules and all((_BACKEND / m.path).is_file() for m in modules)
    # Every gated module plans to its own job: the matrix cannot silently omit one.
    for m in modules:
        assert cp.plan([f"backend/{m.path}"], modules).mutation_modules == [m.path]
