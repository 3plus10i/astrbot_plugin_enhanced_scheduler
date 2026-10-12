"""Independent, interruptible task runners; no global polling/execution loop."""
import asyncio
import copy
import math
import time
import uuid


class SchedulerRuntime:
    def __init__(self, core, store, executor, logger):
        self.core = core
        self.store = store
        self.executor = executor
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
        if not task.get("enabled", True):
            return {"state": "disabled", **({"reason": task["disabled_reason"]} if task.get("disabled_reason") else {})}
        return dict(self._states.get(task_id, {"state": "waiting"}))

    async def start(self):
        if self._started or self._closed:
            return
        # Startup only arms workers. Each worker computes a future deadline from
        # the current wall clock; no historical trigger cursor is persisted.
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
        if not self.core.is_current_task_format(task):
            raise ValueError("不再支持旧任务格式，请重新创建任务")
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
        if not any(t["type"] in self.core.ACTIVE_TYPES for t in triggers):
            raise ValueError("任务至少需要一个主动触发器")
        if not math.isfinite(float(task.get("last_success_time", 0) or 0)):
            raise ValueError("成功时间必须是有限数值")
        ok, message = self.core.validate_task_options(task)
        if not ok:
            raise ValueError(message)
        if "targets" in task or not isinstance(task.get("target", ""), str):
            raise ValueError("任务必须使用单个 target 对话")
        content = task.get("content")
        if not isinstance(content, dict):
            raise ValueError("任务内容格式错误")
        if content.get("mode", "fixed") not in ("fixed", "standalone", "conversation"):
            raise ValueError("任务执行模式错误")
        if (str(content.get("text", "") or "").strip() or any(t["type"] == self.core.T_CONTEXTUAL for t in triggers)) and not task.get("target", "").strip():
            raise ValueError("请选择一个发送目标对话")

    async def _expire(self, task_id, task, now):
        ok, message = self.core.validate_task_options(task)
        if not ok:
            raise ValueError(message)
        if not self.core.task_expired(task, now):
            return False
        if task.get("enabled", True):
            task.update(enabled=False, disabled_reason="有效期已结束", updated_at=now)
            self._log(task_id, task, now, "expired", None, "有效期已结束，任务自动停用", {}, None, "scheduled")
            await self.store.save()
        return True

    def _deadline(self, task, now):
        """Returns (下一个未来触发点, 等待时长)。

        非随机被动约束（区间/冷却）一并纳入：区间未开启或冷却未到期时直接等到
        可触发时刻，避免短周期主动触发器在未激活区间反复空转并写大量跳过日志。
        """
        self._validate(task)
        deadline = self.core.next_trigger_preview(
            task["triggers"], now, float(task.get("last_success_time", 0) or 0)
        )
        expiry = task.get("expires_at")
        if expiry is not None and (deadline is None or expiry <= deadline):
            return expiry, max(0.05, expiry - now)
        if deadline is None or not math.isfinite(deadline):
            raise ValueError("无法计算下一次触发时间，请检查触发器配置")
        return deadline, max(0.05, deadline - now + 0.05)

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
                try:
                    started_wait = time.time()
                    if self._states.get(task_id, {}).get("state") == "faulted":
                        # Invalid expiry must remain parked instead of faulting on every loop.
                        options_valid, _ = self.core.validate_task_options(task)
                        expiry = task.get("expires_at") if options_valid else None
                        if expiry and started_wait >= expiry:
                            async with self.lock(task_id):
                                if await self._expire(task_id, task, started_wait):
                                    return
                        await self._wait(event, max(0.05, expiry - started_wait) if expiry else None)
                        continue
                    async with self.lock(task_id):
                        if await self._expire(task_id, task, started_wait):
                            return
                    deadline, delay = self._deadline(task, started_wait)
                    if self._states.get(task_id, {}).get("state") != "running":
                        self._states[task_id] = {
                            "state": "waiting",
                            "next_check": started_wait + delay,
                        }
                    await self._wait(event, delay)
                    if event.is_set():
                        continue  # Definition changed: recompute, do not use a stale deadline.
                    async with self.lock(task_id):
                        task = self.store.tasks.get(task_id)
                        if task is None or not task.get("enabled", True):
                            continue
                        if await self._expire(task_id, task, time.time()):
                            return
                        if self._states.get(task_id, {}).get("state") == "faulted":
                            continue
                        try:
                            await self._check(task_id, task, deadline)
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

    async def _check(self, task_id, task, active_fire_at=None):
        self._validate(task)
        now = time.time()
        result = self.core.evaluate_task(
            task["triggers"], now, float(task.get("last_success_time", 0) or 0), active_fire_at
        )
        if not result["has_active_fire"]:
            # 当前 deadline 没有匹配的主动触发器时，等待下一轮重新计算。
            return
        task["updated_at"] = now
        tof = result["trigger_tof"]
        details = result["trigger_details"]
        run_id = uuid.uuid4().hex
        # Persist before any external AI request or send; each occurrence is consumed once.
        await self.store.save()
        if result["should_run"]:
            previous = self._states.get(task_id)
            self._states[task_id] = {"state": "running"}
            try:
                for trigger in self.core.ordered_passives(task["triggers"]):
                    if trigger["type"] != self.core.T_CONTEXTUAL:
                        continue
                    if self.core.task_expired(task, time.time()):
                        await self._expire(task_id, task, time.time())
                        return
                    assessment = await self.executor.check_context(task, trigger, time.time(), run_id)
                    details[trigger["id"]] = assessment
                    tof[trigger["id"]] = assessment["result"]
                    if not assessment["result"]:
                        result["should_run"] = False
                        break
            finally:
                self._states[task_id] = previous or {"state": "waiting"}
        if not result["should_run"]:
            self._log(task_id, task, now, "skipped", None,
                      "跳过（被动触发器条件未全部满足）", tof, False, "scheduled", details, run_id)
            await self.store.save()
            await self._expire(task_id, task, time.time())
            return
        # Durably consume the time opportunity before external side effects.
        # Sending has no idempotency key: never automatically replay an uncertain send.
        await self._execute(task_id, task, now, tof, "scheduled", details, run_id)

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
            self._validate(task)
            if await self._expire(task_id, task, time.time()):
                raise ValueError("有效期已结束，请修改有效期后再触发")
            try:
                return await self._execute(task_id, task, time.time(), {}, "manual")
            except Exception as exc:
                await self._fault(task_id, exc, source="manual")
                raise

    async def _execute(self, task_id, task, now, tof, source, trigger_details=None, run_id=None):
        previous = self._states.get(task_id)
        self._states[task_id] = {"state": "running"}
        try:
            run_id = run_id or uuid.uuid4().hex
            definition = copy.deepcopy(task)
            definition["_run_id"] = run_id
            action, target_result, detail, success = await self.executor.execute(
                definition, now, source
            )
            if success:
                task["last_success_time"] = (target_result or {}).get("sent_at", time.time())
            self._log(task_id, task, now, action, target_result, detail, tof,
                      True if source == "scheduled" else None, source, trigger_details, run_id)
            await self.store.save()
            await self._expire(task_id, task, time.time())
            return action, detail
        finally:
            self._states[task_id] = previous or {"state": "waiting"}

    def _log(self, task_id, task, now, action, target_result, detail, tof, logic, source, trigger_details=None, run_id=None):
        self.store.append_log({
            "time": now, "task_id": task_id, "task_name": task.get("name", ""),
            "trigger_tof": tof, "logic_result": logic, "action": action,
            "trigger_labels": self.core.trigger_labels(task.get("triggers", [])),
            "target_result": target_result, "detail": detail, "source": source,
            "run_id": run_id or uuid.uuid4().hex,
            "trigger_details": copy.deepcopy(trigger_details or {}),
            "task_snapshot": copy.deepcopy(task),
        })
