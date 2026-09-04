"""
main.py — 增强计划任务插件（astrbot_plugin_enhanced_scheduler）

未来计划任务调度器。支持周期/cron/区间/随机/冷却五种触发器，通过逻辑表达式组合触发器组，可为同一事件绑定多个计时器。任务内容可为固定文本、AI 提示词或空动作。
核心调度逻辑见 scheduler_core.py；本文件负责 AstrBot 集成：持久化、轮询、执行、Web API。

适用平台：OneBot v11（aiocqhttp / napcat）
适用版本：AstrBot >= 4.27.0
"""

import os
import json
import time
import uuid
import asyncio
from typing import Any, Dict, List, Optional

from astrbot.api import logger
from astrbot.api.all import *  # noqa: F401,F403  提供 Star / Context / register / MessageChain 等
from astrbot.api.event import filter, AstrMessageEvent, MessageChain  # noqa: F401
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Star, Context, register, StarTools

# AstrBot 内部管线工具（对话 AI 模式用于正确构建请求并广播 on_llm_request 钩子）
# 这些是框架内部路径，跨版本可能变动；导入失败时降级为「不广播钩子」而非崩溃。
try:
    from astrbot.core.pipeline.context_utils import call_event_hook as _call_event_hook
    from astrbot.core.star.star_handler import EventType as _EventType
except Exception:  # noqa: BLE001
    _call_event_hook = None
    _EventType = None

# 确保插件目录在 sys.path 中，以便导入同目录的 scheduler_core
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

import scheduler_core as core


PLUGIN_NAME = "astrbot_plugin_enhanced_scheduler"
DATA_FILE_NAME = "tasks.json"
LOG_FILE_NAME = "scheduler.log"
LLM_LOG_FILE_NAME = "llm_calls.jsonl"

# 「独立 AI 回复」模式的默认 system prompt（每个任务可覆盖，新建任务预填此值）
DEFAULT_STANDALONE_SYSTEM_PROMPT = "<proactive_trigger>这是由计划任务自动触发的一次会话，现在时间是 {{time}}。请根据用户的指令进行回复。你的回复将被直接发送给用户。</proactive_trigger>";


