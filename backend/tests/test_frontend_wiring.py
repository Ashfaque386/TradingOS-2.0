"""The console's API client and the backend's routes must not drift apart.

Two directions, both enforced:

* every call in `lib/api.ts` matches a route that exists, by
  method and path -- a renamed or removed endpoint fails the build, not the operator's screen;
* every backend HTTP route is either called by the client or listed in `NOT_CALLED_BY_THE_CONSOLE`
  below with a reason. A new endpoint therefore cannot land without a client function, and a
  client function cannot land without an endpoint. The allowlist is checked both ways: an entry
  for a route that the client now calls (or that no longer exists) is stale and fails too.

The client is read as text. Each `/api/v1/...` string in it is one call: `${...}` becomes a path
parameter, a query string is dropped, and the method comes from `apiGet/apiPost/...` or the
`method:` of a raw `request()`/`fetch()`; no method means GET.
"""

import re
from pathlib import Path

from src.main import app

REPO = Path(__file__).resolve().parents[2]
API = "/api/v1"

CLIENT_FILES = [REPO / "lib" / "api.ts"]

_HELPER_METHODS = {
    "apiGet": "GET",
    "apiGetPage": "GET",
    "apiPost": "POST",
    "apiPut": "PUT",
    "apiPatch": "PATCH",
    "apiDelete": "DELETE",
}

# (method, path) -> why the console does not call it. Group comments say which kind of reason.
NOT_CALLED_BY_THE_CONSOLE: dict[tuple[str, str], str] = {
    # ---- by design: called by something other than the console ----
    ("GET", "/api/v1/admin/ping"): "RBAC probe for the policy tests; no screen needs it",
    ("POST", "/api/v1/webhooks/telegram"): "inbound webhook, called by Telegram",
    ("POST", "/api/v1/webhooks/discord"): "inbound webhook, called by Discord",
    ("POST", "/api/v1/webhooks/slack"): "inbound webhook, called by Slack",
    ("POST", "/api/v1/webhooks/alertmanager"): "inbound webhook, called by Alertmanager",
    # ---- by design: a scheduled job's manual trigger (Mission Control's Run now runs it) ----
    ("POST", "/api/v1/market-data/backup"): "scheduled data-lake backup",
    ("POST", "/api/v1/market-data/bhavcopy-fallback"): "scheduled bhavcopy fallback",
    ("POST", "/api/v1/market-data/catalog-refresh"): "scheduled catalog refresh",
    ("POST", "/api/v1/market-data/ingest/corporate-actions"): "scheduled ingestion",
    ("POST", "/api/v1/market-data/ingest/daily"): "scheduled ingestion",
    ("POST", "/api/v1/market-data/ingest/instrument-master"): "scheduled ingestion",
    ("POST", "/api/v1/market-data/ingest/intraday"): "scheduled ingestion",
    (
        "POST",
        "/api/v1/paper-trading/daily-signal-run",
    ): "scheduled daily signal run; the e2e specs call it directly",
    (
        "POST",
        "/api/v1/live-trading/daily-signal-run",
    ): "scheduled daily signal run; the e2e specs call it directly",
    (
        "POST",
        "/api/v1/paper-trading/subscriptions/{}/tick",
    ): "drives one tick for tests and drills; the scheduler does this in service",
    # ---- by design: a single-item read that the list endpoint already covers ----
    ("GET", "/api/v1/chat/sessions/{}"): "the list and the messages endpoint cover it",
    ("GET", "/api/v1/orchestration/runs/{}"): "the runs list carries the same row",
    ("GET", "/api/v1/backtests/{}"): "the backtests list carries the same row",
    (
        "GET",
        "/api/v1/portfolio/recommendations/{}",
    ): "the recommendations list carries the same row",
    ("GET", "/api/v1/live-trading/intents/{}"): "the intents list carries the same row",
    ("GET", "/api/v1/live-trading/subscriptions/{}"): "the subscriptions list carries the same row",
    (
        "GET",
        "/api/v1/paper-trading/subscriptions/{}",
    ): "the subscriptions list carries the same row",
    ("GET", "/api/v1/paper-trading/subscriptions/{}/position"): "the positions endpoint covers it",
    (
        "GET",
        "/api/v1/live-trading/strategies/{}/position",
    ): "one position only; `GET /orders/positions` lists every symbol, live and paper",
    # ---- no screen yet: the phase of the gap analysis that adds it ----
    (
        "POST",
        "/api/v1/backtests",
    ): "the caller posts the bars; the console runs `/backtests/from-lake` on the lake's own",
}


def _read_path_literal(text: str, start: int, quote: str) -> tuple[str, int]:
    """The string from `start` to its closing `quote`, with each `${...}` replaced by `{}`."""
    out: list[str] = []
    i = start
    while i < len(text):
        ch = text[i]
        if ch == quote:
            return "".join(out), i
        if ch == "$" and text[i + 1 : i + 2] == "{":
            depth, i = 1, i + 2
            while depth and i < len(text):
                depth += {"{": 1, "}": -1}.get(text[i], 0)
                i += 1
            out.append("{}")
            continue
        out.append(ch)
        i += 1
    return "".join(out), i


