"""AstrBot lifecycle and Web API for the enhanced scheduler."""
import copy
import asyncio
import importlib.util
import math
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional
from astrbot.api import logger
from astrbot.api.star import Star, Context, register, StarTools


def _load_local(name):
    """Load a fresh sibling for each plugin load; never reuse top-level module caches."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    spec = importlib.util.spec_from_file_location(__name__ + "_" + name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载插件模块: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


core = _load_local("scheduler_core")
TaskStore = _load_local("storage").TaskStore
TaskExecutor = _load_local("task_executor").TaskExecutor
SchedulerRuntime = _load_local("scheduler_runtime").SchedulerRuntime
PLUGIN_NAME = "astrbot_plugin_enhanced_scheduler"
TASK_NAME_MAX_LENGTH = 32
COPY_NAME_SUFFIX = " (副本)"
COPY_NAME_PATTERN = re.compile(re.escape(COPY_NAME_SUFFIX[:-1]) + r"(?: ([1-9][0-9]*))?" + re.escape(COPY_NAME_SUFFIX[-1]) + r"$")


def _copy_task_name(source_name: str, existing_names: set[str]) -> str:
    """沿用副本编号，检查截断后的完整名称，直到找到未占用的名称。"""
    base_name = source_name
    number = 1
    # 同时收敛此前连续复制形成的重复副本后缀。
    while match := COPY_NAME_PATTERN.search(base_name):
        number = max(number, int(match.group(1) or 1) + 1)
        base_name = base_name[:match.start()]
    while True:
        suffix = COPY_NAME_SUFFIX if number == 1 else COPY_NAME_SUFFIX[:-1] + f" {number}" + COPY_NAME_SUFFIX[-1]
        candidate = base_name[:max(0, TASK_NAME_MAX_LENGTH - len(suffix))].rstrip() + suffix
        if candidate not in existing_names:
            return candidate
        number += 1


@register(PLUGIN_NAME, "3plus10i", "未来任务调度器，支持复杂的多触发条件组合。", "1.6.1")
class EnhancedSchedulerPlugin(Star):
    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.store = TaskStore(str(StarTools.get_data_dir(PLUGIN_NAME)), self.config, core)
        self.executor = TaskExecutor(context, self.config, core, self.store)
        self.runtime = SchedulerRuntime(core, self.store, self.executor, logger)
        self._register_web_apis()

    async def initialize(self):
        await self.runtime.start()
        logger.info("[EnhancedScheduler] 已启动")

    async def terminate(self):
        await self.runtime.close()
        logger.info("[EnhancedScheduler] 已停止")

    def on_config_update(self, config: dict):
        if config and config is not self.config:
            self.config.update(config)
        self.runtime.wake_all()

    def _register_web_apis(self):
        prefix = f"/{PLUGIN_NAME}"
        self.context.register_web_api(f"{prefix}/get_data", self._api_get_data, ["GET"], "获取任务与配置")
        self.context.register_web_api(f"{prefix}/save_config", self._api_save_config, ["POST"], "保存插件配置")
        self.context.register_web_api(f"{prefix}/get_sessions", self._api_get_sessions, ["GET"], "获取活跃会话列表")
        self.context.register_web_api(f"{prefix}/get_providers", self._api_get_providers, ["GET"], "获取语境判定模型选项")
        self.context.register_web_api(f"{prefix}/upsert_task", self._api_upsert_task, ["POST"], "新增/更新任务")
        self.context.register_web_api(f"{prefix}/delete_task", self._api_delete_task, ["POST"], "删除任务")
        self.context.register_web_api(f"{prefix}/copy_task", self._api_copy_task, ["POST"], "复制任务")
        self.context.register_web_api(f"{prefix}/validate", self._api_validate, ["POST"], "校验触发器并预览未来触发时间")
        self.context.register_web_api(f"{prefix}/preview_next", self._api_preview_next, ["POST"], "预览下次触发时间")
        self.context.register_web_api(f"{prefix}/trigger_now", self._api_trigger_now, ["POST"], "立即手动触发一次任务")
        self.context.register_web_api(f"{prefix}/get_run_logs", self._api_get_run_logs, ["GET"], "分页获取运行日志")
        self.context.register_web_api(f"{prefix}/get_llm_logs", self._api_get_llm_logs, ["GET"], "分页获取 LLM 调用日志元数据")
        self.context.register_web_api(f"{prefix}/get_llm_log_detail", self._api_get_llm_log_detail, ["GET"], "获取单条 LLM 调用日志正文")
        self.context.register_web_api(f"{prefix}/get_llm_image", self._api_get_llm_image, ["GET"], "按需获取日志中的图片")
        self.context.register_web_api(f"{prefix}/clear_llm_logs", self._api_clear_llm_logs, ["POST"], "清空 LLM 调用日志")


    async def _api_get_data(self):
        from quart import jsonify
        resp = {"status": "success", "config": self.config,
                "limits": {"task_name_max_length": TASK_NAME_MAX_LENGTH}}
        # 预计算下一次可执行机会与联合概率，纳入所有主动、被动条件。
        now = time.time()
        tasks_out = {}
        for tid, t in self.store.data.get("tasks", {}).items():
            t_copy = copy.deepcopy(t)
            t_copy["_runtime"] = self.runtime.status(tid)
            try:
                preview = core.next_task_preview(t.get("triggers", []), now, float(t.get("last_success_time", 0) or 0))
                if t.get("expires_at") and preview["next_fire"] and preview["next_fire"] >= t["expires_at"]:
                    preview["next_fire"] = None
                t_copy["_next_fire"] = preview["next_fire"] if t_copy["_runtime"]["state"] in ("waiting", "running") else None
                t_copy["_execution_probability"] = preview["execution_probability"]
            except Exception:
                t_copy["_next_fire"] = None
                t_copy["_execution_probability"] = None
            tasks_out[tid] = t_copy
        resp["tasks"] = tasks_out
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
            req = dict(req)
            req["id"] = str(req.get("id", "")).strip() or str(uuid.uuid4())
            task_id = req["id"]
            async with self.runtime.lock(task_id):
                previous = copy.deepcopy(self.store.tasks.get(task_id))
                ok, msg, task_id = self._upsert_task(req)
                if not ok:
                    return jsonify({"status": "error", "message": msg}), 400
                try:
                    await self.store.save(include_logs=False)
                except Exception:
                    if previous is None:
                        self.store.tasks.pop(task_id, None)
                    else:
                        self.store.tasks[task_id] = previous
                    raise
                self.runtime.refresh(task_id)
            return jsonify({"status": "success", "id": task_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500


    async def _api_get_providers(self):
        """Only expose chat provider IDs/models, never provider configuration or keys."""
        from quart import request, jsonify
        try:
            selected = str(request.args.get("provider_id", "") or "").strip()
            providers = []
            for provider in self.context.get_all_providers():
                meta = provider.meta()
                providers.append({"id": meta.id, "model": provider.get_model()})
            models, error = [], ""
            if selected:
                provider = next((p for p in self.context.get_all_providers() if p.meta().id == selected), None)
                if provider is None:
                    return jsonify({"status": "error", "message": "模型提供商不存在"}), 400
                try:
                    models = await asyncio.wait_for(provider.get_models(), timeout=10)
                    models = [m for m in models if isinstance(m, str)]
                except Exception as exc:
                    error = f"模型列表获取失败，可手动输入模型名称: {exc}"
            return jsonify({"status": "success", "providers": providers, "models": models, "message": error})
        except Exception as exc:
            return jsonify({"status": "error", "message": str(exc)}), 500


    def _upsert_task(self, req: dict) -> tuple:
        """新增或更新任务。req 含 id 时为更新，否则为新增。返回 (ok, msg, task_id)。"""
        tasks = self.store.data.setdefault("tasks", {})
        task_id = str(req.get("id", "")).strip()

        # 字段校验
        name = str(req.get("name", "")).strip()
        if not name:
            return False, "任务名称不能为空", ""
        if len(name) > TASK_NAME_MAX_LENGTH:
            return False, f"任务名称不能超过 {TASK_NAME_MAX_LENGTH} 个字符", ""
        if any(key in req for key in ("logic_expr", "migration_notice", "invalid_data")):
            return False, "不再支持旧任务格式，请重新创建任务", ""

        triggers = req.get("triggers", [])
        if not isinstance(triggers, list) or not triggers:
            return False, "至少需要一个触发器", ""
        # 规范化并校验每个触发器。主动触发器不保存历史执行记录，
        # 下次触发点始终从当前墙钟和配置计算。
        is_update = bool(task_id) and task_id in tasks
        now = time.time()
        norm_triggers = []
        seen_ids = set()
        labels = core.trigger_labels(triggers)
        for tg in triggers:
            if not isinstance(tg, dict):
                return False, "触发器格式错误", ""
            if "last_fired" in tg:
                return False, "不再支持触发器 last_fired 字段", ""
            tid = tg.get("id")
            if isinstance(tid, bool) or not isinstance(tid, int) or not 1 <= tid <= core.MAX_TRIGGER_ID:
                return False, f"触发器 id 必须是可精确表示的正整数（收到 {tid}）", ""
            if tid in seen_ids:
                return False, f"触发器 id {tid} 重复", ""
            seen_ids.add(tid)
            label = labels.get(tid, str(tid))
            if not isinstance(tg.get("config", {}), dict):
                return False, f"触发器 {label}: config 必须是对象", ""
            cfg = dict(tg.get("config", {}))
            # interval 触发器缺省 base_time 时，默认设为创建/保存当天的 0 点
            if tg.get("type") == "interval" and not str(cfg.get("base_time", "")).strip():
                cfg["base_time"] = core.day_start_iso(now)
            norm_tg = {
                "id": tid,
                "type": tg.get("type"),
                "config": cfg,
            }
            ok, msg = core.validate_trigger(norm_tg)
            if not ok:
                return False, f"触发器 {label}: {msg}", ""
            norm_triggers.append(norm_tg)

        # 固定语义：主动型之间取或、被动型之间取且 —— 没有主动触发器则永远不会触发
        if not any(t["type"] in core.ACTIVE_TYPES for t in norm_triggers):
            return False, "至少需要一个主动型触发器（周期型或 cron 型）", ""

        content = req.get("content", {})
        if not isinstance(content, dict):
            return False, "任务内容格式错误", ""
        if "use_llm" in content:
            return False, "不再支持 use_llm 字段，请使用 mode 指定执行模式", ""
        text = str(content.get("text", "") or "")
        mode = content.get("mode", "fixed")
        if mode not in ("fixed", "standalone", "conversation"):
            return False, "执行模式不合法", ""
        system_prompt = str(content.get("system_prompt", "") or "")
        time_aware = bool(content.get("time_aware", True))
        holiday_aware = bool(content.get("holiday_aware", False))
        ok, msg = core.validate_task_options(req)
        if not ok:
            return False, msg, ""
        record_history = content.get("record_history", True)
        expires_at = req.get("expires_at")

        if "targets" in req:
            return False, "不再支持 targets 数组，请使用单个 target 对话字符串", ""
        target = req.get("target", "")
        if not isinstance(target, str):
            return False, "target 必须是单个对话字符串", ""
        target = target.strip()
        if (text.strip() or any(t["type"] == core.T_CONTEXTUAL for t in norm_triggers)) and not target:
            return False, "请选择一个发送目标对话（空动作可留空）", ""

        enabled = bool(req.get("enabled", True))
        expired = expires_at is not None and now >= expires_at
        if expired:
            enabled = False

        if task_id and task_id in tasks:
            task = tasks[task_id]
            task["id"] = task_id
            try:
                last_success = float(task.get("last_success_time", 0) or 0)
                task["last_success_time"] = last_success if math.isfinite(last_success) else 0.0
            except (ValueError, TypeError):
                task["last_success_time"] = 0.0
            task["name"] = name
            task["enabled"] = enabled
            task["expires_at"] = expires_at
            task.pop("disabled_reason", None)
            if expired:
                task["disabled_reason"] = "有效期已结束"
            task["triggers"] = norm_triggers
            task["content"] = {
                "text": text,
                "mode": mode,
                "system_prompt": system_prompt,
                "time_aware": time_aware,
                "holiday_aware": holiday_aware,
                "record_history": record_history,
            }
            task["target"] = target
            task["updated_at"] = now
            return True, "", task_id
        else:
            new_id = task_id or str(uuid.uuid4())
            tasks[new_id] = {
                "id": new_id,
                "name": name,
                "enabled": enabled,
                "expires_at": expires_at,
                "triggers": norm_triggers,
                "content": {
                    "text": text,
                    "mode": mode,
                    "system_prompt": system_prompt,
                    "time_aware": time_aware,
                    "holiday_aware": holiday_aware,
                    "record_history": record_history,
                },
                "target": target,
                "last_success_time": 0.0,
                "created_at": now,
                "updated_at": now,
                **({"disabled_reason": "有效期已结束"} if expired else {}),
            }
            return True, "", new_id


    async def _api_delete_task(self):
        from quart import request, jsonify
        try:
            req = await request.json
            if not isinstance(req, dict):
                return jsonify({"status": "error", "message": "请求体必须是 JSON 对象"}), 400
            task_id = str(req.get("id", "")).strip()
            async with self.runtime.lock(task_id):
                if task_id not in self.store.tasks:
                    return jsonify({"status": "error", "message": "任务不存在"}), 404
                previous = self.store.tasks.pop(task_id)
                try:
                    await self.store.save(include_logs=False)
                except Exception:
                    self.store.tasks[task_id] = previous
                    raise
                self.runtime.refresh(task_id)
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
            async with self.runtime.lock(src_id):
                if src_id not in self.store.tasks:
                    return jsonify({"status": "error", "message": "源任务不存在"}), 404
                task = copy.deepcopy(self.store.tasks[src_id])
            now = time.time()
            new_id = str(uuid.uuid4())
            name = _copy_task_name(str(task.get("name", "")), {str(t.get("name", "")) for t in self.store.tasks.values()})
            task.update(id=new_id, name=name, enabled=False,
                        created_at=now, updated_at=now, last_success_time=0.0)
            self.store.tasks[new_id] = task
            try:
                await self.store.save(include_logs=False)
            except Exception:
                self.store.tasks.pop(new_id, None)
                raise
            self.runtime.refresh(new_id)
            return jsonify({"status": "success", "id": new_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500


    async def _api_validate(self):
        """校验触发器并预览未来触发时间，供前端实时校验。"""
        from quart import request, jsonify
        try:
            req = await request.json
            if not isinstance(req, dict):
                return jsonify({"status": "error", "message": "请求体必须是 JSON 对象"}), 400
            results: Dict[str, Any] = {"triggers": [], "has_active": False, "next_fire": None}

            triggers = req.get("triggers", []) or []
            now = time.time()
            seen_ids = set()
            for tg in triggers:
                ok, msg = core.validate_trigger(tg)
                if ok and tg["id"] in seen_ids:
                    ok, msg = False, "触发器 ID 重复"
                if ok:
                    seen_ids.add(tg["id"])
                entry = {"id": tg.get("id"), "ok": ok, "msg": msg, "future_fires": None}
                if ok:
                    if tg.get("type") in core.ACTIVE_TYPES:
                        results["has_active"] = True
                        # 以当前时刻为起点预览该触发器的未来 3 个触发点（与 last_fired 无关）
                        entry["future_fires"] = core.future_fires(tg, now, 3)
                results["triggers"].append(entry)

            task = self.store.tasks.get(str(req.get("task_id", "")), {})
            results["last_success_time"] = float(task.get("last_success_time", 0) or 0)
            results.update(core.next_task_preview(triggers, now, results["last_success_time"]))
            expiry = req.get("expires_at", task.get("expires_at"))
            ok, message = core.validate_task_options({"expires_at": expiry})
            if not ok:
                return jsonify({"status": "error", "message": message}), 400
            if expiry and results["next_fire"] and results["next_fire"] >= expiry:
                results["next_fire"] = None
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
            now = time.time()
            task = self.store.tasks.get(str(req.get("task_id", "")), {})
            preview = core.next_task_preview(triggers, now, float(task.get("last_success_time", 0) or 0))
            expiry = req.get("expires_at", task.get("expires_at"))
            ok, message = core.validate_task_options({"expires_at": expiry})
            if not ok:
                return jsonify({"status": "error", "message": message}), 400
            if expiry and preview["next_fire"] and preview["next_fire"] >= expiry:
                preview["next_fire"] = None
            return jsonify({"status": "success", **preview, "now": now})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500


    async def _api_trigger_now(self):
        from quart import request, jsonify
        try:
            req = await request.json
            if not isinstance(req, dict):
                return jsonify({"status": "error", "message": "请求体必须是 JSON 对象"}), 400
            task_id = str(req.get("id", "")).strip()
            if task_id not in self.store.tasks:
                return jsonify({"status": "error", "message": "任务不存在"}), 404
            action, detail = await self.runtime.trigger_now(task_id)
            return jsonify({"status": "success", "action": action, "detail": detail})
        except (ValueError, RuntimeError) as e:
            return jsonify({"status": "error", "message": str(e)}), 409
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500


    async def _api_get_run_logs(self):
        """分页获取运行日志（最新在前）。日志存在独立的 run_logs.json，不随 get_data 全量下发。"""
        from quart import request, jsonify
        try:
            limit = int(request.args.get("limit", 20))
            offset = int(request.args.get("offset", 0))
        except (TypeError, ValueError):
            limit, offset = 20, 0
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        ordered = list(reversed(self.store.run_logs))
        return jsonify({"status": "success", "total": len(ordered), "entries": ordered[offset : offset + limit]})


    async def _api_get_llm_logs(self):
        """轻量元数据分页（最新在前）。只读记录文件首行，不返回正文与图片。"""
        from quart import request, jsonify
        try:
            limit = int(request.args.get("limit", 10))
            offset = int(request.args.get("offset", 0))
        except (TypeError, ValueError):
            limit, offset = 10, 0
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        try:
            total, entries = await self.store.io(self.store._llm_log_page_io, offset, limit)
            return jsonify({"status": "success", "total": total, "entries": entries})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500


    async def _api_get_llm_log_detail(self):
        """按文件名返回单条记录全文（正文中的图片为占位标记，图片另行按需获取）。"""
        from quart import request, jsonify
        try:
            name = str(request.args.get("name", "")).strip()
            result = await self.store.io(self.store._read_llm_record_io, name)
            if result is None:
                return jsonify({"status": "error", "message": "记录不存在或已被裁剪"}), 404
            meta, body = result
            return jsonify({"status": "success", "meta": meta, "body": body})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500


    async def _api_get_llm_image(self):
        """按内容寻址取单张图片（返回 data URL，前端按 md5 缓存，同一图片只取一次）。"""
        from quart import request, jsonify
        try:
            name = str(request.args.get("name", "")).strip()
            data_url = await self.store.io(self.store._read_llm_image_io, name)
            if data_url is None:
                return jsonify({"status": "error", "message": f"图片不存在或已被回收（{name}）"}), 404
            return jsonify({"status": "success", "data_url": data_url})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500


    async def _api_clear_llm_logs(self):
        """清空全部 LLM 调用记录文件与图片池。"""
        from quart import jsonify
        try:
            await self.store.io(self.store._clear_llm_logs_io)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