@register(
    PLUGIN_NAME,
    "3plus10i",
    "未来任务调度器，支持多触发器组合与逻辑规则。",
    "1.0.0",
)
class EnhancedSchedulerPlugin(Star):
    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        self.config = config or {}

        # 持久化目录（AstrBot 官方推荐：data 下，非插件自身目录）
        self.data_dir = str(StarTools.get_data_dir(PLUGIN_NAME))
        os.makedirs(self.data_dir, exist_ok=True)
        self.data_file = os.path.join(self.data_dir, DATA_FILE_NAME)

        # LLM 调用日志（独立文件，append-only JSONL）
        self.llm_log_file = os.path.join(self.data_dir, LLM_LOG_FILE_NAME)
        self._llm_log_count = 0
        try:
            if os.path.exists(self.llm_log_file):
                with open(self.llm_log_file, "r", encoding="utf-8") as f:
                    self._llm_log_count = sum(1 for _ in f)
        except Exception:
            self._llm_log_count = 0

        # 并发安全锁
        self._lock = asyncio.Lock()

        # 运行期数据
        self.data: Dict[str, Any] = {"tasks": {}, "logs": []}
        self._load_data_sync()

        # 后台轮询任务句柄
        self._poll_task: Optional[asyncio.Task] = None

        # 注册前端 Web API
        self._register_web_apis()

    # ─────────────────────────────────────────────────────────────
    # 生命周期
    # ─────────────────────────────────────────────────────────────

    async def initialize(self):
        """异步初始化：启动自适应调度循环。"""
        self._wake_event = asyncio.Event()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("[EnhancedScheduler] 已启动（自适应调度：按最近触发点唤醒）")

    async def terminate(self):
        """插件卸载时安全取消后台任务。"""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("[EnhancedScheduler] 已停止")

    def on_config_update(self, config: dict):
        """配置热更新。"""
        if config and config is not self.config:
            for k, v in config.items():
                self.config[k] = v
        self._wake()

    # ─────────────────────────────────────────────────────────────
    # 配置读取辅助
    # ─────────────────────────────────────────────────────────────

    def _poll_interval(self) -> int:
        try:
            v = int(self.config.get("poll_interval", 60))
            return max(2, v)
        except (TypeError, ValueError):
            return 60

    def _log_retention(self) -> int:
        try:
            v = int(self.config.get("log_retention", 200))
            return max(10, v)
        except (TypeError, ValueError):
            return 200

    def _llm_timeout(self) -> int:
        try:
            v = int(self.config.get("llm_timeout", 60))
            return max(5, v)
        except (TypeError, ValueError):
            return 60

    def _llm_log_retention(self) -> int:
        try:
            v = int(self.config.get("llm_log_retention", 500))
            return max(50, v)
        except (TypeError, ValueError):
            return 500

    def _standalone_prompt(self, content: dict) -> str:
        """解析独立 AI 模式的 system prompt（任务级，空则回退默认）。"""
        raw = content.get("system_prompt", "") if isinstance(content, dict) else ""
        if isinstance(raw, str) and raw.strip():
            return core.render_template(raw, {}, time.time())
        return DEFAULT_STANDALONE_SYSTEM_PROMPT

    # ─────────────────────────────────────────────────────────────
    # 持久化
    # ─────────────────────────────────────────────────────────────

    def _load_data_sync(self):
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.data = loaded
            except Exception as e:
                logger.error(f"[EnhancedScheduler] 读取数据失败，使用空数据: {e}")
        self.data.setdefault("tasks", {})
        self.data.setdefault("logs", [])
        # 数据迁移：旧版 use_llm(bool) -> mode(fixed/standalone/conversation)
        for task in self.data.get("tasks", {}).values():
            if not isinstance(task, dict):
                continue
            content = task.get("content")
            if isinstance(content, dict):
                content["mode"] = _resolve_mode(content)
                content.pop("use_llm", None)
        # 数据迁移：旧版 interval 触发器无 base_time -> 补为创建日 0 点
        for task in self.data.get("tasks", {}).values():
            if not isinstance(task, dict):
                continue
            created = float(task.get("created_at", 0.0) or 0.0)
            ref_ts = created if created > 0 else time.time()
            for tg in task.get("triggers", []):
                if not isinstance(tg, dict):
                    continue
                if tg.get("type") == "interval":
                    cfg = tg.get("config")
                    if isinstance(cfg, dict) and not str(cfg.get("base_time", "")).strip():
                        cfg["base_time"] = core.day_start_iso(ref_ts)

    async def _save_data(self):
        async with self._lock:
            await asyncio.to_thread(self._save_data_io)

    def _save_data_io(self):
        try:
            tmp = self.data_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.data_file)
        except Exception as e:
            logger.error(f"[EnhancedScheduler] 保存数据失败: {e}")

    # ─────────────────────────────────────────────────────────────
    # 后台轮询
    # ─────────────────────────────────────────────────────────────

    async def _poll_loop(self):
        try:
            while True:
                delay = self._compute_next_delay()
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                self._wake_event.clear()
                try:
                    await self._tick()
                except Exception as e:
                    logger.error(f"[EnhancedScheduler] 调度异常: {e}")
        except asyncio.CancelledError:
            pass

    def _compute_next_delay(self) -> float:
        """计算距离下一个触发点需要 sleep 的秒数。"""
        now = time.time()
        try:
            tasks = self.data.get("tasks", {})
            if core.has_pending_fire(tasks, now):
                return 0.0  # 有过期触发点待处理，立即 tick
            nf = core.next_fire_over_all(tasks, now)
        except Exception:
            nf = None
        if nf is None:
            # 无主动触发器或计算失败：兜底周期（任务/配置变更会通过 _wake 立即唤醒）
            return float(self._poll_interval())
        delay = nf - now + 0.05  # 加微小缓冲，确保醒来时已过触发点
        return max(0.0, min(delay, 3600.0))  # 上限 1 小时，防时钟异常导致长时间阻塞

    def _wake(self):
        """唤醒调度循环（任务/配置变更后调用）。"""
        ev = getattr(self, "_wake_event", None)
        if ev is not None:
            ev.set()

    async def _tick(self):
        now = time.time()
        tasks = self.data.get("tasks", {})
        changed = False

        for task_id, task in list(tasks.items()):
            if not task.get("enabled", True):
                continue

            triggers = task.get("triggers", [])
            logic_expr = task.get("logic_expr", "")
            last_success = float(task.get("last_success_time", 0.0) or 0.0)

            res = core.evaluate_task(triggers, logic_expr, now, last_success)

            if not res["has_active_fire"]:
                continue

            # 更新到点主动触发器的 last_fired（无论逻辑是否通过，到点即推进，避免重复判定）
            fired_points = res.get("fired_points", {})
            if fired_points:
                for tg in triggers:
                    fid = tg.get("id")
                    if fid in fired_points:
                        tg["last_fired"] = fired_points[fid]
                task["updated_at"] = now
                changed = True

            should_run = res["should_run"]
            tof_snapshot = dict(res.get("trigger_tof", {}))

            if not should_run:
                # 逻辑未通过：未执行动作，视为 skip，不推进冷却计时器
                self._append_log({
                    "time": now,
                    "task_id": task_id,
                    "task_name": task.get("name", ""),
                    "trigger_tof": tof_snapshot,
                    "logic_result": False,
                    "action": "skipped",
                    "targets_result": [],
                    "detail": "跳过（逻辑规则未通过，未执行动作）",
                })
                changed = True
                continue

            # 执行任务；success 表示"成功执行了动作"（含空动作视为成功）
            action, targets_result, detail, success = await self._execute_task(task, now, res.get("trigger_desc"))

            # 只有成功执行才更新 last_success_time（冷却型依赖此）；
            # skip（逻辑未通过）与发送失败都不推进冷却
            if success:
                task["last_success_time"] = now
                changed = True

            self._append_log({
                "time": now,
                "task_id": task_id,
                "task_name": task.get("name", ""),
                "trigger_tof": tof_snapshot,
                "logic_result": True,
                "action": action,
                "targets_result": targets_result,
                "detail": detail,
            })
            changed = True

        if changed:
            await self._save_data()

    # ─────────────────────────────────────────────────────────────
    # 任务执行
    # ─────────────────────────────────────────────────────────────

    async def _execute_task(self, task: dict, now: float, trigger_desc: Optional[dict] = None, source: str = "scheduled"):
        """
        执行任务内容并发送。返回 (action, targets_result, detail, success)。
        action: "send_fixed" | "send_llm" | "noop" | "partial" | "failed"
        targets_result: [{"umo":..., "ok":bool, "error":str}, ...]
        success: 是否"成功执行了动作"——空动作(内容留空)视为成功；发送时**所有目标都成功**才为 True，
                 任一部分失败即视为失败（不静默降级为直接发送）。
        trigger_desc: {id: str} 各触发器结果描述（用于模板变量 {{id}}）；
                      自动触发由 evaluate_task 提供，手动触发时缺省则由本方法兜底生成。
        source: "scheduled"（自动）或 "manual"（手动触发），用于 LLM 日志标注。
        """
        content = task.get("content", {})
        text = str(content.get("text", "") or "")
        mode = _resolve_mode(content)
        targets = task.get("targets", []) or []

        log_ctx = {
            "task_id": str(task.get("id", "")),
            "task_name": str(task.get("name", "")),
            "source": source,
        }

        # 空动作（内容留空）：视为成功执行了目标动作（推进冷却），仅记日志不发送
        if not text.strip():
            return "noop", [], "空动作（内容留空，成功执行，仅记日志）", True

        if not targets:
            return "noop", [], "无发送对象，跳过", False

        # 触发器结果描述（{{id}} 模板变量）：自动触发由 evaluate_task 提供；手动触发兜底生成
        if trigger_desc is None:
            trigger_desc = self._describe_triggers(task, now)
        render_params = {str(k): v for k, v in trigger_desc.items()}
        rendered = core.render_template(text, render_params, now)

        # 独立 AI：所有目标共享一次生成结果（不依赖会话人格）；
        # chat_provider_id 从第一个目标 UMO 的会话配置获取。
        standalone_text = None
        standalone_error = None
        if mode == "standalone":
            first_umo = str(targets[0]).strip()
            standalone_text, standalone_error = await self._llm_generate_standalone(
                first_umo, rendered, self._standalone_prompt(content), log_ctx
            )

        targets_result: List[Dict[str, Any]] = []
        errors: List[str] = []

        for umo in targets:
            umo_str = str(umo).strip()
            if not umo_str:
                continue
            try:
                conv_cid = None
                if mode == "fixed":
                    msg_text = rendered
                elif mode == "standalone":
                    if standalone_error:
                        raise RuntimeError(standalone_error)
                    msg_text = standalone_text
                else:  # conversation
                    msg_text, gen_err, conv_cid = await self._llm_generate_for_target(umo_str, rendered, log_ctx)
                    if gen_err:
                        raise RuntimeError(gen_err)

                if not msg_text:
                    targets_result.append({"umo": umo_str, "ok": False, "error": "生成内容为空"})
                    errors.append(f"{umo_str}: 生成内容为空")
                    continue

                msg = MessageChain().message(msg_text)
                await self.context.send_message(umo_str, msg)
                # 对话 AI 模式：把主动回复回写到会话历史，使 AI 记住自己说过的话
                if mode == "conversation" and conv_cid:
                    await self._save_assistant_reply(umo_str, conv_cid, msg_text)
                targets_result.append({"umo": umo_str, "ok": True, "error": ""})
            except Exception as e:
                targets_result.append({"umo": umo_str, "ok": False, "error": str(e)})
                errors.append(f"{umo_str}: {e}")

        total = len(targets_result)
        ok_count = sum(1 for tr in targets_result if tr.get("ok"))

        if total == 0:
            return "failed", [], "无有效发送对象", False

        if ok_count == total:
            action = "send_llm" if mode in ("standalone", "conversation") else "send_fixed"
            return action, targets_result, "成功", True

        # 部分失败 / 全部失败都视为失败（存在失败就报失败）
        if ok_count > 0:
            detail = f"部分失败（{ok_count}/{total} 成功）: " + "; ".join(errors)
            return "partial", targets_result, detail, False
        return "failed", targets_result, "全部失败: " + "; ".join(errors), False

    def _describe_triggers(self, task: dict, now: float) -> Dict[str, str]:
        """生成任务内每个触发器的结果描述（手动触发等未经过 evaluate_task 的场景兜底）。
        主动型视为未到点（手动触发绕过触发规则），被动型按当前时刻实时求值。返回 {str(id): desc}。"""
        desc: Dict[str, str] = {}
        last_success = float(task.get("last_success_time", 0.0) or 0.0)
        for tg in task.get("triggers", []):
            if not isinstance(tg, dict):
                continue
            tid = tg.get("id")
            if not isinstance(tid, int):
                continue
            if tg.get("type") in core.ACTIVE_TYPES:
                desc[str(tid)] = core.describe_active_trigger(tg, None)
            else:
                _, d = core.evaluate_passive_trigger(tg, now, last_success)
                desc[str(tid)] = d
        return desc

    async def _llm_generate_for_target(self, umo: str, user_prompt: str, log_ctx: Optional[dict] = None) -> tuple:
        """
        以目标会话 umo 的当前配置调用 LLM 生成回复。

        正确复制 AstrBot 主 agent 的请求构建流程（_decorate_llm_request 的关键部分）：
          1. 取该 UMO 的 chat_provider_id
          2. 取会话当前 conversation（历史 contexts + 会话级 persona_id）
          3. 取该 UMO 的 provider_settings（prompt_prefix / 默认人格）
          4. 经 persona_manager 解析人格，注入 system_prompt 与 begin_dialogs
          5. 广播 on_llm_request 钩子，让记忆等其它插件注入
          6. 调用 context.llm_generate
        每次调用都会把「最终发送给 AI 的完整请求体」与「AI 回复」写入 llm_calls.jsonl。

        返回 (text, error, cid)：error 为空表示成功，非空表示失败；cid 为目标会话当前
        对话 ID（供调用方在发送成功后把 assistant 回复回写到会话历史）。
        """
        t0 = time.time()
        cid: Optional[str] = None

        def _fail(msg: str) -> tuple:
            logger.error(f"[EnhancedScheduler] {msg} umo={umo}")
            return "", msg, cid

        # 1) provider id
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
        except Exception as e:
            msg = f"获取会话聊天模型失败: {e}"
            await self._log_llm_call(log_ctx, umo, "conversation", None, None, msg, t0)
            return _fail(msg)
        if not provider_id:
            msg = "会话未配置聊天模型 provider"
            await self._log_llm_call(log_ctx, umo, "conversation", None, None, msg, t0)
            return _fail(msg)

        # 2) 会话 conversation（历史 + 会话级 persona_id）
        conversation = None
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if cid:
                conversation = await cm.get_conversation(umo, cid)
        except Exception as e:
            logger.warning(f"[EnhancedScheduler] 获取会话历史失败: {e}")

        # 3) provider_settings 配置
        cfg: Dict[str, Any] = {}
        try:
            cfg = self.context.get_config(umo=umo).get("provider_settings", {}) or {}
        except Exception:
            cfg = {}

        req = ProviderRequest(prompt=user_prompt, system_prompt="", session_id=umo)
        if conversation is not None:
            req.conversation = conversation
            history = getattr(conversation, "history", None)
            if history:
                try:
                    hist = json.loads(history)
                    if isinstance(hist, list):
                        req.contexts = hist
                except Exception:
                    pass

        # 4) prompt prefix
        prefix = cfg.get("prompt_prefix")
        if prefix:
            if "{{prompt}}" in prefix:
                req.prompt = prefix.replace("{{prompt}}", req.prompt)
            else:
                req.prompt = f"{prefix}{req.prompt}"

        # 5) 人格（persona system_prompt + begin_dialogs）
        await self._apply_persona(req, cfg, umo, conversation)

        # 6) 广播 on_llm_request 钩子（其它插件注入）
        event = _MockEvent(umo)
        await self._broadcast_on_llm_request(event, req)

        # 最终请求体（广播后 system_prompt / contexts / model 可能已被插件注入）
        request_body = {
            "provider_id": provider_id,
            "model": getattr(req, "model", None),
            "system_prompt": req.system_prompt or "",
            "contexts": req.contexts or [],
            "prompt": req.prompt or user_prompt,
        }

        kwargs: Dict[str, Any] = {
            "chat_provider_id": provider_id,
            "prompt": req.prompt,
            "system_prompt": req.system_prompt or "",
        }
        if req.contexts:
            kwargs["contexts"] = req.contexts
        model = getattr(req, "model", None)
        if model:
            kwargs["model"] = model

        try:
            resp = await asyncio.wait_for(
                self.context.llm_generate(**kwargs),
                timeout=self._llm_timeout(),
            )
            if resp and getattr(resp, "completion_text", None):
                await self._log_llm_call(log_ctx, umo, "conversation", request_body, resp, None, t0)
                return str(resp.completion_text).strip(), "", cid
            await self._log_llm_call(log_ctx, umo, "conversation", request_body, None, "AI 生成结果为空", t0)
            return "", "AI 生成结果为空", cid
        except asyncio.TimeoutError:
            msg = f"AI 生成超时({self._llm_timeout()}s)"
            logger.warning(f"[EnhancedScheduler] {msg} umo={umo}")
            await self._log_llm_call(log_ctx, umo, "conversation", request_body, None, msg, t0)
            return "", msg, cid
        except Exception as e:
            msg = f"AI 生成失败: {e}"
            logger.error(f"[EnhancedScheduler] {msg} umo={umo}")
            await self._log_llm_call(log_ctx, umo, "conversation", request_body, None, msg, t0)
            return "", msg, cid

    async def _save_assistant_reply(self, umo: str, cid: str, reply_text: str) -> bool:
        """把主动回复回写到目标会话的对话历史，使对话 AI 记住自己说过的话。

        主动触发（conversation 模式）没有真实的用户消息事件，主 agent 不会代为保存
        历史，因此需要插件自行把这条 assistant 消息追加到 conversation.history。
        消息格式与主 agent 的 dump_messages_with_checkpoints 一致：
        {"role": "assistant", "content": [{"type": "text", "text": ...}]}
        """
        if not cid or not reply_text:
            return False
        try:
            cm = self.context.conversation_manager
            # 重新读取最新历史再追加，避免覆盖并发新增的消息
            conv = await cm.get_conversation(umo, cid)
            if conv is None:
                return False
            try:
                history = json.loads(conv.history) if conv.history else []
            except Exception:
                history = []
            if not isinstance(history, list):
                history = []
            history.append({"role": "assistant", "content": [{"type": "text", "text": reply_text}]})
            await cm.update_conversation(umo, cid, history=history)
            return True
        except Exception as e:
            logger.warning(f"[EnhancedScheduler] 写入会话历史失败: {e}")
            return False

    async def _apply_persona(self, req: ProviderRequest, cfg: dict, umo: str, conversation) -> None:
        """复制主 agent 的人格注入：把当前会话人格的 system prompt 与开场白写入请求。

        逻辑对齐 astr_main_agent._ensure_persona_and_skills 的核心部分（不含 skills/tools）。
        """
        try:
            persona_mgr = getattr(self.context, "persona_manager", None)
            if persona_mgr is None or not hasattr(persona_mgr, "resolve_selected_persona"):
                return
            platform_name = _platform_name_from_umo(umo)
            conv_persona_id = getattr(conversation, "persona_id", None) if conversation is not None else None
            _, persona, _, _ = await persona_mgr.resolve_selected_persona(
                umo=umo,
                conversation_persona_id=conv_persona_id,
                platform_name=platform_name,
                provider_settings=cfg,
            )
            if not persona:
                return
            if req.system_prompt is None:
                req.system_prompt = ""
            prompt = persona.get("prompt") if isinstance(persona, dict) else getattr(persona, "prompt", None)
            if prompt:
                req.system_prompt += f"\n# Persona Instructions\n\n{prompt}\n"
            begin_dialogs = persona.get("_begin_dialogs_processed") if isinstance(persona, dict) else getattr(persona, "_begin_dialogs_processed", None)
            if begin_dialogs:
                try:
                    req.contexts = list(begin_dialogs) + list(req.contexts or [])
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[EnhancedScheduler] 注入人格失败: {e}")

    async def _broadcast_on_llm_request(self, event, req: ProviderRequest):
        """广播 on_llm_request 钩子给其它已加载插件（人格/记忆等注入）。

        优先走框架原生的 call_event_hook（正确按注册表/优先级/启用状态分发）；
        若内部 API 不可用则退化为手动遍历（尽力而为）。
        """
        if _call_event_hook is not None and _EventType is not None:
            try:
                await _call_event_hook(event, _EventType.OnLLMRequestEvent, req)
                return
            except Exception as e:
                logger.debug(f"[EnhancedScheduler] call_event_hook 失败，退回手动广播: {e}")
        # 兜底：手动遍历已加载插件
        stars = []
        try:
            sm = getattr(self.context, "star_map", None)
            if isinstance(sm, dict):
                stars = list(sm.values())
        except Exception:
            pass
        if not stars:
            pm = getattr(self.context, "plugin_manager", None)
            if pm and hasattr(pm, "plugins"):
                try:
                    stars = list(pm.plugins.values())
                except Exception:
                    stars = []

        for star in stars:
            if star is self:
                continue
            try:
                handler = getattr(star, "on_llm_request", None)
                if handler is None:
                    continue
                if not _is_on_llm_request_hook(handler):
                    continue
                if asyncio.iscoroutinefunction(handler):
                    await handler(event, req)
                else:
                    handler(event, req)
            except Exception as e:
                logger.debug(f"[EnhancedScheduler] 广播 on_llm_request 跳过某插件: {e}")

    async def _llm_generate_standalone(self, umo: str, user_prompt: str, system_prompt: str, log_ctx: Optional[dict] = None) -> tuple:
        """
        独立 AI 模式：使用任务自己的 system prompt + 任务提示词单独调用 LLM。
        不广播 on_llm_request，因此不注入对话人格、也不与其它插件互动。
        但 chat_provider_id 仍需从指定 UMO 的会话配置正确获取。
        返回 (text, error)：error 为空表示成功，非空表示失败。
        每次调用都会把「最终发送给 AI 的完整请求体」与「AI 回复」写入 llm_calls.jsonl。
        """
        t0 = time.time()
        # 必须正确获取聊天模型 provider id，llm_generate 强制要求
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
        except Exception as e:
            msg = f"获取会话聊天模型失败: {e}"
            logger.error(f"[EnhancedScheduler] {msg} umo={umo}")
            await self._log_llm_call(log_ctx, umo, "standalone", None, None, msg, t0)
            return "", msg
        if not provider_id:
            msg = "会话未配置聊天模型 provider"
            logger.error(f"[EnhancedScheduler] {msg} umo={umo}")
            await self._log_llm_call(log_ctx, umo, "standalone", None, None, msg, t0)
            return "", msg

        request_body = {
            "provider_id": provider_id,
            "model": None,
            "system_prompt": system_prompt,
            "contexts": [],
            "prompt": user_prompt,
        }

        try:
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                ),
                timeout=self._llm_timeout(),
            )
            if resp and getattr(resp, "completion_text", None):
                await self._log_llm_call(log_ctx, umo, "standalone", request_body, resp, None, t0)
                return str(resp.completion_text).strip(), ""
            await self._log_llm_call(log_ctx, umo, "standalone", request_body, None, "独立 AI 生成结果为空", t0)
            return "", "独立 AI 生成结果为空"
        except asyncio.TimeoutError:
            msg = f"独立 AI 生成超时({self._llm_timeout()}s)"
            logger.warning(f"[EnhancedScheduler] {msg}")
            await self._log_llm_call(log_ctx, umo, "standalone", request_body, None, msg, t0)
            return "", msg
        except Exception as e:
            msg = f"独立 AI 生成失败: {e}"
            logger.error(f"[EnhancedScheduler] {msg}")
            await self._log_llm_call(log_ctx, umo, "standalone", request_body, None, msg, t0)
            return "", msg

    # ─────────────────────────────────────────────────────────────
    # 日志
    # ─────────────────────────────────────────────────────────────

    def _append_log(self, entry: dict):
        logs = self.data.setdefault("logs", [])
        logs.append(entry)
        # 截断保留最近 N 条
        retention = self._log_retention()
        if len(logs) > retention:
            del logs[: len(logs) - retention]

    # ─────────────────────────────────────────────────────────────
    # LLM 调用日志（独立文件，记录完整请求体与回复）
    # ─────────────────────────────────────────────────────────────

    async def _append_llm_log(self, entry: dict):
        """向 llm_calls.jsonl 追加一条记录（append-only）。"""
        async with self._lock:
            await asyncio.to_thread(self._append_llm_log_io, entry)

    def _append_llm_log_io(self, entry: dict):
        try:
            line = json.dumps(entry, ensure_ascii=False)
            with open(self.llm_log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._llm_log_count += 1
            retention = self._llm_log_retention()
            # 超过 2 倍保留量才裁剪一次，摊销重写开销
            if self._llm_log_count > retention * 2:
                self._trim_llm_log_io(retention)
        except Exception as e:
            logger.error(f"[EnhancedScheduler] 写入 LLM 日志失败: {e}")

    def _trim_llm_log_io(self, retention: int):
        try:
            with open(self.llm_log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > retention:
                tmp = self.llm_log_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.writelines(lines[-retention:])
                os.replace(tmp, self.llm_log_file)
            self._llm_log_count = min(self._llm_log_count, retention)
        except Exception as e:
            logger.error(f"[EnhancedScheduler] 裁剪 LLM 日志失败: {e}")

    def _read_llm_logs_io(self) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        try:
            if not os.path.exists(self.llm_log_file):
                return entries
            with open(self.llm_log_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        continue
        except Exception as e:
            logger.error(f"[EnhancedScheduler] 读取 LLM 日志失败: {e}")
        return entries

    def _read_llm_logs_raw_io(self) -> str:
        """按原样读取 llm_calls.jsonl 的原始文件内容（不解析、不渲染，供 debug 直接查看）。"""
        try:
            if not os.path.exists(self.llm_log_file):
                return ""
            with open(self.llm_log_file, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
            lines.reverse()
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"[EnhancedScheduler] 读取 LLM 原始日志失败: {e}")
            return ""

    def _clear_llm_logs_io(self):
        try:
            with open(self.llm_log_file, "w", encoding="utf-8") as f:
                pass
            self._llm_log_count = 0
        except Exception as e:
            logger.error(f"[EnhancedScheduler] 清空 LLM 日志失败: {e}")

    def _serialize_llm_response(self, resp) -> Optional[Dict[str, Any]]:
        """把 LLMResponse 转成可 JSON 序列化的字典（不含原始 raw_completion）。"""
        if resp is None:
            return None
        body: Dict[str, Any] = {
            "role": getattr(resp, "role", "") or "",
            "completion_text": getattr(resp, "completion_text", "") or "",
            "reasoning_content": getattr(resp, "reasoning_content", "") or "",
        }
        u = getattr(resp, "usage", None)
        if u is not None:
            body["usage"] = {
                "input": int(getattr(u, "input", 0) or 0),
                "output": int(getattr(u, "output", 0) or 0),
                "total": int(getattr(u, "total", 0) or 0),
            }
        return body

    async def _log_llm_call(
        self,
        log_ctx: Optional[dict],
        umo: str,
        mode: str,
        request_body: Optional[dict],
        resp,
        error: Optional[str],
        t0: float,
    ):
        """写入一条 LLM 调用日志（成功与失败都记录）。"""
        try:
            now_ts = time.time()
            entry = {
                "ts": now_ts,
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts)),
                "task_id": (log_ctx or {}).get("task_id", ""),
                "task_name": (log_ctx or {}).get("task_name", ""),
                "source": (log_ctx or {}).get("source", "scheduled"),
                "mode": mode,
                "umo": umo,
                "ok": error is None,
                "duration_ms": int((now_ts - t0) * 1000),
                "request": _json_safe(request_body) if request_body is not None else None,
                "response": self._serialize_llm_response(resp),
                "error": error,
            }
            await self._append_llm_log(entry)
        except Exception as e:
            logger.error(f"[EnhancedScheduler] 记录 LLM 日志失败: {e}")

    # ─────────────────────────────────────────────────────────────
    # Web API 注册
    # ─────────────────────────────────────────────────────────────

    def _register_web_apis(self):
        prefix = f"/{PLUGIN_NAME}"
        self.context.register_web_api(f"{prefix}/get_data", self._api_get_data, ["GET"], "获取任务与配置")
        self.context.register_web_api(f"{prefix}/save_config", self._api_save_config, ["POST"], "保存插件配置")
        self.context.register_web_api(f"{prefix}/get_sessions", self._api_get_sessions, ["GET"], "获取活跃会话列表")
        self.context.register_web_api(f"{prefix}/upsert_task", self._api_upsert_task, ["POST"], "新增/更新任务")
        self.context.register_web_api(f"{prefix}/delete_task", self._api_delete_task, ["POST"], "删除任务")
        self.context.register_web_api(f"{prefix}/copy_task", self._api_copy_task, ["POST"], "复制任务")
        self.context.register_web_api(f"{prefix}/validate", self._api_validate, ["POST"], "校验触发器与逻辑规则")
        self.context.register_web_api(f"{prefix}/preview_next", self._api_preview_next, ["POST"], "预览下次触发时间")
        self.context.register_web_api(f"{prefix}/trigger_now", self._api_trigger_now, ["POST"], "立即手动触发一次任务")
        self.context.register_web_api(f"{prefix}/get_llm_logs", self._api_get_llm_logs, ["GET"], "获取 LLM 调用日志")
        self.context.register_web_api(f"{prefix}/get_llm_logs_raw", self._api_get_llm_logs_raw, ["GET"], "获取 LLM 调用日志原始文件内容")
        self.context.register_web_api(f"{prefix}/clear_llm_logs", self._api_clear_llm_logs, ["POST"], "清空 LLM 调用日志")

    # ─────────────────────────────────────────────────────────────
    # Web API 实现
    # ─────────────────────────────────────────────────────────────

    async def _api_get_data(self):
        from quart import jsonify
        resp = {"status": "success", "config": self.config}
        # 预计算每个任务的下次主动触发时间，供前端直接展示（无需逐行异步请求）
        now = time.time()
        tasks_out = {}
        for tid, t in self.data.get("tasks", {}).items():
            t_copy = dict(t)
            try:
                t_copy["_next_fire"] = core.next_trigger_preview(t.get("triggers", []), now)
            except Exception:
                t_copy["_next_fire"] = None
            tasks_out[tid] = t_copy
        resp["tasks"] = tasks_out
        resp["logs"] = list(self.data.get("logs", []))
        resp["default_standalone_prompt"] = DEFAULT_STANDALONE_SYSTEM_PROMPT
        return jsonify(resp)

    async def _api_save_config(self):
        from quart import request, jsonify
        try:
            data = await request.json
            if not isinstance(data, dict):
                return jsonify({"status": "error", "message": "请求体必须是 JSON 对象"}), 400
            for k, v in data.items():
                self.config[k] = v
            try:
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
            except Exception:
                pass
            self.on_config_update(self.config)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    async def _api_get_sessions(self):
        """枚举可用的会话 UMO（unified_msg_origin），供发送对象下拉选择。"""
        from quart import jsonify
        sessions: List[str] = []
        seen = set()
        try:
            cm = getattr(self.context, "conversation_manager", None)
            if cm is not None:
                # 1) 内存中已加载的活跃会话（session_conversations 键 = unified_msg_origin）
                sc = getattr(cm, "session_conversations", None)
                if isinstance(sc, dict):
                    for umo in sc.keys():
                        if umo and umo not in seen:
                            seen.add(umo)
                            sessions.append(umo)
                # 2) 数据库里所有对话的 user_id（= unified_msg_origin）
                get_convs = getattr(cm, "get_conversations", None)
                if callable(get_convs):
                    convs = await get_convs()
                    for c in convs or []:
                        uid = getattr(c, "user_id", None)
                        if uid and uid not in seen:
                            seen.add(uid)
                            sessions.append(uid)
        except Exception:
            pass
        return jsonify({"status": "success", "sessions": sessions})

    async def _api_upsert_task(self):
        from quart import request, jsonify
        try:
            req = await request.json
            if not isinstance(req, dict):
                return jsonify({"status": "error", "message": "请求体必须是 JSON 对象"}), 400
            ok, msg, task_id = self._upsert_task(req)
            if not ok:
                return jsonify({"status": "error", "message": msg}), 400
            await self._save_data()
            self._wake()
            return jsonify({"status": "success", "id": task_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    def _upsert_task(self, req: dict) -> tuple:
        """新增或更新任务。req 含 id 时为更新，否则为新增。返回 (ok, msg, task_id)。"""
        tasks = self.data.setdefault("tasks", {})
        task_id = str(req.get("id", "")).strip()

        # 字段校验
        name = str(req.get("name", "")).strip()
        if not name:
            return False, "任务名称不能为空", ""

        triggers = req.get("triggers", [])
        if not isinstance(triggers, list) or not triggers:
            return False, "至少需要一个触发器", ""
        # 规范化并校验每个触发器
        # 更新任务时继承原触发器的 last_fired（避免重置导致立即重复触发）；
        # 新建任务或新增触发器 id 时用当前时间作为 last_fired，使首次触发从未来第一个点开始。
        is_update = bool(task_id) and task_id in tasks
        existing_lf = {}
        if is_update:
            for et in tasks[task_id].get("triggers", []):
                eid = et.get("id")
                if eid is not None:
                    existing_lf[eid] = float(et.get("last_fired", 0.0) or 0.0)
        now = time.time()
        norm_triggers = []
        seen_ids = set()
        for tg in triggers:
            if not isinstance(tg, dict):
                return False, "触发器格式错误", ""
            tid = tg.get("id")
            if not isinstance(tid, int) or tid < 1:
                return False, f"触发器 id 必须是正整数（收到 {tid}）", ""
            if tid in seen_ids:
                return False, f"触发器 id {tid} 重复", ""
            seen_ids.add(tid)
            if tid in existing_lf:
                lf = existing_lf[tid]
            else:
                lf = now
            cfg = dict(tg.get("config", {}) or {})
            # interval 触发器缺省 base_time 时，默认设为创建/保存当天的 0 点
            if tg.get("type") == "interval" and not str(cfg.get("base_time", "")).strip():
                cfg["base_time"] = core.day_start_iso(now)
            norm_tg = {
                "id": tid,
                "type": tg.get("type"),
                "config": cfg,
                "last_fired": lf,
            }
            ok, msg = core.validate_trigger(norm_tg)
            if not ok:
                return False, f"触发器 {tid}: {msg}", ""
            norm_triggers.append(norm_tg)

        logic_expr = str(req.get("logic_expr", "")).strip()
        ok, msg = core.validate_logic_expr(logic_expr, [t["id"] for t in norm_triggers])
        if not ok:
            return False, f"逻辑规则: {msg}", ""

        content = req.get("content", {})
        if not isinstance(content, dict):
            return False, "任务内容格式错误", ""
        text = str(content.get("text", "") or "")
        mode = _resolve_mode(content)
        system_prompt = str(content.get("system_prompt", "") or "")

        targets = req.get("targets", [])
        if not isinstance(targets, list):
            return False, "发送对象必须是数组", ""
        targets = [str(t).strip() for t in targets if str(t).strip()]
        # 空动作（内容为空）允许 targets 为空（仅记日志）；有内容则必须有目标
        if text.strip() and not targets:
            return False, "发送对象不能为空（除非内容为空即空动作）", ""

        enabled = bool(req.get("enabled", True))

        if task_id and task_id in tasks:
            task = tasks[task_id]
            task["name"] = name
            task["enabled"] = enabled
            task["triggers"] = norm_triggers
            task["logic_expr"] = logic_expr
            task["content"] = {"text": text, "mode": mode, "system_prompt": system_prompt}
            task["targets"] = targets
            task["updated_at"] = now
            return True, "", task_id
        else:
            new_id = task_id or str(uuid.uuid4())
            tasks[new_id] = {
                "id": new_id,
                "name": name,
                "enabled": enabled,
                "triggers": norm_triggers,
                "logic_expr": logic_expr,
                "content": {"text": text, "mode": mode, "system_prompt": system_prompt},
                "targets": targets,
                "last_success_time": 0.0,
                "created_at": now,
                "updated_at": now,
            }
            return True, "", new_id

    async def _api_delete_task(self):
        from quart import request, jsonify
        try:
            req = await request.json
            if not isinstance(req, dict):
                return jsonify({"status": "error", "message": "请求体必须是 JSON 对象"}), 400
            task_id = str(req.get("id", "")).strip()
            if not task_id:
                return jsonify({"status": "error", "message": "缺少 id"}), 400
            tasks = self.data.get("tasks", {})
            if task_id not in tasks:
                return jsonify({"status": "error", "message": "任务不存在"}), 404
            tasks.pop(task_id)
            await self._save_data()
            self._wake()
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    async def _api_copy_task(self):
        from quart import request, jsonify
        try:
            req = await request.json
            if not isinstance(req, dict):
                return jsonify({"status": "error", "message": "请求体必须是 JSON 对象"}), 400
            src_id = str(req.get("id", "")).strip()
            tasks = self.data.get("tasks", {})
            if src_id not in tasks:
                return jsonify({"status": "error", "message": "源任务不存在"}), 404
            src = tasks[src_id]
            now = time.time()
            new_id = str(uuid.uuid4())
            new_task = {
                "id": new_id,
                "name": src.get("name", "") + " (副本)",
                "enabled": False,  # 复制后默认停用，避免立即双触发
                "triggers": [
                    {
                        "id": t.get("id"),
                        "type": t.get("type"),
                        "config": json.loads(json.dumps(t.get("config", {}))),  # 深拷贝
                        "last_fired": now,  # 副本从未来首个触发点开始，避免启用后立即触发
                    }
                    for t in src.get("triggers", [])
                ],
                "logic_expr": src.get("logic_expr", ""),
                "content": json.loads(json.dumps(src.get("content", {}))),
                "targets": list(src.get("targets", [])),
                "last_success_time": 0.0,
                "created_at": now,
                "updated_at": now,
            }
            tasks[new_id] = new_task
            await self._save_data()
            self._wake()
            return jsonify({"status": "success", "id": new_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    async def _api_validate(self):
        """校验触发器组与逻辑规则，供前端实时校验。"""
        from quart import request, jsonify
        try:
            req = await request.json
            if not isinstance(req, dict):
                return jsonify({"status": "error", "message": "请求体必须是 JSON 对象"}), 400
            results = {"triggers": [], "logic": None, "next_fire": None}

            triggers = req.get("triggers", []) or []
            now = time.time()
            valid_ids = []
            for tg in triggers:
                ok, msg = core.validate_trigger(tg)
                entry = {"id": tg.get("id"), "ok": ok, "msg": msg, "next_fire": None}
                if ok:
                    valid_ids.append(tg.get("id"))
                    if tg.get("type") in core.ACTIVE_TYPES:
                        # 以当前时刻为起点预览该触发器的下一个触发点（与 last_fired 无关）
                        entry["next_fire"] = core._next_fire_strictly_after(tg, now)
                results["triggers"].append(entry)

            logic_expr = str(req.get("logic_expr", "") or "")
            if logic_expr:
                ok, msg = core.validate_logic_expr(logic_expr, valid_ids)
                results["logic"] = {"ok": ok, "msg": msg}
            else:
                results["logic"] = {"ok": False, "msg": "逻辑规则为空"}

            # 下次触发预览（仅主动触发器）
            norm_triggers = []
            for tg in triggers:
                if core.validate_trigger(tg)[0]:
                    norm_triggers.append({
                        "id": tg.get("id"),
                        "type": tg.get("type"),
                        "config": tg.get("config", {}),
                        "last_fired": float(tg.get("last_fired", 0.0) or 0.0),
                    })
            nf = core.next_trigger_preview(norm_triggers, now)
            results["next_fire"] = nf
            results["now"] = now
            return jsonify({"status": "success", "results": results})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    async def _api_preview_next(self):
        """给定触发器列表，返回下次触发时间预览。"""
        from quart import request, jsonify
        try:
            req = await request.json
            if not isinstance(req, dict):
                return jsonify({"status": "error", "message": "请求体必须是 JSON 对象"}), 400
            triggers = req.get("triggers", []) or []
            norm_triggers = []
            for tg in triggers:
                if core.validate_trigger(tg)[0]:
                    norm_triggers.append({
                        "id": tg.get("id"),
                        "type": tg.get("type"),
                        "config": tg.get("config", {}),
                        "last_fired": float(tg.get("last_fired", 0.0) or 0.0),
                    })
            now = time.time()
            nf = core.next_trigger_preview(norm_triggers, now)
            return jsonify({"status": "success", "next_fire": nf, "now": now})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    async def _api_trigger_now(self):
        """立即手动触发一次任务（忽略触发规则与启用状态，仅执行内容并发送）。"""
        from quart import request, jsonify
        try:
            req = await request.json
            if not isinstance(req, dict):
                return jsonify({"status": "error", "message": "请求体必须是 JSON 对象"}), 400
            task_id = str(req.get("id", "")).strip()
            if not task_id:
                return jsonify({"status": "error", "message": "缺少 id"}), 400
            tasks = self.data.get("tasks", {})
            if task_id not in tasks:
                return jsonify({"status": "error", "message": "任务不存在"}), 404
            task = tasks[task_id]
            now = time.time()
            action, targets_result, detail, success = await self._execute_task(task, now, source="manual")
            if success:
                task["last_success_time"] = now
            self._append_log({
                "time": now,
                "task_id": task_id,
                "task_name": task.get("name", ""),
                "trigger_tof": {},
                "logic_result": None,
                "action": action,
                "targets_result": targets_result,
                "detail": "[手动触发] " + detail,
                "source": "manual",
            })
            await self._save_data()
            return jsonify({"status": "success", "action": action, "detail": detail})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    async def _api_get_llm_logs(self):
        """分页读取 llm_calls.jsonl（最新在前）。支持 query 参数 limit / offset。"""
        from quart import request, jsonify
        try:
            limit = int(request.args.get("limit", 200))
            offset = int(request.args.get("offset", 0))
        except (TypeError, ValueError):
            limit, offset = 200, 0
        limit = max(1, min(limit, 2000))
        offset = max(0, offset)
        try:
            entries = await asyncio.to_thread(self._read_llm_logs_io)
            entries.reverse()  # 最新在前
            total = len(entries)
            page = entries[offset : offset + limit]
            return jsonify({"status": "success", "total": total, "entries": page})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    async def _api_clear_llm_logs(self):
        """清空 llm_calls.jsonl。"""
        from quart import jsonify
        try:
            await asyncio.to_thread(self._clear_llm_logs_io)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    async def _api_get_llm_logs_raw(self):
        """返回 llm_calls.jsonl 的原始文件内容（不解析、不渲染，供 debug 直接查看）。"""
        from quart import jsonify
        try:
            content = await asyncio.to_thread(self._read_llm_logs_raw_io)
            return jsonify({"status": "success", "content": content})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────
# 辅助类与函数
# ─────────────────────────────────────────────────────────────────────

# 执行模式枚举
MODE_FIXED = "fixed"                # 发送文本到对话（不经过 LLM）
MODE_STANDALONE = "standalone"      # 调用独立 AI 回复（本插件 system prompt，不注入人格）
MODE_CONVERSATION = "conversation"  # 调用对话配置的 AI 回复（注入人格/记忆）
VALID_MODES = (MODE_FIXED, MODE_STANDALONE, MODE_CONVERSATION)


def _resolve_mode(content) -> str:
    """解析任务内容的执行模式。兼容旧版 use_llm(bool) 字段。"""
    if not isinstance(content, dict):
        return MODE_FIXED
    mode = content.get("mode")
    if mode in VALID_MODES:
        return mode
    if content.get("use_llm"):
        return MODE_CONVERSATION
    return MODE_FIXED


def _json_safe(obj):
    """递归地把任意对象转成 JSON 可序列化的结构（dict/list/基本类型），其余退化为字符串。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    try:
        import dataclasses

        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return _json_safe(dataclasses.asdict(obj))
    except Exception:
        pass
    # pydantic BaseModel（如 astrbot 的 Message）
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return _json_safe(dump())
        except Exception:
            pass
    d = getattr(obj, "dict", None)
    if callable(d):
        try:
            return _json_safe(d())
        except Exception:
            pass
    try:
        return str(obj)
    except Exception:
        return repr(obj)


def _platform_name_from_umo(umo: str) -> str:
    """从 unified_msg_origin（platform_name:message_type:session_id）取平台名。"""
    try:
        return str(umo).split(":")[0]
    except Exception:
        return ""


class _MockEvent:
    """轻量事件对象，供 on_llm_request 钩子读取会话信息（定时触发无真实事件）。

    尽量对齐 AstrMessageEvent 的常用接口，使 call_event_hook 与各钩子能正常读取：
    unified_msg_origin / plugins_name / is_stopped() / get_platform_name() / get_platform_id()。
    """

    def __init__(self, umo: str):
        self.unified_msg_origin = umo
        self.session_id = umo.split(":")[-1] if ":" in umo else umo
        self.message_str = ""
        self.message_obj = None
        self.plugins_name: Optional[List[str]] = None
        self._extra: Dict[str, Any] = {}
        self._stopped = False

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_sender_id(self):
        return "scheduler"

    def get_sender_name(self):
        return "EnhancedScheduler"

    def get_group_id(self):
        return ""

    def get_platform_name(self):
        return _platform_name_from_umo(self.unified_msg_origin)

    def get_platform_id(self):
        return ""

    def is_stopped(self):
        return self._stopped

    def stop_event(self):
        self._stopped = True


def _is_on_llm_request_hook(handler) -> bool:
    """判断一个方法是否被 AstrBot 注册为 on_llm_request 钩子。
    三重检测以兼容不同 AstrBot 版本的钩子标记方式。"""
    try:
        if getattr(handler, "__event_type__", None) == "on_llm_request":
            return True
    except Exception:
        pass
    try:
        if getattr(handler, "_filter", None) == "on_llm_request":
            return True
    except Exception:
        pass
    try:
        # 方法名精确等于 on_llm_request 作为兜底（instant_memo 验证可行）
        if getattr(handler, "__name__", "") == "on_llm_request":
            return True
    except Exception:
        pass
    return False