def _normalise(raw: str) -> str:
    path = raw.split("?", 1)[0]
    segments = []
    for segment in path.split("/"):
        if segment.startswith("{}"):
            segments.append("{}")
        else:
            # `/orders{}` is a path followed by an optional query string built elsewhere
            segments.append(segment.replace("{}", ""))
    return "/".join(segments)


def _method_for(text: str, literal_start: int, literal_end: int) -> str:
    statement_start = max(
        text.rfind("\nexport ", 0, literal_start), text.rfind("\nasync function ", 0, literal_start)
    )
    before = text[statement_start + 1 : literal_start]
    helper = list(re.finditer(r"\b(apiGetPage|apiGet|apiPost|apiPut|apiPatch|apiDelete)\b", before))
    if helper:
        return _HELPER_METHODS[helper[-1].group(1)]
    following = re.search(r"\n(?:export |async function |function )", text[literal_end:])
    after = text[literal_end : literal_end + following.start()] if following else text[literal_end:]
    explicit = re.search(r"method:\s*'([A-Z]+)'", after)
    return explicit.group(1) if explicit else "GET"


def client_calls() -> set[tuple[str, str]]:
    calls: set[tuple[str, str]] = set()
    for file in CLIENT_FILES:
        text = file.read_text(encoding="utf-8")
        for match in re.finditer(r"""(['"`]|\$\{API_BASE_URL\})/api/v1""", text):
            start = match.start() + len(match.group(1))
            quote = "`" if match.group(1) == "${API_BASE_URL}" else match.group(1)
            raw, end = _read_path_literal(text, start, quote)
            calls.add((_method_for(text, start, end), _normalise(raw)))
    return calls


def _template(path: str) -> str:
    return re.sub(r"\{[^}/]+\}", "{}", path)


def backend_routes() -> set[tuple[str, str]]:
    """Every HTTP route the running app serves, from its public OpenAPI schema."""
    routes: set[tuple[str, str]] = set()
    for path, operations in app.openapi()["paths"].items():
        if path.startswith(API):
            for method in operations:
                if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    routes.add((method.upper(), _template(path)))
    return routes


def _same_route(call: tuple[str, str], route: tuple[str, str]) -> bool:
    """Same method and path, where a `{}` on either side stands for any one segment (the client
    writes `/{broker}/login-url`; the backend serves it as one literal route per broker)."""
    if call[0] != route[0]:
        return False
    a, b = call[1].split("/"), route[1].split("/")
    return len(a) == len(b) and all(x == y or "{}" in (x, y) for x, y in zip(a, b, strict=True))


def _calls_route(calls: set[tuple[str, str]], route: tuple[str, str]) -> bool:
    return any(_same_route(c, route) for c in calls)


def test_the_parser_reads_the_client_the_way_this_test_assumes() -> None:
    calls = client_calls()
    assert ("GET", "/api/v1/agents/{}/activity") in calls
    assert ("POST", "/api/v1/kill-switch/{}/reset") in calls
    assert ("POST", "/api/v1/audit/verify") in calls
    assert ("PATCH", "/api/v1/notification-channels/{}") in calls
    assert ("DELETE", "/api/v1/chat/sessions/{}") in calls
    assert len(calls) > 100, "the client parser found almost no calls"


def test_every_console_call_matches_a_backend_route() -> None:
    routes = backend_routes()
    missing = sorted(c for c in client_calls() if not any(_same_route(c, r) for r in routes))
    assert not missing, f"the console calls routes the backend does not serve: {missing}"


def test_every_backend_route_is_called_by_the_console_or_explained() -> None:
    called = client_calls()
    unexplained = sorted(
        r
        for r in backend_routes()
        if not _calls_route(called, r) and r not in NOT_CALLED_BY_THE_CONSOLE
    )
    assert not unexplained, (
        "backend routes with no console caller and no entry in NOT_CALLED_BY_THE_CONSOLE "
        f"(add a client function, or an entry with a reason): {unexplained}"
    )


def test_the_allowlist_has_no_stale_entries() -> None:
    called = client_calls()
    routes = backend_routes()
    gone = sorted(r for r in NOT_CALLED_BY_THE_CONSOLE if r not in routes)
    now_called = sorted(r for r in NOT_CALLED_BY_THE_CONSOLE if _calls_route(called, r))
    assert not gone, f"allowlisted routes that no longer exist: {gone}"
    assert (
        not now_called
    ), f"allowlisted routes the console now calls (remove the entry): {now_called}"


def test_lib_api_is_the_only_file_that_names_a_route() -> None:
    """The wiring test reads one file; a second caller would escape it."""
    quoted_route = re.compile(r"""['"`]/api/v1""")
    offenders = []
    for folder in ("app", "components", "lib"):
        for file in (REPO / folder).rglob("*"):
            if file.suffix not in {".ts", ".tsx"} or file == REPO / "lib" / "api.ts":
                continue
            if quoted_route.search(file.read_text(encoding="utf-8")):
                offenders.append(str(file.relative_to(REPO)))
    assert not offenders, f"route paths outside lib/api.ts: {offenders}"
