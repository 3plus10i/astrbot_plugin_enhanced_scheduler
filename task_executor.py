"""Execute one task against one conversation, including AstrBot AI integration."""
import asyncio
import json
import time
import urllib.request
from typing import Any, Dict, List, Optional
from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.provider import ProviderRequest
# AstrBot 内部管线工具（对话 AI 模式用于正确构建请求并广播 on_llm_request 钩子）
# 这些是框架内部路径，跨版本可能变动；导入失败时降级为「不广播钩子」而非崩溃。
try:
    from astrbot.core.pipeline.context_utils import call_event_hook as _call_event_hook
    from astrbot.core.star.star_handler import EventType as _EventType
except Exception:  # noqa: BLE001
    _call_event_hook = None

# 标准化的任务提示词包裹模板（两种 AI 模式共用）
#   {{time}}            -> YYYY年MM月DD日 星期X HH:MM:SS
#   {{holiday_clause}}  -> "" 或 "，今天是…"（节假日感知开关开启时）
#   {{task_prompt}}     -> 用户填写的任务提示词
SCHEDULED_PROMPT_WITH_TIME = "<scheduled_task>这是由计划任务自动触发的一次会话，现在时间是 {{time}}{{holiday_clause}}。请根据以下指令进行回复。你的回复将被直接发送给用户。{{task_prompt}}</scheduled_task>"
SCHEDULED_PROMPT_NO_TIME = "<scheduled_task>这是由计划任务自动触发的一次会话{{holiday_clause}}。请根据以下指令进行回复。你的回复将被直接发送给用户。{{task_prompt}}</scheduled_task>"

# 工作日/节假日查询接口（timor.tech），按日期查询，返回 JSON
# 用 https：http 会被 302 到 https，多一次握手且实测更慢/更易超时
# 该接口对空 User-Agent 返回 403，必须带上浏览器风格 UA
HOLIDAY_API_URL = "https://timor.tech/api/holiday/info/"

