from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone

import _path  # noqa: F401
from tele_secretary.scheduler.runner import REMINDER_POLL_INTERVAL_SECONDS, Scheduler


NOW = datetime(2026, 8, 16, 15, 0, 0, 123456, tzinfo=timezone.utc)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_runs_one_immediate_cycle_and_is_idempotent(self) -> None:
        cycles: list[datetime] = []
        sleeps: list[float] = []
        cycle_started = asyncio.Event()
        sleep_started = asyncio.Event()

        async def run_cycle(cycle_time: datetime) -> None:
            cycles.append(cycle_time)
            cycle_started.set()

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            sleep_started.set()
            await asyncio.Future()

        scheduler = Scheduler(run_cycle=run_cycle, clock=lambda: NOW, sleep=sleep)
        scheduler.start()
        scheduler.start()
        await cycle_started.wait()
        await sleep_started.wait()
        await scheduler.stop()
        await scheduler.stop()

        self.assertEqual(cycles, [NOW.replace(microsecond=0)])
        self.assertEqual(sleeps, [REMINDER_POLL_INTERVAL_SECONDS])

    async def test_loop_continues_after_an_ordinary_cycle_failure(self) -> None:
        cycles: list[datetime] = []
        sleeps: list[float] = []
        second_cycle_started = asyncio.Event()

        async def run_cycle(cycle_time: datetime) -> None:
            cycles.append(cycle_time)
            if len(cycles) == 1:
                raise RuntimeError("sensitive failure text")
            second_cycle_started.set()

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 1:
                return
            await asyncio.Future()

        scheduler = Scheduler(run_cycle=run_cycle, clock=lambda: NOW, sleep=sleep)
        with self.assertLogs("tele_secretary.scheduler.runner", level="ERROR") as logs:
            scheduler.start()
            await second_cycle_started.wait()
            await scheduler.stop()

        self.assertEqual(len(cycles), 2)
        self.assertEqual(sleeps, [REMINDER_POLL_INTERVAL_SECONDS, REMINDER_POLL_INTERVAL_SECONDS])
        self.assertNotIn("sensitive failure text", "\n".join(logs.output))

    async def test_blocked_cycle_cannot_overlap_with_another_cycle(self) -> None:
        active_cycles = 0
        started = asyncio.Event()
        release_cycle = asyncio.Event()
        sleep_started = asyncio.Event()
        cycle_count = 0

        async def run_cycle(_: datetime) -> None:
            nonlocal active_cycles, cycle_count
            active_cycles += 1
            cycle_count += 1
            self.assertEqual(active_cycles, 1)
            started.set()
            await release_cycle.wait()
            active_cycles -= 1

        async def sleep(_: float) -> None:
            sleep_started.set()
            await asyncio.Future()

        scheduler = Scheduler(run_cycle=run_cycle, clock=lambda: NOW, sleep=sleep)
        scheduler.start()
        await started.wait()
        await asyncio.sleep(0)
        self.assertEqual(cycle_count, 1)
        self.assertFalse(sleep_started.is_set())

        release_cycle.set()
        await sleep_started.wait()
        await scheduler.stop()

        self.assertEqual(cycle_count, 1)

    async def test_stop_cancels_a_blocked_cycle_without_logging_a_failure(self) -> None:
        cycle_started = asyncio.Event()

        async def run_cycle(_: datetime) -> None:
            cycle_started.set()
            await asyncio.Future()

        scheduler = Scheduler(run_cycle=run_cycle, clock=lambda: NOW)
        with self.assertNoLogs("tele_secretary.scheduler.runner", level="ERROR"):
            scheduler.start()
            await cycle_started.wait()
            await scheduler.stop()


if __name__ == "__main__":
    unittest.main()
