"""Background collector thread with its own event loop and a daily start time."""

import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone

from inventory.collector.runner import (
    RunInProgress,
    Scope,
    create_run,
    execute_run,
    last_good_run,
    mark_interrupted,
)
from inventory.db.connection import connect
from inventory.settings import Settings

log = logging.getLogger(__name__)

RETRY_SLEEP_S = 60  # wait after an error in the schedule loop before trying again


def next_start(now: datetime, at: str) -> datetime:
    """Next time of day `at` ("HH:MM") strictly after `now`."""
    hour, minute = (int(part) for part in at.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


class CollectorThread(threading.Thread):
    def __init__(self, settings: Settings):
        super().__init__(name="collector", daemon=True)
        self.settings = settings
        self._loop = asyncio.new_event_loop()
        self._stop_flag = threading.Event()
        self._stop_async = asyncio.Event()
        self._lock = threading.Lock()
        self._tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        # Always, even with the collector off: a run left 'running' would block all runs.
        conn = connect(self.settings.db_path)
        try:
            n = mark_interrupted(conn)
        finally:
            conn.close()
        if n:
            log.info("marked %d unfinished run(s) as interrupted", n)
        super().start()

    def request_run(self, trigger: str, triggered_by: str | None, scope: Scope) -> int:
        """Create a run and schedule it on the collector loop. Raises RunInProgress."""
        with self._lock:
            if self._stop_flag.is_set():
                raise RuntimeError("collector is stopped")
            conn = connect(self.settings.db_path)
            try:
                run_id = create_run(conn, trigger, triggered_by, scope)
            finally:
                conn.close()
            self._loop.call_soon_threadsafe(self._spawn, run_id)
        return run_id

    def stop(self, timeout: float | None = None) -> None:
        self._stop_flag.set()
        if not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._stop_async.set)
            except RuntimeError:  # loop closed in between
                pass
        if self.is_alive():
            self.join(timeout)

    def _spawn(self, run_id: int) -> None:
        task = self._loop.create_task(execute_run(self.settings, run_id, should_stop=self._stop_flag.is_set))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._log_failure)

    @staticmethod
    def _log_failure(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            log.error("run task failed", exc_info=task.exception())

    def run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _main(self) -> None:
        schedule = None
        if self.settings.collector_enabled:
            schedule = asyncio.create_task(self._schedule())
        await self._stop_async.wait()
        if schedule is not None:
            schedule.cancel()
            await asyncio.gather(schedule, return_exceptions=True)
        # Running runs see the stop flag and end as 'interrupted'.
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _start_scheduled(self, triggered_by: str | None) -> None:
        try:
            run_id = self.request_run("schedule", triggered_by, Scope())
            log.info("started scheduled run %s", run_id)
        except RunInProgress as e:
            log.info("run %s is active, skipping scheduled start", e.run_id)
        except RuntimeError:
            pass

    def _is_stale(self) -> bool:
        conn = connect(self.settings.db_path)
        try:
            row = last_good_run(conn)
        finally:
            conn.close()
        if row is None:
            return True
        when = datetime.fromisoformat(row["finished_at"] or row["started_at"])
        return datetime.now(timezone.utc) - when > timedelta(hours=self.settings.stale_after_hours)

    async def _schedule(self) -> None:
        try:
            if self._is_stale():
                self._start_scheduled("startup")
        except Exception:
            log.exception("startup staleness check failed")
        while not self._stop_flag.is_set():
            try:
                target = next_start(datetime.now(), self.settings.collect_daily_at)
                # Sleep in short steps so clock changes do not delay the start much.
                while (remaining := (target - datetime.now()).total_seconds()) > 0:
                    await asyncio.sleep(min(remaining, 60))
                self._start_scheduled(None)
            except Exception:
                log.exception("schedule loop error, retrying in %s s", RETRY_SLEEP_S)
                await asyncio.sleep(RETRY_SLEEP_S)
