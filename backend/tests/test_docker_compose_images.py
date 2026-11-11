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
