"""Sandbox tests (Build Spec §9): the sandbox genuinely blocks network and
filesystem-outside-scratch (adversarial escape attempts, not just the
AST ban-list), enforces a wall-clock timeout, and the warm pool measurably
reduces per-call latency versus a fresh process every time.
"""

import subprocess
import sys
import time

from src.engine.sandbox.process_runtime import (
    UNSHARE_NET_ARGS,
    RestrictedProcessSandboxRuntime,
)
from src.engine.sandbox.types import SandboxLimits
from src.engine.sandbox.warm_pool import SandboxWarmPool

FAST_LIMITS = SandboxLimits(cpu_seconds=5, memory_bytes=256 * 1024 * 1024, timeout_seconds=5)


def test_valid_code_executes_and_returns_a_real_result(tmp_path):
    runtime = RestrictedProcessSandboxRuntime()
    code = """
def run_backtest(data, config):
    return {"trades": len(data), "sharpe": 1.5}
"""
    result = runtime.execute(
        code,
        params={"data": [1, 2, 3], "config": {}},
        scratch_dir=tmp_path / "scratch",
        data_dir=tmp_path / "data",
        limits=FAST_LIMITS,
    )
    assert result.success is True
    assert result.result == {"trades": 3, "sharpe": 1.5}


def test_network_namespace_blocks_outbound_connections_at_the_os_level():
    """Independent of any Python-level guard (restricted_exec.py) -- this
    proves the underlying `unshare --net` primitive itself genuinely
    isolates the network, not merely that our own import guard happens to
    stop `import socket`. Runs completely unrestricted code through the
    exact isolation wrapper process_runtime.py's worker uses.
    """
    script = (
        "import socket\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "s.settimeout(2)\n"
        "try:\n"
        "    s.connect(('8.8.8.8', 53))\n"
        "    print('CONNECTED')\n"
        "except OSError as e:\n"
        "    print(f'BLOCKED: {e}')\n"
    )
    result = subprocess.run(
        [*UNSHARE_NET_ARGS, sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "BLOCKED" in result.stdout
    assert "CONNECTED" not in result.stdout


def test_sandbox_execute_blocks_network_import_via_guard(tmp_path):
    runtime = RestrictedProcessSandboxRuntime()
    code = """
def run_backtest(data, config):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    s.connect(("8.8.8.8", 53))
    return {"escaped": True}
"""
    result = runtime.execute(
        code,
        params={},
        scratch_dir=tmp_path / "scratch",
        data_dir=tmp_path / "data",
        limits=FAST_LIMITS,
    )
    assert result.success is False
    assert "not allowed" in result.error
    assert "socket" in result.error


def test_sandbox_blocks_reading_a_file_outside_scratch_and_data(tmp_path):
    runtime = RestrictedProcessSandboxRuntime()
    code = """
def run_backtest(data, config):
    with open("/etc/passwd", "r") as f:
        return {"leaked": f.read()[:20]}
"""
    result = runtime.execute(
        code,
        params={},
        scratch_dir=tmp_path / "scratch",
        data_dir=tmp_path / "data",
        limits=FAST_LIMITS,
    )
    assert result.success is False
    assert "outside the allowed" in result.error


def test_sandbox_blocks_path_traversal_escape_from_scratch(tmp_path):
    runtime = RestrictedProcessSandboxRuntime()
    code = """
def run_backtest(data, config):
    with open("../../../../../../etc/passwd", "r") as f:
        return {"leaked": f.read()[:20]}
"""
    result = runtime.execute(
        code,
        params={},
        scratch_dir=tmp_path / "scratch",
        data_dir=tmp_path / "data",
        limits=FAST_LIMITS,
    )
    assert result.success is False
    assert "outside the allowed" in result.error


def test_sandbox_blocks_writing_into_the_read_only_data_mount(tmp_path):
    runtime = RestrictedProcessSandboxRuntime()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    code = """
def run_backtest(data, config):
    with open(config["data_path"] + "/evil.txt", "w") as f:
        f.write("pwned")
    return {"wrote": True}
"""
    result = runtime.execute(
        code,
        params={"config": {"data_path": str(data_dir)}},
        scratch_dir=tmp_path / "scratch",
        data_dir=data_dir,
        limits=FAST_LIMITS,
    )
    assert result.success is False
    assert "outside the allowed" in result.error


def test_sandbox_allows_writing_and_reading_back_within_scratch(tmp_path):
    runtime = RestrictedProcessSandboxRuntime()
    code = """
def run_backtest(data, config):
    with open("scratch_output.txt", "w") as f:
        f.write("hello")
    with open("scratch_output.txt", "r") as f:
        return {"content": f.read()}
"""
    result = runtime.execute(
        code,
        params={},
        scratch_dir=tmp_path / "scratch",
        data_dir=tmp_path / "data",
        limits=FAST_LIMITS,
    )
    assert result.success is True
    assert result.result == {"content": "hello"}


def test_sandbox_allows_reading_within_the_data_mount(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "prices.csv").write_text("100,200,300")

    runtime = RestrictedProcessSandboxRuntime()
    code = """
def run_backtest(data, config):
    with open(config["data_path"] + "/prices.csv", "r") as f:
        return {"content": f.read()}
"""
    result = runtime.execute(
        code,
        params={"config": {"data_path": str(data_dir)}},
        scratch_dir=tmp_path / "scratch",
        data_dir=data_dir,
        limits=FAST_LIMITS,
    )
    assert result.success is True
    assert result.result == {"content": "100,200,300"}


def test_sandbox_enforces_wall_clock_timeout(tmp_path):
    runtime = RestrictedProcessSandboxRuntime()
    code = """
def run_backtest(data, config):
    while True:
        pass
"""
    result = runtime.execute(
        code,
        params={},
        scratch_dir=tmp_path / "scratch",
        data_dir=tmp_path / "data",
        limits=SandboxLimits(cpu_seconds=10, memory_bytes=256 * 1024 * 1024, timeout_seconds=1),
    )
    assert result.success is False
    assert "timeout" in result.error.lower()
    assert result.duration_seconds < 5  # didn't actually run away


def test_warm_pool_reduces_latency_on_repeated_calls(tmp_path):
    code = """
def run_backtest(data, config):
    return {"ok": True}
"""
    scratch_dir = tmp_path / "scratch"
    data_dir = tmp_path / "data"
    n_calls = 5

    cold = RestrictedProcessSandboxRuntime()
    start = time.monotonic()
    for _ in range(n_calls):
        cold.execute(
            code, params={}, scratch_dir=scratch_dir, data_dir=data_dir, limits=FAST_LIMITS
        )
    cold_elapsed = time.monotonic() - start

    pool = SandboxWarmPool(size=2)
    try:
        start = time.monotonic()
        for _ in range(n_calls):
            pool.execute(
                code, params={}, scratch_dir=scratch_dir, data_dir=data_dir, limits=FAST_LIMITS
            )
        warm_elapsed = time.monotonic() - start
    finally:
        pool.shutdown()

    # A rough assertion, not a precise benchmark: the warm pool amortizes
    # the unshare+interpreter-boot cost across calls, the cold path pays
    # it every time -- expect a clear, not marginal, difference.
    assert warm_elapsed < cold_elapsed * 0.7
