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


def task(task_id="a", due=True):
    now = time.time()
    return {
        "id": task_id, "name": task_id, "enabled": True, "target": "test:FriendMessage:" + task_id,
        "content": {"mode": "fixed", "text": "hello"}, "last_success_time": 0,
        "triggers": [{"id": 1, "type": "interval", "config": {
            "base_time": datetime.fromtimestamp(now - 600).isoformat(timespec="seconds"),
            "minutes": 1,
        }, "last_fired": now - 120 if due else now}],
    }


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.002)


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


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TestDirectory()
        assert Path(self.directory.name).resolve().is_relative_to((ROOT / "tests").resolve())
        self.addCleanup(self.directory.cleanup)
        self.store = storage_module.TaskStore(self.directory.name, {}, core)
        self.executor = Executor()
        self.logger = Mock()
        self.runtime = runtime_module.SchedulerRuntime(core, self.store, self.executor, {}, self.logger)
        self.addAsyncCleanup(self.runtime.close)

    async def test_slow_task_does_not_block_another(self):
        self.store.tasks.update(a=task("a"), b=task("b"))
        self.executor.blocked["a"] = asyncio.Event()
        self.runtime.start()
        await until(lambda: any(log["task_id"] == "b" for log in self.store.run_logs))
        self.assertEqual(self.runtime.status("a")["state"], "running")
        self.assertGreater(self.store.tasks["b"]["last_success_time"], 0)

    async def test_invalid_passive_faults_once_and_does_not_spin(self):
        bad = task("a")
        bad["triggers"].append({"id": 2, "type": "random", "config": {"threshold": 2}})
        self.store.tasks.update(a=bad, b=task("b"))
        self.runtime.start()
        await until(lambda: self.runtime.status("a")["state"] == "faulted" and len(self.executor.calls) == 1)
        for _ in range(5):
            self.runtime.wake_all()
            await asyncio.sleep(0.005)
        self.assertEqual(self.logger.error.call_count, 1)
        self.assertEqual([call[0] for call in self.executor.calls], ["b"])

    async def test_execution_exception_logged_and_not_retried(self):
        self.store.tasks["a"] = task()
        self.executor.errors.add("a")
        self.runtime.start()
        await until(lambda: self.runtime.status("a")["state"] == "faulted")
        self.runtime.wake_all()
        await asyncio.sleep(0.02)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.store.run_logs[0]["action"], "failed")
        self.assertFalse(core.check_active_fire(self.store.tasks["a"]["triggers"][0], time.time())[0])

    async def test_send_failure_consumes_occurrence_without_advancing_cooldown(self):
        self.store.tasks["a"] = task()
        self.executor.failures.add("a")
        self.runtime.start()
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
        self.runtime.start()
        await until(lambda: len(self.store.run_logs) == 1)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.store.run_logs[0]["action"], "skipped")
        self.assertEqual(definition["last_success_time"], 0)

    async def test_edit_wakes_only_its_task_and_recovers_fault(self):
        bad = task()
        bad["triggers"][0]["config"]["minutes"] = -1
        self.store.tasks.update(a=bad, b=task("b", due=False))
        self.runtime.start()
        await until(lambda: self.runtime.status("a")["state"] == "faulted")
        other_worker = self.runtime._workers["b"]
        async with self.runtime.lock("a"):
            self.store.tasks["a"] = task()
            await self.store.save()
            self.runtime.refresh("a")
        await until(lambda: len(self.executor.calls) == 1)
        self.assertIs(self.runtime._workers["b"], other_worker)
        self.assertEqual(self.executor.calls[0][0], "a")

    async def test_manual_and_automatic_cannot_overlap(self):
        self.store.tasks["a"] = task()
        self.executor.blocked["a"] = asyncio.Event()
        self.runtime.start()
        await until(lambda: len(self.executor.calls) == 1)
        with self.assertRaisesRegex(RuntimeError, "正在执行"):
            await self.runtime.trigger_now("a")
        self.assertEqual(len(self.executor.calls), 1)

    async def test_manual_duplicates_rejected_and_close_cancels_manual(self):
        self.store.tasks["a"] = task(due=False)
        self.executor.blocked["a"] = asyncio.Event()
        self.runtime.start()
        manual = asyncio.create_task(self.runtime.trigger_now("a"))
        await until(lambda: len(self.executor.calls) == 1)
        with self.assertRaises(RuntimeError):
            await self.runtime.trigger_now("a")
        await self.runtime.close()
        with self.assertRaises(asyncio.CancelledError):
            await manual
        self.assertFalse(self.runtime._workers)

    async def test_disabled_and_deleted_workers_stop(self):
        self.store.tasks.update(a=task(due=False), b=task("b", due=False))
        self.runtime.start()
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
        self.runtime.start()
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
        self.store._write_json = Mock(side_effect=OSError("disk full"))
        self.runtime.start()
        await until(lambda: self.runtime.status("a")["state"] == "faulted")
        await asyncio.sleep(0.02)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.logger.error.call_count, 1)
        self.assertIn("日志保存失败", self.runtime.status("a")["error"])

    async def test_trigger_is_persisted_before_sending(self):
        self.store.tasks["a"] = task()
        self.executor.blocked["a"] = asyncio.Event()
        self.runtime.start()
        await until(lambda: len(self.executor.calls) == 1)
        saved = json.loads(Path(self.store.data_file).read_text(encoding="utf-8"))
        self.assertFalse(core.check_active_fire(saved["tasks"]["a"]["triggers"][0], time.time())[0])

    async def test_migrated_task_cannot_be_manually_sent_before_edit(self):
        definition = task()
        definition.update(enabled=False, migration_notice="请重新配置")
        self.store.tasks["a"] = definition
        self.runtime.start()
        with self.assertRaisesRegex(ValueError, "先编辑"):
            await self.runtime.trigger_now("a")
        self.assertEqual(self.executor.calls, [])

    async def test_manual_fault_stops_automatic_wait_until_edit(self):
        self.store.tasks["a"] = task(due=False)
        self.executor.errors.add("a")
        self.runtime.start()
        await asyncio.sleep(0.01)
        with self.assertRaises(TypeError):
            await self.runtime.trigger_now("a")
        self.assertEqual(self.store.run_logs[-1]["source"], "manual")
        self.store.tasks["a"]["triggers"][0]["last_fired"] -= 120
        self.runtime.wake_all()
        await asyncio.sleep(0.02)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.runtime.status("a")["state"], "faulted")

    async def test_success_time_uses_completion_time(self):
        self.store.tasks["a"] = task()
        gate = self.executor.blocked["a"] = asyncio.Event()
        self.runtime.start()
        await until(lambda: len(self.executor.calls) == 1)
        completed_after = time.time()
        gate.set()
        await until(lambda: self.store.tasks["a"]["last_success_time"] > 0)
        self.assertGreaterEqual(self.store.tasks["a"]["last_success_time"], completed_after)


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TestDirectory()
        assert Path(self.directory.name).resolve().is_relative_to((ROOT / "tests").resolve())
        self.addCleanup(self.directory.cleanup)

    async def test_multi_target_migration_is_disabled_and_idempotent(self):
        multi = task()
        multi.pop("target")
        multi["targets"] = ["first", "second"]
        single = task("b")
        single.pop("target")
        single["targets"] = ["only"]
        path = Path(self.directory.name) / "tasks.json"
        path.write_text(json.dumps({"tasks": {"a": multi, "b": single}}), encoding="utf-8")
        store = storage_module.TaskStore(self.directory.name, {}, core)
        self.assertEqual(store.tasks["a"]["target"], "first")
        self.assertFalse(store.tasks["a"]["enabled"])
        self.assertNotIn("targets", store.tasks["a"])
        self.assertTrue(store.tasks["b"]["enabled"])
        await store.save()
        reloaded = storage_module.TaskStore(self.directory.name, {}, core)
        self.assertEqual(reloaded.tasks, store.tasks)

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

    async def test_saving_migrated_task_clears_notice(self):
        old = task(due=False)
        old.update(enabled=False, migration_notice="需要重新配置")
        self.plugin.store.tasks["a"] = old
        self.request.body = task(due=False)
        result = await self.plugin._api_upsert_task()
        self.assertEqual(result["status"], "success")
        self.assertNotIn("migration_notice", self.plugin.store.tasks["a"])
        self.assertTrue(self.plugin.store.tasks["a"]["enabled"])


if __name__ == "__main__":
    unittest.main()