class TaskExecutor:
    def __init__(self, context, config, core, store):
        self.context = context
        self.config = config
        self.core = core
        self.store = store
        self._holiday_cache = {}
        self._session_locks = {}

    async def execute(self, task, now, source="scheduled"):
        target = task.get("target", "")
        # Serialize this plugin's requests/history updates for the same conversation.
        lock = self._session_locks.setdefault(target, asyncio.Lock())
        async with lock:
            try:
                return await asyncio.wait_for(
                    self._execute_task(task, time.time(), source), timeout=self._llm_timeout() + 40
                )
            except asyncio.TimeoutError:
                detail = "任务执行超时，送达状态未知，本次不自动重发"
                return "failed", {"umo": target, "ok": False, "error": detail}, detail, False

    def _llm_timeout(self) -> int:
        try:
            v = int(self.config.get("llm_timeout", 60))
            return max(5, v)
        except (TypeError, ValueError):
            return 60

    def _wrap_scheduled_prompt(self, task_prompt: str, time_aware: bool, holiday_clause: str, now: float) -> str:
        """把任务提示词包进标准化的 <scheduled_task> 外层（两种 AI 模式共用）。"""
        tpl = SCHEDULED_PROMPT_WITH_TIME if time_aware else SCHEDULED_PROMPT_NO_TIME
        return self.core.render_template(
            tpl,
            {"task_prompt": task_prompt, "holiday_clause": holiday_clause},
            now,
        )

    def _standalone_system_prompt(self, content: dict) -> str:
        """独立 AI 模式的系统提示（任务级文本，不做参数渲染）。"""
        raw = content.get("system_prompt", "") if isinstance(content, dict) else ""
        return raw.strip() if isinstance(raw, str) else ""

    async def _holiday_clause(self, now: float) -> str:
        """取「，今天是…」子句（工作日/周末/节日/调休）。查询失败返回空串，不影响发送。"""
        date_str = time.strftime("%Y-%m-%d", time.localtime(now))
        cached = self._holiday_cache.get(date_str)
        if cached is not None:
            return cached
        try:
            data = await asyncio.to_thread(self._fetch_holiday_io, date_str)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[EnhancedScheduler] 查询节假日信息异常: {e}")
            return ""
        desc = _format_holiday_desc(data)
        if not desc:
            return ""
        clause = "，" + desc
        self._holiday_cache[date_str] = clause
        return clause

    def _fetch_holiday_io(self, date_str: str) -> Optional[dict]:
        """线程内查询节假日接口；失败返回 None。"""
        try:
            req = urllib.request.Request(
                f"{HOLIDAY_API_URL}{date_str}",
                headers={"User-Agent": HOLIDAY_API_UA},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[EnhancedScheduler] 获取节假日信息失败 {date_str}: {e}")
            return None

    async def _execute_task(self, task: dict, now: float, source: str = "scheduled"):
        """Return (action, single target result, detail, success). Never resend on failure."""
        content = task.get("content", {})
        text = str(content.get("text", "") or "")
        mode = _resolve_mode(content)
        umo = task.get("target", "").strip()
        if not text.strip():
            return "noop", None, "空动作（仅记日志）", True
        if not umo:
            raise ValueError("请选择一个发送目标对话")
        log_ctx = {"task_id": task.get("id", ""), "task_name": task.get("name", ""), "source": source}
        if mode == "fixed":
            prompt = text
        else:
            holiday = await self._holiday_clause(now) if content.get("holiday_aware", False) else ""
            prompt = self._wrap_scheduled_prompt(text, content.get("time_aware", True), holiday, now)
        cid = None
        if mode == "fixed":
            message, error = prompt, ""
        elif mode == "standalone":
            message, error = await self._llm_generate_standalone(
                umo, prompt, self._standalone_system_prompt(content), log_ctx
            )
        else:
            message, error, cid = await self._llm_generate_for_target(umo, prompt, log_ctx)
        if error or not message:
            detail = error or "AI 生成结果为空"
            return "failed", {"umo": umo, "ok": False, "error": detail}, detail, False
        try:
            await asyncio.wait_for(self.context.send_message(umo, MessageChain().message(message)), timeout=30)
        except asyncio.TimeoutError:
            detail = "发送超时，送达状态未知，本次不自动重发"
            return "failed", {"umo": umo, "ok": False, "error": detail}, detail, False
        except Exception as exc:
            detail = f"发送失败，本次不自动重发: {exc}"
            return "failed", {"umo": umo, "ok": False, "error": detail}, detail, False
        detail = "成功"
        if mode == "conversation" and cid:
            if not await self._save_assistant_reply(umo, cid, message):
                detail = "发送成功，但会话历史写入失败"
        action = "send_fixed" if mode == "fixed" else "send_llm"
        return action, {"umo": umo, "ok": True, "error": ""}, detail, True

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
        每次调用都会把「最终发送给 AI 的完整请求体」与「AI 回复」写为一条 LLM 调用记录。

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
        每次调用都会把「最终发送给 AI 的完整请求体」与「AI 回复」写为一条 LLM 调用记录。
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
            await self.store.append_llm_log(entry)
        except Exception as e:
            logger.error(f"[EnhancedScheduler] 记录 LLM 日志失败: {e}")


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


def _format_holiday_desc(data) -> str:
    """把 timor.tech 节假日接口的返回转成一句「今天是…」；无法判断时返回空串。

    取值规则：
      holiday 为 null 且 type.type == 0（工作日）      -> 今天是工作日
      holiday 为 null 且 type.type == 1（周末）        -> 今天是周六（用 type.name）
      holiday.holiday == false（调休上班）             -> 今天是工作日，是“国庆前调休”
      holiday.holiday == true（放假）                  -> 今天是“中秋节”假期
    """
    if not isinstance(data, dict) or data.get("code") != 0:
        return ""
    holiday = data.get("holiday")
    if isinstance(holiday, dict):
        name = str(holiday.get("name") or "").strip()
        if not name:
            return ""
        return f"今天是“{name}”假期" if holiday.get("holiday") else f"今天是工作日，是“{name}”"
    ttype = data.get("type")
    if isinstance(ttype, dict):
        if ttype.get("type") == 0:
            return "今天是工作日"
        name = str(ttype.get("name") or "").strip()
        if name:
            return f"今天是{name}"
    return ""


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


# ─────────────────────────────────────────────────────────────────────
# AstrBot 事件适配


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
