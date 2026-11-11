"""Decides which CI jobs a change actually needs.

CI used to run everything on every push: the backend suite (10-15 min), Playwright, both Docker
image builds and ten mutation jobs (4-9 min each) -- for a one-line docs change as much as for a
change to the risk gate. This looks at the files a push or pull request touched and says which
jobs have something to say about them; the workflow runs only those.

The rule that makes this safe: **when in doubt, run everything.** Everything runs for a tag, a
manual dispatch, the weekly scheduled run (which also catches drift nothing in the repo caused:
a new dependency advisory, a base-image change), a change to this planner or to the workflow
itself, and whenever the diff cannot be computed (a force-push that rewrote the base, a shallow
checkout). Only a diff that was read successfully can skip a job.

    python scripts/ci_plan.py        # reads EVENT_NAME, REF_TYPE, BASE_SHA, PR_BASE, HEAD_SHA

Writes `backend`, `frontend`, `e2e`, `images` (true/false) and `mutation_modules` (a JSON list of
module paths, as written in pyproject.toml) to $GITHUB_OUTPUT, and a table of what runs and why to
$GITHUB_STEP_SUMMARY.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = "backend/"

# Files that change nothing CI can test.
DOC_PREFIXES = ("docs/",)
DOC_SUFFIXES = (".md",)
DOC_FILES = frozenset({"LICENSE", ".gitignore", ".github/dependabot.yml"})

# Changing CI itself, or this planner, re-runs everything: the old plan cannot vouch for the new.
CI_DEFINITION = frozenset({".github/workflows/ci.yml", "backend/scripts/ci_plan.py"})

# What the Python/Postgres side depends on. `deploy/` holds the seccomp profile the sandbox tests
# and the e2e backend run under; `config/` is the gateway config the backend loads.
BACKEND_PREFIXES = ("backend/", "config/", "deploy/")

# Files outside backend/ that the backend's own tests read. Changing one is a backend change even
# though no backend file moved: the console's API client (checked against the routes by
# tests/test_frontend_wiring.py), the RBAC export (tests/test_frontend_permissions.py), the
# all-in-one Dockerfile (tests/test_docker_compose_images.py), the paper-attention parity fixture
# (tests/test_paper_attention.py), and by prefix the compose files (the test_docker_compose_* files,
# test_container_settings_persistence.py, test_docker_sandbox_seccomp.py) and the Prometheus and
# Alertmanager configs (tests/test_alertmanager_wiring.py). A compose-only image bump once skipped
# the backend suite here, and the test that pins those images failed on the next backend change.
BACKEND_CONTRACT_FILES = frozenset(
    {
        "lib/api.ts",
        "lib/permissions.generated.json",
        "Dockerfile.allinone",
        "e2e/fixtures/paper-attention.json",
    }
)
BACKEND_CONTRACT_PREFIXES = ("docker-compose", "observability/")

# Not part of any Docker image (so a change here needs no image build).
NOT_IN_IMAGES_PREFIXES = ("backend/tests/", "e2e/", ".github/")

# A change to the gate re-measures every mutation module; so does a change to the parts of
# pyproject.toml that decide the baselines and how the tests run (below). A change to its
# *dependencies* (a bump, a new package) does not: it touches no gated module, and the weekly run
# measures them all against whatever the dependencies have become.
MUTATION_GATE = frozenset({"backend/scripts/mutation_gate.py"})
PYPROJECT = "backend/pyproject.toml"
MUTATION_TABLES = (("tool", "tradingos", "mutation"), ("tool", "mutmut"), ("tool", "pytest"))


@dataclass(frozen=True)
class MutationModule:
    path: str  # as written in pyproject.toml, relative to backend/
    tests: tuple[str, ...]


@dataclass
class Plan:
    backend: bool = False
    frontend: bool = False
    e2e: bool = False
    images: bool = False
    mutation_modules: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)

    def as_outputs(self) -> dict[str, str]:
        return {
            "backend": str(self.backend).lower(),
            "frontend": str(self.frontend).lower(),
            "e2e": str(self.e2e).lower(),
            "images": str(self.images).lower(),
            "mutation_modules": json.dumps(self.mutation_modules),
        }


def is_docs_only(path: str) -> bool:
    return path.startswith(DOC_PREFIXES) or path.endswith(DOC_SUFFIXES) or path in DOC_FILES


def everything(modules: list[MutationModule], why: str) -> Plan:
    return Plan(
        backend=True,
        frontend=True,
        e2e=True,
        images=True,
        mutation_modules=[m.path for m in modules],
        reasons={k: why for k in ("backend", "frontend", "e2e", "images", "mutation")},
    )


def mutation_config_moved(old: str | None, new: str | None) -> bool:
    """Whether pyproject.toml changed in a way that moves a mutation baseline or the test run: its
    mutation tables, the mutmut table or pytest's options. Unreadable on either side counts as
    moved, so the answer errs towards measuring."""
    if old is None or new is None:
        return True
    try:
        before, after = tomllib.loads(old), tomllib.loads(new)
    except tomllib.TOMLDecodeError:
        return True

    def pick(data: dict, path: tuple[str, ...]) -> object:
        for key in path:
            if not isinstance(data, dict) or key not in data:
                return None
            data = data[key]
        return data

    return any(pick(before, p) != pick(after, p) for p in MUTATION_TABLES)


def plan(
    changed: list[str], modules: list[MutationModule], *, mutation_config_changed: bool = True
) -> Plan:
    """The jobs needed for a successfully read diff (`changed`: repo-relative, forward slashes).
    `mutation_config_changed` says whether a change to pyproject.toml touched the parts that matter
    to the mutation jobs; unknown (the default) is yes."""
    relevant = [f for f in changed if not is_docs_only(f)]
    if not relevant:
        reason = "only documentation changed" if changed else "no files changed"
        return Plan(
            reasons={k: reason for k in ("backend", "frontend", "e2e", "images", "mutation")}
        )

    if CI_DEFINITION.intersection(relevant):
        return everything(modules, "the CI definition or planner changed")

    plan_ = Plan()

    backend_files = [
        f
        for f in relevant
        if f.startswith(BACKEND_PREFIXES)
        or f in BACKEND_CONTRACT_FILES
        or f.startswith(BACKEND_CONTRACT_PREFIXES)
    ]
    # Everything else that is not CI plumbing is the frontend side (app, components, lib, e2e,
    # package manifests, Dockerfiles, root scripts): conservative on purpose.
    frontend_files = [
        f for f in relevant if not f.startswith(BACKEND_PREFIXES) and not f.startswith(".github/")
    ]
    plan_.backend = bool(backend_files)
    plan_.frontend = bool(frontend_files)
    plan_.e2e = plan_.backend or plan_.frontend
    plan_.images = any(not f.startswith(NOT_IN_IMAGES_PREFIXES) for f in relevant)

    if MUTATION_GATE.intersection(relevant) or (PYPROJECT in relevant and mutation_config_changed):
        plan_.mutation_modules = [m.path for m in modules]
        mutation_why = "mutation baselines or the gate changed"
    else:
        touched = set(relevant)
        plan_.mutation_modules = [
            m.path
            for m in modules
            if f"{BACKEND}{m.path}" in touched or any(f"{BACKEND}{t}" in touched for t in m.tests)
        ]
        mutation_why = (
            "its module or tests changed" if plan_.mutation_modules else "no covered module changed"
        )

    plan_.reasons = {
        "backend": (
            "backend/, config/, deploy/ or a file the backend tests read changed"
            if plan_.backend
            else "nothing backend"
        ),
        "frontend": "frontend or root files changed" if plan_.frontend else "nothing frontend",
        "e2e": "backend or frontend changed" if plan_.e2e else "nothing to exercise",
        "images": "image contents changed" if plan_.images else "only tests/CI-only files changed",
        "mutation": mutation_why,
    }
    return plan_


def load_modules(pyproject: Path) -> list[MutationModule]:
    table = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["tradingos"]["mutation"]
    return [MutationModule(m["path"], tuple(m["tests"])) for m in table["module"]]


def changed_files(
    base_sha: str, head_sha: str, pr_base: str, *, cwd: Path = REPO_ROOT
) -> list[str] | None:
    """Files touched, or None if the diff cannot be read (the caller then runs everything)."""
    if pr_base:
        spec = [f"origin/{pr_base}...{head_sha}"]  # merge-base diff: what the PR itself changes
    elif base_sha and set(base_sha) != {"0"}:
        spec = [base_sha, head_sha]
    else:
        return None  # a brand-new branch/tag: there is no "before" to compare with
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", *spec],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:  # no git at all
        return None
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def pyproject_at(revision: str, *, cwd: Path = REPO_ROOT) -> str | None:
    """backend/pyproject.toml as it was at `revision`, or None if git cannot say."""
    try:
        result = subprocess.run(
            ["git", "show", f"{revision}:{PYPROJECT}"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def base_revision(
    base_sha: str, head_sha: str, pr_base: str, *, cwd: Path = REPO_ROOT
) -> str | None:
    """The commit the diff is measured from (a pull request's merge base, or a push's `before`)."""
    if pr_base:
        try:
            result = subprocess.run(
                ["git", "merge-base", f"origin/{pr_base}", head_sha],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return None
        return result.stdout.strip() or None if result.returncode == 0 else None
    if base_sha and set(base_sha) != {"0"}:
        return base_sha
    return None


def decide(env: dict[str, str], modules: list[MutationModule], *, cwd: Path = REPO_ROOT) -> Plan:
    event = env.get("EVENT_NAME", "")
    if event in ("workflow_dispatch", "schedule"):
        return everything(modules, f"a {event} run always runs everything")
    if env.get("REF_TYPE") == "tag":
        return everything(modules, "a tag publishes a release: run everything")
    files = changed_files(
        env.get("BASE_SHA", ""), env.get("HEAD_SHA", "HEAD"), env.get("PR_BASE", ""), cwd=cwd
    )
    if files is None:
        return everything(modules, "the diff could not be read: when in doubt, run everything")
    moved = True
    if PYPROJECT in files:
        revision = base_revision(
            env.get("BASE_SHA", ""), env.get("HEAD_SHA", "HEAD"), env.get("PR_BASE", ""), cwd=cwd
        )
        old = pyproject_at(revision, cwd=cwd) if revision else None
        new_path = cwd / PYPROJECT
        new = new_path.read_text(encoding="utf-8") if new_path.is_file() else None
        moved = mutation_config_moved(old, new)
    return plan(files, modules, mutation_config_changed=moved)


def summary(plan_: Plan) -> str:
    rows = [
        (
            "Backend (pytest, ruff, bandit, mypy, pip-audit)",
            plan_.backend,
            plan_.reasons["backend"],
        ),
        ("Frontend (tsc, lint, logic specs)", plan_.frontend, plan_.reasons["frontend"]),
        ("Frontend E2E (Playwright)", plan_.e2e, plan_.reasons["e2e"]),
        ("Docker images", plan_.images, plan_.reasons["images"]),
        (
            f"Mutation ({len(plan_.mutation_modules)} module(s))",
            bool(plan_.mutation_modules),
            plan_.reasons["mutation"],
        ),
    ]
    lines = ["### What CI runs for this change", "", "| Job | Runs | Why |", "|---|---|---|"]
    lines += [f"| {name} | {'yes' if runs else 'skipped'} | {why} |" for name, runs, why in rows]
    if plan_.mutation_modules:
        lines += ["", "Mutation modules: " + ", ".join(f"`{m}`" for m in plan_.mutation_modules)]
    return "\n".join(lines) + "\n"


def main() -> int:
    modules = load_modules(REPO_ROOT / "backend" / "pyproject.toml")
    decided = decide(dict(os.environ), modules)
    outputs = decided.as_outputs()
    text = summary(decided)
    print(text)
    print(json.dumps(outputs))
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.writelines(f"{key}={value}\n" for key, value in outputs.items())
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
