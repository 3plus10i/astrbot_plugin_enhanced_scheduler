"""Exercise the scheduler's Agent boundary without a running AstrBot installation."""
import asyncio
import json
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_scheduler import core, executor_module, task


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.logs = []
        self.weather = types.SimpleNamespace(name="weather", active=True)
        self.context = types.SimpleNamespace(
            get_config=lambda **_: {"plugin_set": ["weather-plugin"], "provider_settings": {}},
            get_platform_inst=lambda _: types.SimpleNamespace(meta=lambda: types.SimpleNamespace(name="aiocqhttp")),
            get_provider_by_id=lambda _: object(),
            get_current_chat_provider_id=AsyncMock(return_value="chat"),
            llm_generate=AsyncMock(), send_message=AsyncMock(),
            conversation_manager=types.SimpleNamespace(
                get_curr_conversation_id=AsyncMock(return_value="cid"),
                get_conversation=AsyncMock(return_value=types.SimpleNamespace(
                    history=json.dumps([{"role": "user", "content": "history"}]), persona_id="p")),
                update_conversation=AsyncMock()),
        )
        self.executor = executor_module.TaskExecutor(self.context, {}, core,
            types.SimpleNamespace(append_llm_log=AsyncMock(side_effect=self.logs.append)))
        self.output, self.failure, self.seen = "周末有雨", None, None
        owner = self

        class Event:
            def __init__(self, **kw):
                self.unified_msg_origin = kw["session"].umo
                self.session = kw["session"]
                self.platform_meta = types.SimpleNamespace(name="cron")
                self.message_obj = types.SimpleNamespace(group_id="")
                self.extras = {}

            def get_extra(self, key, default=None):
                return self.extras.get(key, default)

            def set_extra(self, key, value):
                self.extras[key] = value

        class Session:
            @staticmethod
            def from_str(umo):
                pid, kind, sid = umo.split(":", 2)
                return types.SimpleNamespace(umo=umo, platform_id=pid, session_id=sid,
                                             message_type=types.SimpleNamespace(value=kind))

        async def ensure(req, cfg, context, event):
            req.system_prompt += " PERSONA SKILLS"
            req.contexts.insert(0, {"role": "assistant", "content": "opening"})
            req.func_tool = [owner.weather]

        async def search(event, req, context):
            req.func_tool.append(types.SimpleNamespace(name="web_search"))

        self.filter = Mock(side_effect=lambda event, req: None)
        self.ensure = AsyncMock(side_effect=ensure)
        self.agent = types.ModuleType("astrbot.core.astr_main_agent")
        self.agent._ensure_persona_and_skills = self.ensure
        self.agent._apply_web_search_tools = AsyncMock(side_effect=search)
        self.agent._plugin_tool_fix = self.filter

        class Runner:
            async def reset(self, **kwargs):
                self.kwargs = kwargs
                owner.seen = kwargs

            async def step_until_done(self, limit):
                kw = self.kwargs
                await kw["agent_hooks"].on_tool_start(kw["run_context"], owner.weather, {"city": "北京"})
                if owner.failure:
                    raise owner.failure
                await kw["agent_hooks"].on_tool_end(kw["run_context"], owner.weather,
                                                  {"city": "北京"}, {"forecast": "rain"})
                yield None

            def get_final_llm_resp(self):
                return types.SimpleNamespace(completion_text=owner.output)

        modules = {
            "astrbot.core": {"astr_main_agent": self.agent},
            "astrbot.core.astr_main_agent": self.agent,
            "astrbot.core.cron.events": {"CronMessageEvent": Event},
            "astrbot.core.platform.message_session": {"MessageSession": Session},
            "astrbot.core.agent.hooks": {"BaseAgentRunHooks": type("Hooks", (), {})},
            "astrbot.core.agent.runners.tool_loop_agent_runner": {"ToolLoopAgentRunner": Runner},
            "astrbot.core.astr_agent_context": {"AstrAgentContext": types.SimpleNamespace,
                                                "AgentContextWrapper": types.SimpleNamespace},
            "astrbot.core.astr_agent_tool_exec": {"FunctionToolExecutor": object},
        }
        for name, values in list(modules.items()):
            if isinstance(values, dict):
                module = types.ModuleType(name)
                module.__dict__.update(values)
                modules[name] = module
        self.patch = patch.dict(sys.modules, modules)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def execute(self, mode="conversation"):
        definition = task()
        definition["target"] = "adapter-1:GroupMessage:123"
        definition["content"].update(mode=mode, text="查询周末天气", system_prompt="TASK")
        return await self.executor.execute(definition, time.time())

    async def test_conversation_keeps_history_persona_tools_and_audit(self):
        self.assertTrue((await self.execute())[3])
        request = self.seen["request"]
        self.assertIn("PERSONA", request.system_prompt)
        self.assertIn("history", str(request.contexts))
        event = self.seen["run_context"].context.event
        self.assertEqual(event.plugins_name, ["weather-plugin"])
        self.assertEqual(event.message_obj.group_id, "123")
        self.assertEqual(event.platform_meta.name, "aiocqhttp")
        self.filter.assert_called_once()
        self.assertEqual(self.logs[-1]["request"]["tools"], ["weather", "web_search"])
        self.assertEqual(self.logs[-1]["request"]["tool_calls"][0]["result"], {"forecast": "rain"})
        self.context.llm_generate.assert_not_called()

    async def test_standalone_keeps_task_prompt_without_persona_or_history(self):
        self.assertTrue((await self.execute("standalone"))[3])
        req = self.seen["request"]
        self.assertTrue(req.system_prompt.startswith("TASK"))
        self.assertNotIn("PERSONA", req.system_prompt)
        self.assertEqual(req.contexts, [])
        self.assertEqual(len(req.func_tool), 2)

    async def test_explicit_silence_succeeds_without_delivery_or_history(self):
        self.output = executor_module.NO_MESSAGE
        result = await self.execute()
        self.assertEqual(result[0], "noop")
        self.assertTrue(result[3])
        self.context.send_message.assert_not_called()
        self.context.conversation_manager.update_conversation.assert_not_called()

    async def test_tool_failure_and_timeout_do_not_send_or_fallback(self):
        for error in (RuntimeError("search unavailable"), asyncio.TimeoutError()):
            self.failure = error
            result = await self.execute()
            self.assertFalse(result[3])
            self.assertFalse(self.logs[-1]["ok"])
            self.assertEqual(self.logs[-1]["request"]["tool_calls"][0]["status"], "started")
        self.context.send_message.assert_not_called()
        self.context.llm_generate.assert_not_called()

    async def test_empty_reply_is_failure_and_fixed_sentinel_is_literal(self):
        self.output = ""
        self.assertFalse((await self.execute())[3])
        definition = task()
        definition["content"]["text"] = executor_module.NO_MESSAGE
        self.assertTrue((await self.executor.execute(definition, time.time()))[3])
        self.assertEqual(self.context.send_message.call_args.args[1].text, executor_module.NO_MESSAGE)

    async def test_hook_model_and_context_fields_reach_runner(self):
        async def inject(event, req):
            req.model = "hook-model"
            req.extra_user_content_parts = ["memory"]
        self.executor._broadcast_on_llm_request = inject
        await self.execute()
        self.assertEqual(self.seen["request"].model, "hook-model")
        self.assertEqual(self.seen["request"].extra_user_content_parts, ["memory"])

    async def test_preparation_failure_does_not_fallback_to_text_generation(self):
        self.ensure.side_effect = ImportError("incompatible AstrBot API")
        for mode in ("conversation", "standalone"):
            self.assertFalse((await self.execute(mode))[3])
        self.context.llm_generate.assert_not_called()
        self.context.send_message.assert_not_called()

    async def test_hook_added_tools_are_filtered_before_execution(self):
        async def inject(event, req):
            req.func_tool.append(types.SimpleNamespace(name="disabled_plugin_tool"))
        self.executor._broadcast_on_llm_request = inject
        def filter_tools(event, req):
            self.assertEqual(event.plugins_name, ["weather-plugin"])
            self.assertIn("disabled_plugin_tool", [tool.name for tool in req.func_tool])
            req.func_tool = [tool for tool in req.func_tool if tool.name != "disabled_plugin_tool"]
        self.filter.side_effect = filter_tools
        await self.execute()
        self.assertNotIn("disabled_plugin_tool", self.logs[-1]["request"]["tools"])
