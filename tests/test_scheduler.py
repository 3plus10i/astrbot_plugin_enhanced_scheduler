"""Regression tests runnable without AstrBot: python -m unittest discover -s tests -v."""
import asyncio
import copy
import importlib.util
import json
import logging
import sys
import shutil
import uuid
import threading
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


class TestDirectory:
    # Inherit workspace ACLs on Windows (Python 3.13 mkdtemp uses a private ACL).
    def __init__(self):
        self.path = ROOT / "tests" / (".scheduler-test-" + uuid.uuid4().hex)
        self.path.mkdir()
        self.name = str(self.path)

    def cleanup(self):
        assert self.path.resolve().is_relative_to((ROOT / "tests").resolve())
        shutil.rmtree(self.path)


def load(name):
    spec = importlib.util.spec_from_file_location("test_" + name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Only the AstrBot boundary is stubbed; scheduling, storage, and executor are real.
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("scheduler-test")
api.logger.addHandler(logging.NullHandler())
event_api = types.ModuleType("astrbot.api.event")


class MessageChain:
    def message(self, text):
        self.text = text
        return self


class Star:
    def __init__(self, context):
        self.context = context


event_api.MessageChain = MessageChain
provider_api = types.ModuleType("astrbot.api.provider")
class ProviderRequest:
    def __init__(self, **kwargs):
        self.contexts = []
        self.model = None
        self.__dict__.update(kwargs)


provider_api.ProviderRequest = ProviderRequest
star_api = types.ModuleType("astrbot.api.star")
star_api.Star = Star
star_api.Context = object
star_api.StarTools = object
star_api.register = lambda *args: lambda cls: cls
sys.modules.update({"astrbot": types.ModuleType("astrbot"), "astrbot.api": api,
                    "astrbot.api.event": event_api, "astrbot.api.provider": provider_api,
                    "astrbot.api.star": star_api})
core = load("scheduler_core")
runtime_module = load("scheduler_runtime")
storage_module = load("storage")
executor_module = load("task_executor")
main_module = load("main")


def task(task_id="a"):
    """A task whose single trigger fires on every whole minute, so all tasks share points."""
    return {
        "id": task_id, "name": task_id, "enabled": True, "target": "test:FriendMessage:" + task_id,
        "content": {"mode": "fixed", "text": "hello"}, "last_success_time": 0,
        "triggers": [{"id": 1, "type": "interval", "config": {
            "base_time": "2026-01-01T00:00:00", "minutes": 1,
        }}],
    }


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.002)


