"""Independent, interruptible task runners; no global polling/execution loop."""
import asyncio
import copy
import math
import time


class SchedulerRuntime:
    def __init__(self, core, store, executor, config, logger):
        self.core = core
        self.store = store
        self.executor = executor
        self.config = config
        self.logger = logger
        self._workers = {}
        self._events = {}
        self._locks = {}
        self._states = {}
        self._manual = set()
        self._manual_ids = set()
        self._retired = set()
        self._started = False
        self._closed = False

    def lock(self, task_id):
        return self._locks.setdefault(task_id, asyncio.Lock())

    def status(self, task_id):
        task = self.store.tasks.get(task_id, {})
        if not isinstance(task, dict):
            return {"state": "faulted", "error": "任务格式错误"}
        if task.get("migration_notice"):
            return {"state": "needs_configuration", "error": task["migration_notice"]}
        if not task.get("enabled", True):
            return {"state": "disabled"}
        return dict(self._states.get(task_id, {"state": "waiting"}))

    def start(self):
        if self._started or self._closed:
            return
        self._started = True
        for task_id in self.store.tasks:
            self.refresh(task_id)

    def refresh(self, task_id):
        """Called after a durable edit, under the task lock; reset only this task."""
        self._states.pop(task_id, None)
        task = self.store.tasks.get(task_id)
        worker = self._workers.get(task_id)
        if not isinstance(task, dict) or not task.get("enabled", True):
            if worker and not worker.done():
                worker.cancel()
                self._retired.add(worker)
                worker.add_done_callback(self._retired.discard)
            self._workers.pop(task_id, None)
        elif self._started and not self._closed and (worker is None or worker.done()):
            self._workers[task_id] = asyncio.create_task(
                self._run(task_id), name=f"enhanced-scheduler:{task_id}"
            )
        self._events.setdefault(task_id, asyncio.Event()).set()

    def wake_all(self):
        for event in self._events.values():
            event.set()

    async def close(self):
        self._closed = True
        pending = set(self._workers.values()) | self._retired | self._manual
        for worker in pending:
            worker.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._workers.clear()
        self._manual.clear()
        self._retired.clear()

    def _validate(self, task):
        triggers = task.get("triggers")
        if not isinstance(triggers, list) or not triggers:
            raise ValueError("任务至少需要一个主动触发器")
        ids = set()
        for trigger in triggers:
            ok, message = self.core.validate_trigger(trigger)
            if not ok:
                raise ValueError(message)
            if trigger["id"] in ids:
                raise ValueError("触发器 id 重复")
            ids.add(trigger["id"])
            if not math.isfinite(float(trigger.get("last_fired", 0) or 0)):
                raise ValueError("触发时间必须是有限数值")
        if not any(t["type"] in self.core.ACTIVE_TYPES for t in triggers):
            raise ValueError("任务至少需要一个主动触发器")
        if not math.isfinite(float(task.get("last_success_time", 0) or 0)):
            raise ValueError("成功时间必须是有限数值")
        if "targets" in task or not isinstance(task.get("target", ""), str):
            raise ValueError("任务必须使用单个 target 对话")
        content = task.get("content")
        if not isinstance(content, dict):
            raise ValueError("任务内容格式错误")
        if content.get("mode", "fixed") not in ("fixed", "standalone", "conversation"):
            raise ValueError("任务执行模式错误")
        if str(content.get("text", "") or "").strip() and not task.get("target", "").strip():
            raise ValueError("请选择一个发送目标对话")

    def _delay(self, task, now):
        self._validate(task)
        if any(self.core.check_active_fire(t, now)[0] for t in task["triggers"]):
            return 0.0
        next_fire = self.core.next_trigger_preview(task["triggers"], now)
        if next_fire is None or not math.isfinite(next_fire):
            raise ValueError("无法计算下一次触发时间，请检查触发器配置")
        # Periodic clock recheck bounds the effect of wall-clock corrections.
        try:
            clock_check = max(2, min(3600, int(self.config.get("poll_interval", 600))))
        except (ValueError, TypeError):
            clock_check = 600
        return max(0.05, min(next_fire - now + 0.05, float(clock_check)))

    async def _wait(self, event, delay=None):
        if delay is None:
            await event.wait()
        elif delay > 0:
            try:
                await asyncio.wait_for(event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _run(self, task_id):
        event = self._events.setdefault(task_id, asyncio.Event())
        try:
            while not self._closed:
                event.clear()
                task = self.store.tasks.get(task_id)
                if task is None or not task.get("enabled", True):
                    return
                if self._states.get(task_id, {}).get("state") == "faulted":
                    await self._wait(event)
                    continue
                try:
                    delay = self._delay(task, time.time())
                    if self._states.get(task_id, {}).get("state") != "running":
                        self._states[task_id] = {
                            "state": "waiting",
                            "next_check": time.time() + delay,
                        }
                    await self._wait(event, delay)
                    if event.is_set():
                        continue  # Definition changed: recompute, do not use a stale deadline.
                    async with self.lock(task_id):
                        task = self.store.tasks.get(task_id)
                        if task is None or not task.get("enabled", True):
                            continue
                        if self._states.get(task_id, {}).get("state") == "faulted":
                            continue
                        try:
                            await self._check(task_id, task)
                        except Exception as exc:
                            await self._fault(task_id, exc)
                except Exception as exc:
                    await self._fault(task_id, exc)
        except asyncio.CancelledError:
            raise

    async def _fault(self, task_id, exc, source="scheduled"):
        detail = f"任务已暂停调度，修正后保存任务可恢复: {exc}"
        fault = {"state": "faulted", "error": detail}
        self._states[task_id] = fault
        self._events.setdefault(task_id, asyncio.Event()).set()
        self.logger.error(f"[EnhancedScheduler] 任务 {task_id}: {detail}")
        task = self.store.tasks.get(task_id, {})
        self._log(task_id, task, time.time(), "failed", None, detail, {}, None, source)
        try:
            await self.store.save()
        except Exception as save_error:
            # Keep a visible in-memory fault even if the disk is full. No retry loop.
            fault["error"] += f"；日志保存失败: {save_error}"

    async def _check(self, task_id, task):
        self._validate(task)
        now = time.time()
        result = self.core.evaluate_task(
            task["triggers"], now, float(task.get("last_success_time", 0) or 0)
        )
        if not result["has_active_fire"]:
            # A pending trigger must either be consumed or fault; never zero-delay spin.
            if any(self.core.check_active_fire(t, now)[0] for t in task["triggers"]):
                raise RuntimeError("到点触发器未被处理")
            return
        points = result["fired_points"]
        if not points:
            raise RuntimeError("到点触发器缺少触发时间")
        for trigger in task["triggers"]:
            if trigger["id"] in points:
                trigger["last_fired"] = points[trigger["id"]]
        task["updated_at"] = now
        tof = result["trigger_tof"]
        if not result["should_run"]:
            self._log(task_id, task, now, "skipped", None,
                      "跳过（被动触发器条件未全部满足）", tof, False, "scheduled")
            await self.store.save()
            return
        # Durably consume the time opportunity before external side effects.
        # Sending has no idempotency key: never automatically replay an uncertain send.
        await self.store.save()
        await self._execute(task_id, task, now, tof, "scheduled")

    async def trigger_now(self, task_id):
        if self._closed or not self._started:
            raise RuntimeError("调度器尚未启动或已停止")
        if self.lock(task_id).locked() or task_id in self._manual_ids:
            raise RuntimeError("该任务正在执行或编辑，请稍后再试")
        self._manual_ids.add(task_id)
        work = asyncio.create_task(self._manual_run(task_id))
        self._manual.add(work)
        work.add_done_callback(self._manual.discard)
        try:
            return await work
        finally:
            self._manual_ids.discard(task_id)

    async def _manual_run(self, task_id):
        async with self.lock(task_id):
            task = self.store.tasks[task_id]
            if task.get("migration_notice"):
                raise ValueError("请先编辑保存迁移后的单目标任务")
            self._validate(task)
            try:
                return await self._execute(task_id, task, time.time(), {}, "manual")
            except Exception as exc:
                await self._fault(task_id, exc, source="manual")
                raise

    async def _execute(self, task_id, task, now, tof, source):
        previous = self._states.get(task_id)
        self._states[task_id] = {"state": "running"}
        try:
            action, target_result, detail, success = await self.executor.execute(
                copy.deepcopy(task), now, source
            )
            if success:
                task["last_success_time"] = time.time()
            self._log(task_id, task, now, action, target_result, detail, tof,
                      True if source == "scheduled" else None, source)
            await self.store.save()
            return action, detail
        finally:
            self._states[task_id] = previous or {"state": "waiting"}

    def _log(self, task_id, task, now, action, target_result, detail, tof, logic, source):
        self.store.append_log({
            "time": now, "task_id": task_id, "task_name": task.get("name", ""),
            "trigger_tof": tof, "logic_result": logic, "action": action,
            "target_result": target_result, "detail": detail, "source": source,
        })
