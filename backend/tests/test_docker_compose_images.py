"""The images the stack runs are named by version, not by `latest`.

`docker compose pull` or a rebuild on a new machine would otherwise move Prometheus, Grafana and
Temporal to whatever their publishers released last, and a stack that worked yesterday could differ
today with nothing in the repository saying why. The images this project builds itself are the only
`latest` allowed (`tradingos-2.0-*:latest` is the tag compose gives its own builds).
"""

import re
from pathlib import Path

import pytest
import yaml

COMPOSE_PATH = Path(__file__).resolve().parent.parent.parent / "docker-compose.yml"
OWN_BUILDS_PREFIX = "tradingos-2.0-"


def _images() -> dict[str, str]:
    services = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))["services"]
    return {name: s["image"] for name, s in services.items() if "image" in s}


def test_no_third_party_image_is_latest_or_untagged():
    floating = {
        name: image
        for name, image in _images().items()
        if not image.startswith(OWN_BUILDS_PREFIX)
        and (
            "@" not in image and (":" not in image.rsplit("/", 1)[-1] or image.endswith(":latest"))
        )
    }

    assert floating == {}


@pytest.mark.parametrize(
    ("service", "pinned"),
    [
        ("prometheus", r"prom/prometheus:v\d+\.\d+\.\d+"),
        ("grafana", r"grafana/grafana:\d+\.\d+\.\d+"),
        ("temporal", r"temporalio/temporal:\d+\.\d+\.\d+"),
    ],
)
def test_the_images_that_were_latest_are_named_by_version(service, pinned):
    # Which version is Dependabot's to move (the stack-images group); that it is a full version,
    # and not a floating tag such as `latest` or `v3`, is what is checked here.
    assert re.fullmatch(pinned, _images()[service])


def test_the_python_dependencies_are_pinned_where_every_build_reads_them():
    root = COMPOSE_PATH.parent
    lock = (root / "backend" / "requirements.txt").read_text(encoding="utf-8")
    pins = [line for line in lock.splitlines() if line and not line.startswith("#")]

    assert pins and all("==" in line for line in pins)
    for build_file in ("backend/Dockerfile", "Dockerfile.allinone", ".github/workflows/ci.yml"):
        text = (root / build_file).read_text(encoding="utf-8")
        commands = " ".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert "-c requirements.txt" in commands, build_file
        assert "pip install ." not in commands.replace("-c requirements.txt .", ""), build_file


@pytest.mark.parametrize("dockerfile", ["backend/Dockerfile", "Dockerfile.allinone"])
def test_the_apt_layer_is_rebuilt_on_every_ci_run(dockerfile):
    """A cached apt layer keeps packages from before Debian's security fixes (nginx deb13u10 on
    2026-11-20, four critical CVEs fixed in deb13u11): `APT_REFRESH` comes before the first apt
    line, which uses it, so a new value rebuilds that layer."""
    lines = (COMPOSE_PATH.parent / dockerfile).read_text(encoding="utf-8").splitlines()
    arg = lines.index("ARG APT_REFRESH=")
    first_apt = next(i for i, line in enumerate(lines) if "apt-get update" in line)
    assert arg < first_apt
    assert "${APT_REFRESH" in " ".join(lines[first_apt - 1 : first_apt + 1])


def test_ci_gives_both_image_builds_a_new_apt_refresh_each_run():
    workflow = yaml.safe_load((COMPOSE_PATH.parent / ".github/workflows/ci.yml").read_text("utf-8"))
    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("docker/build-push-action")
    ]
    assert len(steps) == 2  # the build Trivy scans, and the one that publishes it
    for step in steps:
        assert step["with"]["build-args"] == (
            "APT_REFRESH=${{ github.run_id }}-${{ github.run_attempt }}"
        )
