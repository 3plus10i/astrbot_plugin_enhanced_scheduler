"""AstrBot lifecycle and Web API for the enhanced scheduler."""
import copy
import importlib.util
import math
import os
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


@register(PLUGIN_NAME, "3plus10i", "未来任务调度器，主动取或、被动取且的多触发器组合。", "1.4.0")
class EnhancedSchedulerPlugin(Star):
    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.store = TaskStore(str(StarTools.get_data_dir(PLUGIN_NAME)), self.config, core)
        self.executor = TaskExecutor(context, self.config, core, self.store)
        self.runtime = SchedulerRuntime(core, self.store, self.executor, self.config, logger)
        self._register_web_apis()

    async def initialize(self):
        await self.store.save()
        self.runtime.start()
        logger.info("[EnhancedScheduler] 已启动（每任务独立等待）")

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
        resp = {"status": "success", "config": self.config}
        # 预计算每个任务的下次主动触发时间，供前端直接展示（无需逐行异步请求）
        now = time.time()
        tasks_out = {}
        for tid, t in self.store.data.get("tasks", {}).items():
            t_copy = copy.deepcopy(t)
            t_copy["_runtime"] = self.runtime.status(tid)
            try:
                t_copy["_next_fire"] = (
                    core.next_trigger_preview(t.get("triggers", []), now)
                    if t_copy["_runtime"]["state"] in ("waiting", "running") else None
                )
            except Exception:
                t_copy["_next_fire"] = None
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


    def _upsert_task(self, req: dict) -> tuple:
        """新增或更新任务。req 含 id 时为更新，否则为新增。返回 (ok, msg, task_id)。"""
        tasks = self.store.data.setdefault("tasks", {})
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
            old_triggers = tasks[task_id].get("triggers", [])
            for et in old_triggers if isinstance(old_triggers, list) else []:
                if not isinstance(et, dict):
                    continue
                eid = et.get("id")
                if eid is not None:
                    try:
                        value = float(et.get("last_fired", 0.0) or 0.0)
                        existing_lf[eid] = value if math.isfinite(value) else time.time()
                    except (ValueError, TypeError):
                        existing_lf[eid] = time.time()
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
            if not isinstance(tg.get("config", {}), dict):
                return False, f"触发器 {tid}: config 必须是对象", ""
            cfg = dict(tg.get("config", {}))
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

        # 固定语义：主动型之间取或、被动型之间取且 —— 没有主动触发器则永远不会触发
        if not any(t["type"] in core.ACTIVE_TYPES for t in norm_triggers):
            return False, "至少需要一个主动型触发器（周期型或 cron 型）", ""

        content = req.get("content", {})
        if not isinstance(content, dict):
            return False, "任务内容格式错误", ""
        text = str(content.get("text", "") or "")
        mode = content.get("mode", "fixed")
        if mode not in ("fixed", "standalone", "conversation"):
            return False, "执行模式不合法", ""
        system_prompt = str(content.get("system_prompt", "") or "")
        time_aware = bool(content.get("time_aware", True))
        holiday_aware = bool(content.get("holiday_aware", False))

        if "targets" in req:
            return False, "不再支持 targets 数组，请使用单个 target 对话字符串", ""
        target = req.get("target", "")
        if not isinstance(target, str):
            return False, "target 必须是单个对话字符串", ""
        target = target.strip()
        if text.strip() and not target:
            return False, "请选择一个发送目标对话（空动作可留空）", ""

        enabled = bool(req.get("enabled", True))

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
            task["triggers"] = norm_triggers
            task["content"] = {
                "text": text,
                "mode": mode,
                "system_prompt": system_prompt,
                "time_aware": time_aware,
                "holiday_aware": holiday_aware,
            }
            task["target"] = target
            task.pop("migration_notice", None)
            task.pop("invalid_data", None)
            task["updated_at"] = now
            return True, "", task_id
        else:
            new_id = task_id or str(uuid.uuid4())
            tasks[new_id] = {
                "id": new_id,
                "name": name,
                "enabled": enabled,
                "triggers": norm_triggers,
                "content": {
                    "text": text,
                    "mode": mode,
                    "system_prompt": system_prompt,
                    "time_aware": time_aware,
                    "holiday_aware": holiday_aware,
                },
                "target": target,
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
            task.update(id=new_id, name=task.get("name", "") + " (副本)", enabled=False,
                        created_at=now, updated_at=now, last_success_time=0.0)
            for trigger in task.get("triggers", []):
                trigger["last_fired"] = now
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
            for tg in triggers:
                ok, msg = core.validate_trigger(tg)
                entry = {"id": tg.get("id"), "ok": ok, "msg": msg, "future_fires": None}
                if ok:
                    if tg.get("type") in core.ACTIVE_TYPES:
                        results["has_active"] = True
                        # 以当前时刻为起点预览该触发器的未来 3 个触发点（与 last_fired 无关）
                        entry["future_fires"] = core.future_fires(tg, now, 3)
                results["triggers"].append(entry)

            # 下次检查触发时间（触发器组级别，仅主动触发器）
            results["next_fire"] = core.next_trigger_preview(triggers, now)
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