class Clock:
    """The wall clock the runtime reads; shifted so a trigger point can be placed nearby.

    Only wall-clock reads are shifted, asyncio still waits in real time: an armed
    worker observes its trigger point arriving after a short real sleep.
    """

    def __init__(self):
        self.offset = 0.0

    def time(self):
        return time.time() + self.offset

    def idle(self):
        """Sit 30s into a whole minute, so the next trigger point is never imminent."""
        now = self.time()
        self.offset += (now // 60 * 60 + 30) - now

    def arm(self, triggers):
        """Shift the clock to 0.05s before the next trigger point of these triggers."""
        now = self.time()
        point = core.next_trigger_preview(triggers, now)
        if point is not None:
            self.offset += (point - 0.05) - now


class Executor:
    def __init__(self):
        self.calls = []
        self.blocked = {}
        self.errors = set()
        self.failures = set()

    async def execute(self, definition, now, source):
        task_id = definition["id"]
        self.calls.append((task_id, source, now))
        if task_id in self.blocked:
            await self.blocked[task_id].wait()
        if task_id in self.errors:
            raise TypeError("evaluate_task() missing argument")
        success = task_id not in self.failures
        return ("send_fixed" if success else "failed"), {
            "umo": definition["target"], "ok": success,
        }, "result", success


class PreviewTests(unittest.TestCase):
    """The preview must fold deterministic passive constraints (window/cooldown) in."""

    @staticmethod
    def at(month, day, hour, minute):
        return datetime(2026, month, day, hour, minute).timestamp()

    def interval(self, minutes, base="2026-01-01T00:00:00"):
        return {"id": 1, "type": "interval", "config": {"base_time": base, "minutes": minutes}}

    def test_window_defers_preview_to_next_open(self):
        # 2026-01-01 is a Thursday; a per-minute trigger idle at 02:00 waits until 08:00.
        triggers = [self.interval(1), {"id": 2, "type": "window", "config": {"start": "08:00", "end": "20:00"}}]
        self.assertAlmostEqual(core.next_trigger_preview(triggers, self.at(1, 1, 2, 0)), self.at(1, 1, 8, 0), delta=0.001)

    def test_window_weekdays_delay_to_matching_day(self):
        triggers = [
            self.interval(30),
            {"id": 2, "type": "window", "config": {"start": "08:00", "end": "09:00", "weekdays": [0]}},
        ]
        self.assertAlmostEqual(core.next_trigger_preview(triggers, self.at(1, 1, 2, 0)), self.at(1, 5, 8, 0), delta=0.001)

    def test_cooldown_waits_until_last_success_plus_window(self):
        now = self.at(1, 1, 2, 0)
        triggers = [self.interval(1), {"id": 2, "type": "cooldown", "config": {"hours": 2}}]
        self.assertAlmostEqual(core.next_trigger_preview(triggers, now, now), self.at(1, 1, 4, 0), delta=0.001)

    def test_random_trigger_does_not_delay_preview(self):
        triggers = [self.interval(5), {"id": 2, "type": "random", "config": {"threshold": 0.5}}]
        self.assertAlmostEqual(core.next_trigger_preview(triggers, self.at(1, 1, 2, 0)), self.at(1, 1, 2, 5), delta=0.001)

    def test_result_is_strictly_after_now(self):
        triggers = [self.interval(1)]
        now = self.at(1, 1, 2, 0)
        self.assertGreater(core.next_trigger_preview(triggers, now), now)

    def test_disjoint_windows_have_no_preview(self):
        triggers = [
            self.interval(1),
            {"id": 2, "type": "window", "config": {"start": "22:00", "end": "06:00"}},
            {"id": 3, "type": "window", "config": {"start": "08:00", "end": "20:00"}},
        ]
        self.assertIsNone(core.next_trigger_preview(triggers, self.at(1, 1, 9, 0)))

    def test_task_preview_combines_active_passive_and_random_conditions(self):
        now = self.at(1, 1, 2, 0)
        triggers = [self.interval(5),
                    {"id": 2, "type": "interval", "config": {"base_time": "2026-01-01T00:00:00", "minutes": 3}},
                    {"id": 3, "type": "window", "config": {"start": "08:00", "end": "09:00"}},
                    {"id": 4, "type": "window", "config": {"start": "08:30", "end": "10:00"}},
                    {"id": 5, "type": "cooldown", "config": {"hours": 7}},
                    {"id": 6, "type": "random", "config": {"threshold": 0.5}},
                    {"id": 7, "type": "random", "config": {"threshold": 0.5}}]
        # 冷却在 08:46 到期，最快的主动条件在 08:48 到点；不实际采样随机数。
        success = self.at(1, 1, 1, 46)
        with patch.object(core._random, "random", side_effect=AssertionError("预览不能采样")):
            preview = core.next_task_preview(triggers, now, success)
        self.assertEqual(preview["next_fire"], self.at(1, 1, 8, 48))
        self.assertEqual(preview["execution_probability"], 0.25)
        self.assertEqual(core.next_task_preview([self.interval(5)], now)["execution_probability"], 1)

    def test_task_preview_zero_probability_and_invalid_conditions(self):
        triggers = [self.interval(5), {"id": 2, "type": "random", "config": {"threshold": 0}}]
        preview = core.next_task_preview(triggers, self.at(1, 1, 2, 0))
        self.assertIsNone(preview["next_fire"])
        self.assertEqual(preview["execution_probability"], 0)
        triggers[1]["config"]["threshold"] = 2
        preview = core.next_task_preview(triggers, self.at(1, 1, 2, 0))
        self.assertIsNone(preview["next_fire"])
        self.assertIsNone(preview["execution_probability"])

    def test_trigger_labels_sort_each_group_and_keep_ids(self):
        stamp = 1791710000000
        triggers = [{"id": stamp + 4, "type": "random"}, {"id": stamp + 3, "type": "cron"},
                    {"id": stamp + 1, "type": "interval"}, {"id": stamp + 2, "type": "window"}]
        original = copy.deepcopy(triggers)
        self.assertEqual(core.trigger_labels(triggers), {stamp + 1: "A1", stamp + 3: "A2", stamp + 2: "B1", stamp + 4: "B2"})
        self.assertEqual(triggers, original)
        triggers = [tg for tg in triggers if tg["id"] != stamp + 2]
        self.assertEqual(core.trigger_labels(triggers)[stamp + 4], "B1")


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TestDirectory()
        assert Path(self.directory.name).resolve().is_relative_to((ROOT / "tests").resolve())
        self.addCleanup(self.directory.cleanup)
        self.store = storage_module.TaskStore(self.directory.name, {}, core)
        self.executor = Executor()
        self.logger = Mock()
        self.clock = Clock()
        clock_patch = patch.object(runtime_module, "time", self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.clock.idle()
        self.runtime = runtime_module.SchedulerRuntime(core, self.store, self.executor, self.logger)
        self.addAsyncCleanup(self.runtime.close)

    async def _start_armed(self, *task_ids):
        """Start with the clock armed, so each listed task reaches its next point in ~0.1s."""
        for task_id in task_ids or list(self.store.tasks):
            self.clock.arm(self.store.tasks[task_id]["triggers"])
        await self.runtime.start()

    async def test_startup_arms_future_deadlines_without_mutating_tasks(self):
        self.store.tasks.update(a=task("a"), b=task("b"), c=task("c"))
        self.store.tasks["a"]["last_success_time"] = time.time() - 3600
        previous = copy.deepcopy(self.store.tasks)
        await self.runtime.start()
        await until(lambda: all(self.runtime.status(key).get("next_check") for key in previous))
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.store.run_logs, [])
        self.assertEqual(self.store.tasks, previous)
        await self.runtime.start()
        self.assertEqual(self.store.tasks, previous)

    async def test_next_future_fire_still_runs_after_startup(self):
        self.store.tasks["a"] = task()
        await self.runtime.start()
        next_fire = core.next_trigger_preview(self.store.tasks["a"]["triggers"], time.time())
        async with self.runtime.lock("a"):
            with patch.object(runtime_module, "time", types.SimpleNamespace(time=lambda: next_fire + 0.01)):
                await self.runtime._check("a", self.store.tasks["a"], next_fire)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertNotIn("last_fired", self.store.tasks["a"]["triggers"][0])
        self.assertEqual(self.store.run_logs[0]["action"], "send_fixed")

    async def test_startup_does_not_persist_or_consume_a_trigger(self):
        self.store.tasks["a"] = task()
        previous = copy.deepcopy(self.store.tasks)
        await self.runtime.start()
        self.assertEqual(self.store.tasks, previous)
        self.assertEqual(self.executor.calls, [])

    async def test_slow_task_does_not_block_another(self):
        self.store.tasks.update(a=task("a"), b=task("b"))
        self.executor.blocked["a"] = asyncio.Event()
        await self._start_armed()
        await until(lambda: self.runtime.status("a")["state"] == "running")
        await until(lambda: any(log["task_id"] == "b" for log in self.store.run_logs))
        self.assertGreater(self.store.tasks["b"]["last_success_time"], 0)

    async def test_invalid_passive_faults_once_and_does_not_spin(self):
        bad = task("a")
        bad["triggers"].append({"id": 2, "type": "random", "config": {"threshold": 2}})
        self.store.tasks.update(a=bad, b=task("b"))
        await self._start_armed()
        await until(lambda: self.runtime.status("a")["state"] == "faulted" and len(self.executor.calls) == 1)
        for _ in range(5):
            self.runtime.wake_all()
            await asyncio.sleep(0.005)
        self.assertEqual(self.logger.error.call_count, 1)
        self.assertEqual([call[0] for call in self.executor.calls], ["b"])

    async def test_execution_exception_logged_and_not_retried(self):
        self.store.tasks["a"] = task()
        self.executor.errors.add("a")
        await self._start_armed()
        await until(lambda: self.runtime.status("a")["state"] == "faulted")
        self.runtime.wake_all()
        await asyncio.sleep(0.02)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.store.run_logs[0]["action"], "failed")
        self.assertNotIn("last_fired", self.store.tasks["a"]["triggers"][0])

    async def test_send_failure_consumes_occurrence_without_advancing_cooldown(self):
        self.store.tasks["a"] = task()
        self.executor.failures.add("a")
        await self._start_armed()
        await until(lambda: len(self.store.run_logs) == 1)
        self.runtime.wake_all()
        await asyncio.sleep(0.02)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.store.tasks["a"]["last_success_time"], 0)
        self.assertEqual(self.store.run_logs[0]["action"], "failed")

    async def test_skip_advances_trigger_but_not_cooldown(self):
        definition = task()
        definition["triggers"].append({"id": 2, "type": "random", "config": {"threshold": 0}})
        self.store.tasks["a"] = definition
        await self._start_armed()
        await until(lambda: len(self.store.run_logs) == 1)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.store.run_logs[0]["action"], "skipped")
        log = self.store.run_logs[0]
        self.assertEqual(log["trigger_labels"], {1: "A1", 2: "B1"})
        definition["triggers"].pop()
        self.assertEqual(log["trigger_labels"], {1: "A1", 2: "B1"})
        self.assertEqual(definition["last_success_time"], 0)

    async def test_cooldown_defers_worker_instead_of_logging_skips(self):
        definition = task("a")
        definition["last_success_time"] = self.clock.time()
        definition["triggers"].append({"id": 2, "type": "cooldown", "config": {"minutes": 10}})
        self.store.tasks["a"] = definition
        # Arm the clock right before the next interval point; the cooldown still holds,
        # so the worker must sleep through it instead of writing skipped logs every minute.
        self.clock.arm([definition["triggers"][0]])
        await self.runtime.start()
        await asyncio.sleep(0.2)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.store.run_logs, [])

    async def test_edit_wakes_only_its_task_and_recovers_fault(self):
        bad = task()
        bad["triggers"][0]["config"]["minutes"] = -1
        self.store.tasks.update(a=bad, b=task("b"))
        await self.runtime.start()
        await until(lambda: self.runtime.status("a")["state"] == "faulted")
        other_worker = self.runtime._workers["b"]
        async with self.runtime.lock("a"):
            self.store.tasks["a"] = task()
            self.clock.arm(self.store.tasks["a"]["triggers"])
            await self.store.save()
            self.runtime.refresh("a")
        await until(lambda: len(self.executor.calls) == 1)
        self.assertIs(self.runtime._workers["b"], other_worker)
        self.assertEqual(self.executor.calls[0][0], "a")

    async def test_manual_and_automatic_cannot_overlap(self):
        self.store.tasks["a"] = task()
        self.executor.blocked["a"] = asyncio.Event()
        await self._start_armed()
        await until(lambda: len(self.executor.calls) == 1)
        with self.assertRaisesRegex(RuntimeError, "正在执行"):
            await self.runtime.trigger_now("a")
        self.assertEqual(len(self.executor.calls), 1)

    async def test_manual_duplicates_rejected_and_close_cancels_manual(self):
        self.store.tasks["a"] = task()
        self.executor.blocked["a"] = asyncio.Event()
        await self.runtime.start()
        manual = asyncio.create_task(self.runtime.trigger_now("a"))
        await until(lambda: len(self.executor.calls) == 1)
        with self.assertRaises(RuntimeError):
            await self.runtime.trigger_now("a")
        await self.runtime.close()
        with self.assertRaises(asyncio.CancelledError):
            await manual
        self.assertFalse(self.runtime._workers)

    async def test_disabled_and_deleted_workers_stop(self):
        self.store.tasks.update(a=task("a"), b=task("b"))
        await self.runtime.start()
        workers = list(self.runtime._workers.values())
        async with self.runtime.lock("a"):
            self.store.tasks["a"]["enabled"] = False
            self.runtime.refresh("a")
        async with self.runtime.lock("b"):
            del self.store.tasks["b"]
            self.runtime.refresh("b")
        await until(lambda: all(worker.done() for worker in workers))
        self.assertEqual(self.executor.calls, [])

    async def test_edit_waits_for_inflight_execution(self):
        self.store.tasks["a"] = task()
        gate = self.executor.blocked["a"] = asyncio.Event()
        await self._start_armed()
        await until(lambda: len(self.executor.calls) == 1)
        edited = asyncio.Event()

        async def edit():
            async with self.runtime.lock("a"):
                self.store.tasks["a"]["target"] = "new:FriendMessage:target"
                self.runtime.refresh("a")
                edited.set()

        pending = asyncio.create_task(edit())
        await asyncio.sleep(0.01)
        self.assertFalse(edited.is_set())
        gate.set()
        await pending
        self.assertEqual(self.store.run_logs[0]["target_result"]["umo"], "test:FriendMessage:a")

    async def test_disk_failure_prevents_send_and_faults_without_log_flood(self):
        self.store.tasks["a"] = task()
        await self._start_armed()
        self.store._write_json = Mock(side_effect=OSError("disk full"))
        await until(lambda: self.runtime.status("a")["state"] == "faulted")
        await asyncio.sleep(0.02)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.logger.error.call_count, 1)
        self.assertIn("日志保存失败", self.runtime.status("a")["error"])

    async def test_trigger_is_persisted_before_sending(self):
        self.store.tasks["a"] = task()
        self.executor.blocked["a"] = asyncio.Event()
        await self._start_armed()
        await until(lambda: len(self.executor.calls) == 1)
        saved = json.loads(Path(self.store.data_file).read_text(encoding="utf-8"))
        self.assertNotIn("last_fired", saved["tasks"]["a"]["triggers"][0])

    async def test_legacy_task_cannot_be_manually_sent(self):
        definition = task()
        definition.update(enabled=False, migration_notice="请重新配置")
        self.store.tasks["a"] = definition
        await self.runtime.start()
        with self.assertRaisesRegex(ValueError, "不再支持旧任务格式"):
            await self.runtime.trigger_now("a")
        self.assertEqual(self.executor.calls, [])

    async def test_manual_fault_stops_automatic_wait_until_edit(self):
        self.store.tasks["a"] = task()
        self.executor.errors.add("a")
        await self.runtime.start()
        await asyncio.sleep(0.01)
        with self.assertRaises(TypeError):
            await self.runtime.trigger_now("a")
        self.assertEqual(self.store.run_logs[-1]["source"], "manual")
        # A faulted task stays parked even when its trigger point arrives.
        self.clock.arm(self.store.tasks["a"]["triggers"])
        self.runtime.wake_all()
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.runtime.status("a")["state"], "faulted")

    async def test_refresh_recomputes_from_current_wall_clock_without_cursor(self):
        self.store.tasks["a"] = task()
        await self.runtime.start()
        self.runtime.refresh("a")
        await asyncio.sleep(0.05)
        self.assertEqual(self.executor.calls, [])
        self.clock.arm(self.store.tasks["a"]["triggers"])
        self.runtime.refresh("a")
        await until(lambda: len(self.executor.calls) == 1)
        self.assertNotIn("last_fired", self.store.tasks["a"]["triggers"][0])

    async def test_success_time_uses_completion_time(self):
        self.store.tasks["a"] = task()
        gate = self.executor.blocked["a"] = asyncio.Event()
        await self._start_armed()
        await until(lambda: len(self.executor.calls) == 1)
        completed_after = self.clock.time()
        gate.set()
        await until(lambda: self.store.tasks["a"]["last_success_time"] > 0)
        self.assertGreaterEqual(self.store.tasks["a"]["last_success_time"], completed_after)


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TestDirectory()
        assert Path(self.directory.name).resolve().is_relative_to((ROOT / "tests").resolve())
        self.addCleanup(self.directory.cleanup)

    async def test_legacy_tasks_are_skipped_without_conversion(self):
        variants = {}
        for field, value in (("targets", ["first", "second"]), ("logic_expr", "1 and 2"),
                             ("migration_notice", "请重新配置")):
            old = task(field)
            old[field] = value
            variants[field] = old
        old_mode = task("mode")
        old_mode["content"] = {"text": "hello", "use_llm": True}
        variants["mode"] = old_mode
        old_interval = task("interval")
        old_interval["triggers"][0]["config"].pop("base_time")
        variants["interval"] = old_interval
        cursor = task("cursor")
        cursor["triggers"][0]["last_fired"] = 1
        variants["cursor"] = cursor
        variants["malformed"] = "broken"
        variants["current"] = task("current")
        path = Path(self.directory.name) / "tasks.json"
        original = json.dumps({"tasks": variants, "logs": [{"detail": "legacy"}]})
        path.write_text(original, encoding="utf-8")
        store = storage_module.TaskStore(self.directory.name, {}, core)
        self.assertEqual(store.tasks, {"current": variants["current"]})
        self.assertEqual(store.run_logs, [])
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        await store.save()
        reloaded = storage_module.TaskStore(self.directory.name, {}, core)
        self.assertEqual(reloaded.tasks, store.tasks)
        self.assertNotIn("logs", reloaded.data)

    async def test_legacy_llm_file_is_not_imported_or_renamed(self):
        path = Path(self.directory.name) / "llm_calls.jsonl"
        original = json.dumps({"ts": time.time(), "task_name": "old", "ok": True})
        path.write_text(original + "\n", encoding="utf-8")
        store = storage_module.TaskStore(self.directory.name, {}, core)
        self.assertEqual(store._list_llm_records_io(), [])
        self.assertEqual(path.read_text(encoding="utf-8"), original + "\n")
        self.assertFalse(path.with_suffix(".jsonl.migrated").exists())

    async def test_corrupt_file_is_not_overwritten(self):
        path = Path(self.directory.name) / "tasks.json"
        path.write_text("broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            storage_module.TaskStore(self.directory.name, {}, core)
        self.assertEqual(path.read_text(encoding="utf-8"), "broken")

    async def test_extracted_llm_store_preserves_images_and_pagination(self):
        store = storage_module.TaskStore(self.directory.name, {}, core)
        await store.append_llm_log({"ts": time.time(), "task_name": "test", "request": {
            "prompt": "hello", "image": "data:image/png;base64,aGVsbG8=",
        }, "ok": True})
        total, rows = await store.io(store._llm_log_page_io, 0, 20)
        self.assertEqual(total, 1)
        metadata, body = await store.io(store._read_llm_record_io, rows[0]["name"])
        self.assertIn("[img:", body["request"]["image"])
        image = await store.io(store._read_llm_image_io, metadata["images"][0]["name"])
        self.assertEqual(image, "data:image/png;base64,aGVsbG8=")

    async def test_snapshot_and_cancelled_write_keep_io_serialized(self):
        store = storage_module.TaskStore(self.directory.name, {}, core)
        store.tasks["a"] = task()
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = store._save_snapshot
        snapshots = []

        def blocked(data, logs):
            snapshots.append(copy.deepcopy(data))
            started.set()
            release.wait(2)
            original(data, logs)

        store._save_snapshot = blocked
        first = asyncio.create_task(store.save())
        await until(started.is_set)
        store.tasks["a"]["name"] = "edited"
        first.cancel()
        second = asyncio.create_task(store.save())
        await asyncio.sleep(0.01)
        first.cancel()  # A second shutdown/caller cancellation must not release the I/O lock.
        await asyncio.sleep(0.005)
        self.assertEqual(len(snapshots), 1)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await second
        self.assertEqual(snapshots[0]["tasks"]["a"]["name"], "a")
        self.assertEqual(json.loads(Path(store.data_file).read_text(encoding="utf-8"))["tasks"]["a"]["name"], "edited")


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_validation_rejects_multi_target_and_accepts_single(self):
        plugin = main_module.EnhancedSchedulerPlugin.__new__(main_module.EnhancedSchedulerPlugin)
        plugin.store = types.SimpleNamespace(data={"tasks": {}})
        multi = task()
        multi["targets"] = ["one", "two"]
        self.assertFalse(plugin._upsert_task(multi)[0])
        single = task()
        self.assertTrue(plugin._upsert_task(single)[0])
        self.assertEqual(plugin.store.data["tasks"]["a"]["target"], single["target"])

    async def test_reloading_ignores_stale_module_caches(self):
        old = types.ModuleType("scheduler_core")
        old.evaluate_task = lambda triggers, logic, now, success: None
        sys.modules["scheduler_core"] = old
        self.addCleanup(sys.modules.pop, "scheduler_core", None)
        fresh = main_module._load_local("scheduler_core")
        self.assertIsNot(fresh, old)
        self.assertFalse(fresh.evaluate_task([], time.time(), 0)["has_active_fire"])
        self.assertIsNot(main_module._load_local("scheduler_runtime"), main_module._load_local("scheduler_runtime"))

    async def test_executor_sends_once_and_reports_single_result(self):
        sends = []

        async def send(umo, chain):
            sends.append((umo, chain.text))

        executor = executor_module.TaskExecutor(types.SimpleNamespace(send_message=send), {}, core, None)
        definition = task()
        result = await executor.execute(definition, time.time())
        self.assertEqual(sends, [(definition["target"], "hello")])
        self.assertIsInstance(result[1], dict)
        self.assertTrue(result[3])

    async def test_executor_does_not_resend_uncertain_delivery(self):
        calls = 0

        async def send(umo, chain):
            nonlocal calls
            calls += 1
            raise asyncio.TimeoutError()

        executor = executor_module.TaskExecutor(types.SimpleNamespace(send_message=send), {}, core, None)
        result = await executor.execute(task(), time.time())
        self.assertEqual(calls, 1)
        self.assertFalse(result[3])
        self.assertIn("不自动重发", result[2])

    async def test_executor_serializes_same_conversation_only(self):
        calls = []
        release = asyncio.Event()

        async def send(umo, chain):
            calls.append(umo)
            if umo == "shared":
                await release.wait()

        executor = executor_module.TaskExecutor(types.SimpleNamespace(send_message=send), {}, core, None)
        first, second, third = task("a"), task("b"), task("c")
        first["target"] = second["target"] = "shared"
        work = [asyncio.create_task(executor.execute(t, time.time())) for t in (first, second, third)]
        try:
            await until(lambda: len(calls) >= 2)
            self.assertEqual(calls.count("shared"), 1)
            self.assertIn(third["target"], calls)
        finally:
            release.set()
            await asyncio.gather(*work)
        self.assertEqual(calls.count("shared"), 2)

    async def test_both_ai_modes_keep_target_and_call_logs(self):
        calls, sends, logs, histories = [], [], [], []

        async def provider(umo):
            return "provider-for-" + umo

        async def generate(**kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(completion_text="AI reply")

        async def send(umo, chain):
            sends.append((umo, chain.text))

        async def record(entry):
            logs.append(entry)

        async def conversation_id(umo):
            return "conversation"

        async def conversation(umo, cid):
            return types.SimpleNamespace(history="[]", persona_id=None)

        async def update(umo, cid, history):
            histories.append((umo, history))

        context = types.SimpleNamespace(
            get_current_chat_provider_id=provider, llm_generate=generate,
            send_message=send, get_config=lambda **kwargs: {},
            conversation_manager=types.SimpleNamespace(
                get_curr_conversation_id=conversation_id, get_conversation=conversation,
                update_conversation=update,
            ),
        )
        executor = executor_module.TaskExecutor(context, {}, core, types.SimpleNamespace(append_llm_log=record))
        for mode in ("standalone", "conversation"):
            definition = task(mode)
            definition["content"]["mode"] = mode
            result = await executor.execute(definition, time.time())
            self.assertTrue(result[3])
            self.assertEqual(sends[-1][0], definition["target"])
            self.assertEqual(calls[-1]["chat_provider_id"], "provider-for-" + definition["target"])
            self.assertEqual(logs[-1]["mode"], mode)
        self.assertEqual(histories[0][0], task("conversation")["target"])


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TestDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tools_patch = patch.object(main_module, "StarTools", types.SimpleNamespace(get_data_dir=lambda _: self.directory.name))
        self.tools_patch.start()
        self.addCleanup(self.tools_patch.stop)

        class Request:
            body = {}
            args = {}

            @property
            def json(self):
                async def value():
                    return self.body
                return value()

        self.request = Request()
        quart = types.ModuleType("quart")
        quart.request = self.request
        quart.jsonify = lambda value: value
        quart_patch = patch.dict(sys.modules, {"quart": quart})
        quart_patch.start()
        self.addCleanup(quart_patch.stop)
        self.plugin = main_module.EnhancedSchedulerPlugin(types.SimpleNamespace(register_web_api=Mock()), {})
        await self.plugin.initialize()
        self.addAsyncCleanup(self.plugin.terminate)

    async def test_create_copy_delete_and_runtime_status(self):
        self.request.body = task()
        created = await self.plugin._api_upsert_task()
        self.assertEqual(created["status"], "success")
        data = await self.plugin._api_get_data()
        self.assertEqual(data["tasks"]["a"]["target"], self.request.body["target"])
        self.assertEqual(data["tasks"]["a"]["_runtime"]["state"], "waiting")
        self.request.body = {"id": "a"}
        copied = await self.plugin._api_copy_task()
        copy_id = copied["id"]
        self.assertFalse(self.plugin.store.tasks[copy_id]["enabled"])
        self.assertEqual(self.plugin.store.tasks[copy_id]["target"], self.plugin.store.tasks["a"]["target"])
        deleted = await self.plugin._api_delete_task()
        self.assertEqual(deleted["status"], "success")
        self.assertNotIn("a", self.plugin.store.tasks)
        self.assertNotIn("a", self.plugin.runtime._workers)

    async def test_multi_target_request_is_rejected_without_creating_task(self):
        self.request.body = dict(task(), targets=["first", "second"])
        response, status = await self.plugin._api_upsert_task()
        self.assertEqual(status, 400)
        self.assertIn("不再支持", response["message"])
        self.assertEqual(self.plugin.store.tasks, {})

    async def test_task_time_previews_use_saved_cooldown_and_probability(self):
        now = PreviewTests.at(1, 1, 2, 0)
        definition = task()
        definition["last_success_time"] = now
        definition["triggers"].extend([
            {"id": 1791710000001, "type": "cooldown", "config": {"hours": 2}},
            {"id": 1791710000002, "type": "random", "config": {"threshold": 0.5}},
            {"id": 1791710000003, "type": "random", "config": {"threshold": 0.5}},
        ])
        self.plugin.store.tasks["a"] = definition
        self.request.body = {"task_id": "a", "triggers": definition["triggers"]}
        with patch.object(main_module.time, "time", return_value=now):
            data = await self.plugin._api_get_data()
            validation = (await self.plugin._api_validate())["results"]
            preview = await self.plugin._api_preview_next()
        expected = PreviewTests.at(1, 1, 4, 0)
        self.assertEqual(data["tasks"]["a"]["_next_fire"], expected)
        self.assertEqual(data["tasks"]["a"]["_execution_probability"], 0.25)
        for response in (validation, preview):
            self.assertEqual(response["next_fire"], expected)
            self.assertEqual(response["execution_probability"], 0.25)
        self.assertEqual(len(validation["triggers"][0]["future_fires"]), 3)

    async def test_timestamp_ids_are_preserved_and_duplicates_rejected(self):
        definition = task()
        stamp = 1791710000000
        definition["triggers"][0]["id"] = stamp
        self.request.body = definition
        self.assertEqual((await self.plugin._api_upsert_task())["status"], "success")
        self.assertEqual(self.plugin.store.tasks["a"]["triggers"][0]["id"], stamp)
        for invalid in (True, core.MAX_TRIGGER_ID + 1):
            self.request.body = copy.deepcopy(definition)
            self.request.body["triggers"][0]["id"] = invalid
            self.assertEqual((await self.plugin._api_upsert_task())[1], 400)
        self.request.body = copy.deepcopy(definition)
        self.request.body["triggers"].append({"id": stamp, "type": "random", "config": {"threshold": 0.5}})
        self.assertEqual((await self.plugin._api_upsert_task())[1], 400)
        self.assertEqual(len(self.plugin.store.tasks["a"]["triggers"]), 1)
        validation = (await self.plugin._api_validate())["results"]
        self.assertFalse(validation["triggers"][1]["ok"])
        self.assertIn("重复", validation["triggers"][1]["msg"])
        self.assertIsNone(validation["next_fire"])

    async def test_task_name_limit_on_create_update_and_copy(self):
        self.request.body = dict(task(), name="中" * 33)
        response, status = await self.plugin._api_upsert_task()
        self.assertEqual(status, 400)
        self.assertIn("32", response["message"])
        self.assertEqual(self.plugin.store.tasks, {})
        self.request.body = dict(task(), name="中" * 32)
        self.assertEqual((await self.plugin._api_upsert_task())["status"], "success")
        self.request.body = dict(task(), name="改" * 33)
        response, status = await self.plugin._api_upsert_task()
        self.assertEqual(status, 400)
        self.assertEqual(self.plugin.store.tasks["a"]["name"], "中" * 32)
        self.request.body = {"id": "a"}
        copied = await self.plugin._api_copy_task()
        name = self.plugin.store.tasks[copied["id"]]["name"]
        self.assertEqual(len(name), 32)
        self.assertTrue(name.endswith(" (副本)"))

    async def test_copying_copies_numbers_names_within_shared_limit(self):
        for case, limit, original_name in (("short", main_module.TASK_NAME_MAX_LENGTH, "每日提醒"),
                                           ("long", main_module.TASK_NAME_MAX_LENGTH, "中" * main_module.TASK_NAME_MAX_LENGTH),
                                           ("custom", 12, "改" * 12)):
            with self.subTest(case=case), patch.object(main_module, "TASK_NAME_MAX_LENGTH", limit):
                source_id = "source-" + case
                self.request.body = dict(task(source_id), name=original_name, last_success_time=123)
                self.assertEqual((await self.plugin._api_upsert_task())["status"], "success")
                original = copy.deepcopy(self.plugin.store.tasks[source_id])
                names = {original_name}
                ids = {source_id}
                for number in range(1, 13):
                    self.request.body = {"id": source_id}
                    response = await self.plugin._api_copy_task()
                    self.assertEqual(response["status"], "success")
                    copied = self.plugin.store.tasks[response["id"]]
                    suffix = main_module.COPY_NAME_SUFFIX if number == 1 else f" (副本 {number})"
                    self.assertEqual(copied["name"], original_name[:limit - len(suffix)] + suffix)
                    self.assertLessEqual(len(copied["name"]), limit)
                    self.assertNotIn(copied["name"], names)
                    self.assertNotIn(copied["id"], ids)
                    self.assertFalse(copied["enabled"])
                    self.assertEqual(copied["last_success_time"], 0)
                    for key in ("target", "triggers", "content"):
                        self.assertEqual(copied[key], original[key])
                    names.add(copied["name"])
                    ids.add(copied["id"])
                    source_id = copied["id"]
                self.assertEqual(self.plugin.store.tasks[original["id"]], original)
                copied["content"]["text"] = "修改最后一个副本"
                self.assertEqual(self.plugin.store.tasks[original["id"]]["content"], original["content"])

    async def test_copy_skips_existing_names_after_truncation(self):
        limit = main_module.TASK_NAME_MAX_LENGTH
        original_name = "长" * limit
        occupied = []
        for number in range(1, 11):
            suffix = main_module.COPY_NAME_SUFFIX if number == 1 else f" (副本 {number})"
            occupied.append(original_name[:limit - len(suffix)] + suffix)
        for index, name in enumerate([original_name, *occupied]):
            self.request.body = dict(task(str(index)), name=name)
            self.assertEqual((await self.plugin._api_upsert_task())["status"], "success")
        previous = copy.deepcopy(self.plugin.store.tasks)
        # 既覆盖再次复制原任务，也覆盖复制已有副本后跳过所有重名编号。
        for source_id, number in (("0", 11), ("1", 12)):
            self.request.body = {"id": source_id}
            response = await self.plugin._api_copy_task()
            self.assertEqual(response["status"], "success")
            name = self.plugin.store.tasks[response["id"]]["name"]
            suffix = f" (副本 {number})"
            self.assertEqual(name, original_name[:limit - len(suffix)] + suffix)
            self.assertLessEqual(len(name), limit)
        for task_id, source in previous.items():
            self.assertEqual(self.plugin.store.tasks[task_id], source)

    async def test_copy_of_numbered_copy_continues_its_number(self):
        self.request.body = dict(task(), name="提醒 (副本 4)")
        self.assertEqual((await self.plugin._api_upsert_task())["status"], "success")
        self.request.body = {"id": "a"}
        response = await self.plugin._api_copy_task()
        self.assertEqual(response["status"], "success")
        self.assertEqual(self.plugin.store.tasks[response["id"]]["name"], "提醒 (副本 5)")

    async def test_copy_collapses_repeated_suffixes(self):
        self.request.body = dict(task(), name="提醒 (副本) (副本)")
        self.assertEqual((await self.plugin._api_upsert_task())["status"], "success")
        self.request.body = {"id": "a"}
        response = await self.plugin._api_copy_task()
        self.assertEqual(response["status"], "success")
        self.assertEqual(self.plugin.store.tasks[response["id"]]["name"], "提醒 (副本 2)")

    async def test_old_fields_cannot_be_saved_as_current_tasks(self):
        for field, value in (("logic_expr", "1"), ("migration_notice", "重新配置")):
            with self.subTest(field=field):
                self.request.body = dict(task(), **{field: value})
                response, status = await self.plugin._api_upsert_task()
                self.assertEqual(status, 400)
                self.assertEqual(self.plugin.store.tasks, {})
        self.request.body = dict(task(), content={"use_llm": True, "text": "hello"})
        response, status = await self.plugin._api_upsert_task()
        self.assertEqual(status, 400)
        self.assertIn("use_llm", response["message"])
        self.request.body = task()
        self.request.body["triggers"][0]["last_fired"] = 123
        response, status = await self.plugin._api_upsert_task()
        self.assertEqual(status, 400)
        self.assertIn("last_fired", response["message"])

    async def test_name_limit_is_shared_by_ui_save_and_copy(self):
        with patch.object(main_module, "TASK_NAME_MAX_LENGTH", 12):
            response = await self.plugin._api_get_data()
            self.assertEqual(response["limits"]["task_name_max_length"], 12)
            self.request.body = dict(task(), name="中" * 13)
            self.assertEqual((await self.plugin._api_upsert_task())[1], 400)
            self.request.body = dict(task(), name="中" * 12)
            self.assertEqual((await self.plugin._api_upsert_task())["status"], "success")
            self.request.body = {"id": "a"}
            copied = await self.plugin._api_copy_task()
            name = self.plugin.store.tasks[copied["id"]]["name"]
            self.assertEqual(len(name), 12)
            self.assertTrue(name.endswith(main_module.COPY_NAME_SUFFIX))


if __name__ == "__main__":
    unittest.main()
