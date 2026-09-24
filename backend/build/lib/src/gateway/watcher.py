"""Hot-reload file watcher (Build Spec §6.2). Watches the config file's
parent directory (watchdog watches directories, not individual files) and
re-runs the same apply_config_from_file pipeline the CLI uses on every
change to that filename. Debounced by content hash so duplicate
filesystem events for a single logical save (many editors write-then-
rename, or fire multiple modify events) don't cause redundant DB writes.
"""

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path

import structlog
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from src.core.db import AsyncSessionLocal
from src.gateway.apply import ApplyResult, apply_config_from_file
from src.models.agent_config_version import ConfigVersionStatus

logger = structlog.get_logger(__name__)


class _Handler(FileSystemEventHandler):
    def __init__(self, target_name: str, on_change: Callable[[], None]) -> None:
        self._target_name = target_name
        self._on_change = on_change

    def _maybe_trigger(self, path: str, is_directory: bool) -> None:
        if is_directory:
            return
        if Path(path).name != self._target_name:
            return
        self._on_change()

    def on_modified(self, event: FileSystemEvent) -> None:
        self._maybe_trigger(event.src_path, event.is_directory)

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_trigger(event.src_path, event.is_directory)

    def on_moved(self, event: FileSystemEvent) -> None:
        # Some editors save via write-to-temp-file then rename over the target.
        dest_path = getattr(event, "dest_path", "")
        self._maybe_trigger(dest_path, event.is_directory)


class ConfigWatcher:
    def __init__(self, config_path: Path, *, source: str = "hot-reload") -> None:
        self._config_path = config_path
        self._source = source
        self._observer: Observer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_content_hash: str | None = None
        self._on_applied: Callable[[ApplyResult], None] | None = None

    def start(self, *, on_applied: Callable[[ApplyResult], None] | None = None) -> None:
        """Must be called from within a running event loop — that loop is
        what the watchdog thread's callbacks schedule work onto.
        """
        self._loop = asyncio.get_running_loop()
        self._on_applied = on_applied
        handler = _Handler(self._config_path.name, self._schedule_apply)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._config_path.parent), recursive=False)
        self._observer.start()
        logger.info("agent_gateway.watcher_started", path=str(self._config_path))

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
            logger.info("agent_gateway.watcher_stopped")

    def _schedule_apply(self) -> None:
        if self._loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(self._apply(), self._loop)
        future.add_done_callback(self._log_task_exception)

    @staticmethod
    def _log_task_exception(future: "asyncio.Future") -> None:
        exc = future.exception()
        if exc is not None:
            logger.error("agent_gateway.hot_reload_apply_crashed", error=str(exc))

    async def _apply(self) -> None:
        try:
            text = self._config_path.read_text(encoding="utf-8")
        except OSError:
            # Transient — e.g. an editor mid-write. The next fs event retries.
            return

        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if content_hash == self._last_content_hash:
            return
        self._last_content_hash = content_hash

        async with AsyncSessionLocal() as session:
            result = await apply_config_from_file(session, self._config_path, source=self._source)

        if result.status == ConfigVersionStatus.ACTIVE:
            logger.info("agent_gateway.hot_reload_applied", version=result.version_id)
        else:
            logger.warning(
                "agent_gateway.hot_reload_rejected", version=result.version_id, errors=result.errors
            )

        if self._on_applied is not None:
            self._on_applied(result)
