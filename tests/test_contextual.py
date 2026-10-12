"""Behavioral regressions for contextual conditions, expiry, and delivery history."""
import asyncio
import copy
import json
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_scheduler import (core, executor_module, runtime_module, storage_module,
                            main_module, task, TestDirectory, until)


def contextual(tid=4, **config):
    return {"id": tid, "type": "contextual", "config": {"requirement": "用户最近在讨论学习", **config}}


class ConditionTests(unittest.TestCase):
    def test_cooldown_short_circuits_random_even_when_input_order_differs(self):
        definition = task()
        triggers = definition["triggers"] + [contextual(),
            {"id": 2, "type": "random", "config": {"threshold": 0.5}},
            {"id": 3, "type": "cooldown", "config": {"hours": 1}}]
        fire_at = core.future_fires(triggers[0], time.time(), 1)[0]
        with patch.object(core._random, "random", side_effect=AssertionError("should not sample")):
            result = core.evaluate_task(triggers, fire_at, fire_at - 100, fire_at)
        self.assertFalse(result["should_run"])
        self.assertNotIn(2, result["trigger_tof"])
        self.assertEqual(result["trigger_details"][2]["status"], "not_checked")
        self.assertEqual(result["trigger_details"][4]["status"], "not_checked")
        self.assertEqual(result["trigger_details"][3]["elapsed_seconds"], 100)

    def test_random_sample_is_the_same_value_saved_in_log_and_overlap_runs_once(self):
        triggers = task()["triggers"]
        triggers += [{"id": 2, "type": "interval", "config": copy.deepcopy(triggers[0]["config"])},
                     {"id": 3, "type": "random", "config": {"threshold": 0.25}}]
        fire_at = core.future_fires(triggers[0], time.time(), 1)[0]
        with patch.object(core._random, "random", return_value=0.12) as sample:
            result = core.evaluate_task(triggers, fire_at, 0, fire_at)
        sample.assert_called_once()
        self.assertTrue(result["should_run"])
        self.assertEqual(result["trigger_tof"], {1: True, 2: True, 3: True})
        self.assertEqual(result["trigger_details"][3]["sample"], 0.12)

    def test_context_validation_and_preview_never_call_ai(self):
        trigger = contextual()
        self.assertTrue(core.validate_trigger(trigger)[0])
        for change in ({"requirement": ""}, {"history_count": True}, {"history_count": 201},
                       {"provider_id": []}, {"model": 2}, {"time_aware": "false"}):
            with self.subTest(change=change):
                invalid = contextual(**change)
                self.assertFalse(core.validate_trigger(invalid)[0])
        base = task()["triggers"]
        now = time.time()
        self.assertEqual(core.next_task_preview(base, now), core.next_task_preview(base + [trigger], now))

    def test_strict_json_rejects_strings_extra_fields_code_fences_and_long_reasons(self):
        self.assertEqual(executor_module._parse_context_result('{"result":false,"reason":"不符合"}')["result"], False)
        for output in ('{"result":"false","reason":"x"}', '{"result":1,"reason":"x"}',
                       '```json\n{"result":true,"reason":"x"}\n```',
                       '{"result":true,"reason":"x","extra":1}',
                       '{"result":true,"result":false,"reason":"x"}',
                       '{"result":true,"reason":""}', json.dumps({"result": True, "reason": "字" * 101})):
            with self.subTest(output=output), self.assertRaises(ValueError):
                executor_module._parse_context_result(output)

    def test_prompt_inserts_values_once_and_switches_are_independent(self):
        executor = executor_module.TaskExecutor(None, {}, core, None)
        cfg = {"requirement": "保留 {{time}} {{context_messages}}", "time_aware": False}
        prompt = executor._wrap_context_prompt(cfg, [{"content": "{{contextual_requirement}}"}], "，今天是工作日", time.time())
        self.assertNotIn("现在时间是", prompt)
        self.assertIn("今天是工作日", prompt)
        self.assertIn(cfg["requirement"], prompt)
        self.assertIn("{{contextual_requirement}}", prompt)
        cfg["time_aware"] = True
        prompt = executor._wrap_context_prompt(cfg, [], "", time.time())
        self.assertIn("现在时间是", prompt)
        self.assertNotIn("今天是", prompt)


class ContextExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.logs = []
        async def record(entry):
            self.logs.append(entry)
        self.cm = types.SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="cid"),
            get_conversation=AsyncMock(return_value=types.SimpleNamespace(history='[{"role":"user","content":"学习"}]')),
            update_conversation=AsyncMock(), new_conversation=AsyncMock(return_value="cid"),
        )
        self.context = types.SimpleNamespace(
            conversation_manager=self.cm,
            get_current_chat_provider_id=AsyncMock(return_value="chat-default"),
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text='{"result":true,"reason":"符合"}')),
            send_message=AsyncMock(), get_config=lambda **kwargs: {},
        )
        self.executor = executor_module.TaskExecutor(self.context, {}, core, types.SimpleNamespace(append_llm_log=record))

    async def test_context_only_reads_history_and_overrides_model_per_request(self):
        result = await self.executor.check_context(task(), contextual(provider_id="judge", model="small"), time.time(), "run")
        self.assertTrue(result["result"])
        self.assertEqual(result["history_source"], "conversation_history")
        kwargs = self.context.llm_generate.call_args.kwargs
        self.assertEqual(kwargs["chat_provider_id"], "judge")
        self.assertEqual(kwargs["model"], "small")
        self.assertNotIn("contexts", kwargs)
        self.context.send_message.assert_not_called()
        self.cm.update_conversation.assert_not_called()
        self.context.get_current_chat_provider_id.assert_not_called()
        self.assertEqual(self.logs[0]["mode"], "contextual")
        self.assertEqual(self.logs[0]["run_id"], "run")

    async def test_platform_messages_are_preferred_with_correct_scope(self):
        rows = [types.SimpleNamespace(content={"type": "user", "message": [{"type": "plain", "text": "学习"}]},
                                      sender_name="用户", created_at="today")]
        hm = types.SimpleNamespace(get=AsyncMock(return_value=rows))
        self.context.message_history_manager = hm
        result = await self.executor.check_context(task(), contextual(history_count=3), time.time())
        hm.get.assert_awaited_once_with(platform_id="test", user_id=task()["target"], page=1, page_size=3)
        self.assertEqual(result["history_source"], "platform_history")
        self.cm.get_conversation.assert_not_called()

    async def test_empty_history_skips_ai_and_records_error(self):
        self.cm.get_conversation.return_value.history = "[]"
        result = await self.executor.check_context(task(), contextual(), time.time())
        self.assertEqual(result["status"], "error")
        self.context.llm_generate.assert_not_called()
        self.assertFalse(self.logs[0]["ok"])

    async def test_rejection_invalid_output_and_timeout_do_not_send(self):
        for output, expected in (("{\"result\":false,\"reason\":\"不符合\"}", "rejected"), ("hello", "error")):
            self.context.llm_generate.return_value.completion_text = output
            result = await self.executor.check_context(task(), contextual(), time.time())
            self.assertEqual(result["status"], expected)
            self.assertFalse(result["result"])
        self.context.llm_generate.side_effect = asyncio.TimeoutError()
        result = await self.executor.check_context(task(), contextual(), time.time())
        self.assertEqual(result["status"], "error")
        self.context.send_message.assert_not_called()
        self.cm.update_conversation.assert_not_called()

    async def test_holiday_failure_does_not_prevent_judgment(self):
        self.executor._holiday_clause = AsyncMock(return_value="")
        result = await self.executor.check_context(task(), contextual(time_aware=False, holiday_aware=True), time.time())
        self.assertTrue(result["result"])
        self.executor._holiday_clause.assert_awaited_once()
        prompt = self.context.llm_generate.call_args.kwargs["prompt"]
        self.assertNotIn("现在时间是", prompt)
        self.assertNotIn("今天是", prompt)

    async def test_all_modes_obey_history_switch_and_append_only_actual_reply(self):
        for mode in ("fixed", "standalone", "conversation"):
            for record in (False, True):
                with self.subTest(mode=mode, record=record):
                    self.cm.update_conversation.reset_mock()
                    definition = task()
                    definition["content"].update(mode=mode, record_history=record)
                    result = await self.executor.execute(definition, time.time())
                    self.assertTrue(result[3])
                    self.assertEqual(result[1]["history_status"], "succeeded" if record else "disabled")
                    if record:
                        self.assertEqual(self.cm.update_conversation.call_args.kwargs["history"][-1]["role"], "assistant")
                        self.assertEqual(len(self.cm.update_conversation.call_args.kwargs["history"]), 2)
                    else:
                        self.cm.update_conversation.assert_not_called()

    async def test_successful_send_with_history_failure_does_not_fail_delivery(self):
        self.cm.update_conversation.side_effect = RuntimeError("disk")
        result = await self.executor.execute(task(), time.time())
        self.assertTrue(result[3])
        self.assertEqual(result[1]["send_status"], "succeeded")
        self.assertEqual(result[1]["history_status"], "failed")
        self.context.send_message.assert_awaited_once()

    async def test_history_timeout_after_delivery_still_counts_as_success(self):
        original_wait_for = asyncio.wait_for
        async def short_execution_budget(awaitable, timeout):
            return await original_wait_for(awaitable, timeout=0.02 if timeout == 100 else timeout)
        async def slow_write(*args, **kwargs):
            await asyncio.sleep(0.1)
        self.cm.update_conversation.side_effect = slow_write
        with patch.object(executor_module.asyncio, "wait_for", side_effect=short_execution_budget):
            result = await self.executor.execute(task(), time.time())
        self.assertTrue(result[3])
        self.assertEqual(result[1]["send_status"], "succeeded")
        self.assertEqual(result[1]["history_status"], "unknown")
        self.context.send_message.assert_awaited_once()

    async def test_failed_send_never_appends_history(self):
        self.context.send_message.side_effect = asyncio.TimeoutError()
        result = await self.executor.execute(task(), time.time())
        self.assertEqual(result[1]["send_status"], "unknown")
        self.cm.update_conversation.assert_not_called()

    async def test_missing_conversation_is_created_after_delivery_and_corrupt_history_preserved(self):
        self.cm.get_curr_conversation_id.return_value = None
        result = await self.executor.execute(task(), time.time())
        self.assertTrue(result[3])
        self.cm.new_conversation.assert_awaited_once()
        self.cm.update_conversation.reset_mock()
        self.cm.get_conversation.return_value.history = "invalid"
        result = await self.executor.execute(task(), time.time())
        self.assertTrue(result[3])
        self.assertEqual(result[1]["history_status"], "failed")
        self.cm.update_conversation.assert_not_called()

    async def test_expiry_during_generation_prevents_send(self):
        definition = task()
        definition["content"]["mode"] = "standalone"
        definition["expires_at"] = time.time() + 0.02
        async def generate(**kwargs):
            await asyncio.sleep(0.04)
            return types.SimpleNamespace(completion_text="reply")
        self.context.llm_generate.side_effect = generate
        result = await self.executor.execute(definition, time.time())
        self.assertEqual(result[0], "expired")
        self.context.send_message.assert_not_called()


class NewRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TestDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = storage_module.TaskStore(self.directory.name, {}, core)
        self.executor = types.SimpleNamespace(
            execute=AsyncMock(return_value=("send_fixed", {"ok": True}, "成功", True)),
            check_context=AsyncMock(return_value={"type": "contextual", "status": "passed", "result": True, "reason": "符合"}),
        )
        self.runtime = runtime_module.SchedulerRuntime(core, self.store, self.executor, Mock())
        self.addAsyncCleanup(self.runtime.close)

    async def test_expired_at_startup_is_disabled_once_and_survives_restart(self):
        self.store.tasks["a"] = dict(task(), expires_at=time.time() - 1)
        await self.runtime.start()
        await until(lambda: not self.store.tasks["a"]["enabled"])
        await until(lambda: len(self.store.run_logs) == 1)
        await self.runtime.close()
        restored = storage_module.TaskStore(self.directory.name, {}, core)
        self.assertFalse(restored.tasks["a"]["enabled"])
        self.assertEqual(restored.run_logs[0]["action"], "expired")
        self.executor.execute.assert_not_called()

    async def test_wait_wakes_for_expiry_before_next_trigger_and_manual_cannot_bypass(self):
        self.store.tasks["a"] = dict(task(), expires_at=time.time() + 0.08)
        await self.runtime.start()
        await until(lambda: not self.store.tasks["a"]["enabled"])
        await until(lambda: not self.runtime.lock("a").locked())
        with self.assertRaisesRegex(ValueError, "有效期"):
            await self.runtime.trigger_now("a")
        self.executor.execute.assert_not_called()

    async def test_invalid_expiry_faults_once_and_does_not_flood_logs(self):
        self.store.tasks["a"] = dict(task(), expires_at="invalid")
        await self.runtime.start()
        await until(lambda: self.runtime.status("a")["state"] == "faulted")
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.store.run_logs), 1)
        self.executor.execute.assert_not_called()

    async def test_context_rejection_records_snapshot_and_short_circuits_later_judgments(self):
        definition = task()
        definition["triggers"] += [contextual(2), contextual(3)]
        self.store.tasks["a"] = definition
        self.executor.check_context.return_value = {"type": "contextual", "status": "rejected", "result": False, "reason": "不符合"}
        fire = core.future_fires(definition["triggers"][0], time.time(), 1)[0]
        with patch.object(runtime_module.time, "time", return_value=fire):
            await self.runtime._check("a", definition, fire)
        self.executor.check_context.assert_awaited_once()
        self.executor.execute.assert_not_called()
        log = self.store.run_logs[-1]
        self.assertEqual(log["action"], "skipped")
        self.assertEqual(log["trigger_details"][3]["status"], "not_checked")
        self.assertNotIn(3, log["trigger_tof"])
        definition["name"] = "edited"
        self.assertEqual(log["task_snapshot"]["name"], "a")

    async def test_context_and_generation_share_run_id_and_noop_retains_cooldown(self):
        definition = task()
        definition["triggers"].append(contextual())
        self.store.tasks["a"] = definition
        fire = core.future_fires(definition["triggers"][0], time.time(), 1)[0]
        with patch.object(runtime_module.time, "time", return_value=fire):
            await self.runtime._check("a", definition, fire)
        run_id = self.store.run_logs[-1]["run_id"]
        self.assertEqual(self.executor.execute.call_args.args[0]["_run_id"], run_id)
        self.assertEqual(self.executor.check_context.call_args.args[3], run_id)
        self.assertTrue(self.store.run_logs[-1]["trigger_tof"][4])
        self.assertEqual(definition["last_success_time"], fire)


if __name__ == "__main__":
    unittest.main()
