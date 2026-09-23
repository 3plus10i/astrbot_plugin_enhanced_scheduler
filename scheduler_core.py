"""
scheduler_core.py — 增强计划任务插件的核心调度逻辑

本模块负责：
  1. 触发器配置校验
  2. 主动触发器的下次触发时间计算与"是否到点"判定
  3. 被动触发器的实时真值（ToF, Trigger-or-False）求值
  4. 一次轮询中"合并评估"算法：主动型之间取或，被动型之间取且

所有时间戳均为本地时间的 Unix 秒。
所有函数对非法输入返回安全默认值并尽量给出错误信息。

触发器类型约定：
  interval  主动-周期型：从 base_time（基准时间）起，每 days+hours+minutes 累加间隔触发一次
  cron      主动-cron型：标准 5 段 cron 表达式（分 时 日 月 周）
  window    被动-区间型：每天 start~end 且当天星期几在 weekdays 中时为真
  random    被动-随机型：每次求值时采样 U(0,1) < threshold 为真
  cooldown  被动-冷却型：now - task_last_success >= 冷却时长 为真
"""

from __future__ import annotations

import re
import math
import random as _random
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, List, Any

# croniter 仅在 cron 触发器中使用；若未安装则 cron 功能不可用但不影响其它类型
try:
    from croniter import croniter
    _HAS_CRONITER = True
except Exception:  # pragma: no cover
    _HAS_CRONITER = False


# ─────────────────────────────────────────────────────────────────────
# 类型常量
# ─────────────────────────────────────────────────────────────────────

T_INTERVAL = "interval"
T_CRON = "cron"
T_WINDOW = "window"
T_RANDOM = "random"
T_COOLDOWN = "cooldown"

ACTIVE_TYPES = (T_INTERVAL, T_CRON)      # 主动型：自己到点触发（多个之间取"或"）
PASSIVE_TYPES = (T_WINDOW, T_RANDOM, T_COOLDOWN)  # 被动型：被主动型唤起后求值（多个之间取"且"）
ALL_TYPES = ACTIVE_TYPES + PASSIVE_TYPES

# 下次触发时刻推进上限：每次推进都跨过一个未开启区间，正常几步内收敛
_PREVIEW_MAX_STEPS = 64


def _ts(dt: datetime) -> float:
    """datetime -> 本地时间戳"""
    return dt.timestamp()


def _dt(ts: float) -> datetime:
    """时间戳 -> 本地 datetime"""
    return datetime.fromtimestamp(ts)


def _parse_iso(s: Any) -> Optional[datetime]:
    """解析 ISO 时间字符串，支持 2026-01-01T00:00:00 / 2026-01-01 00:00:00 / 2026-01-01T00:00 / 2026-01-01 00:00 / 2026-01-01。"""
    if not isinstance(s, str):
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def day_start_iso(ts: float) -> str:
    """给定时间戳所在自然日 0 点，返回 ISO 字符串（YYYY-MM-DDTHH:MM:SS）。
    用于为 interval 触发器提供默认 base_time（创建当天的 0 点）。"""
    dt = datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0, microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# ─────────────────────────────────────────────────────────────────────
# 触发器配置校验
# ─────────────────────────────────────────────────────────────────────

def validate_trigger(trigger: Dict[str, Any]) -> Tuple[bool, str]:
    """校验单个触发器配置合法性。返回 (是否合法, 错误信息)。"""
    if not isinstance(trigger, dict):
        return False, "触发器必须是对象"

    tid = trigger.get("id")
    if not isinstance(tid, int) or tid < 1:
        return False, "触发器 id 必须是正整数"

    ttype = trigger.get("type")
    if ttype not in ALL_TYPES:
        return False, f"触发器类型必须是 {ALL_TYPES} 之一"

    cfg = trigger.get("config")
    if not isinstance(cfg, dict):
        return False, "触发器 config 必须是对象"

    if ttype == T_INTERVAL:
        days = cfg.get("days", 0)
        hours = cfg.get("hours", 0)
        minutes = cfg.get("minutes", 0)
        try:
            days, hours, minutes = int(days), int(hours), int(minutes)
        except (TypeError, ValueError):
            return False, "周期型的 days/hours/minutes 必须是整数"
        if days < 0 or hours < 0 or minutes < 0:
            return False, "周期型的 days/hours/minutes 不能为负"
        if days == 0 and hours == 0 and minutes == 0:
            return False, "周期型间隔必须大于 0（days/hours/minutes 至少一个 > 0）"
        base = cfg.get("base_time")
        if not isinstance(base, str) or not base.strip():
            return False, "周期型必须提供 base_time（基准时间，如 2026-01-01T00:00）"
        if _parse_iso(base) is None:
            return False, "base_time 不是合法的时间格式（如 2026-01-01T00:00）"

    elif ttype == T_CRON:
        if not _HAS_CRONITER:
            return False, "cron 触发器需要 croniter 库，但当前环境未安装"
        expr = cfg.get("expr")
        if not isinstance(expr, str) or not expr.strip():
            return False, "cron 表达式不能为空"
        if not _is_valid_cron(expr):
            return False, "cron 表达式不合法（应为 5 段：分 时 日 月 周）"

    elif ttype == T_WINDOW:
        start = cfg.get("start")
        end = cfg.get("end")
        if not _is_valid_hhmm(start):
            return False, "start 必须是 HH:MM 格式"
        if not _is_valid_hhmm(end):
            return False, "end 必须是 HH:MM 格式"
        weekdays = cfg.get("weekdays")
        if weekdays is None:
            pass
        elif not isinstance(weekdays, list):
            return False, "weekdays 必须是数组"
        else:
            for w in weekdays:
                if not isinstance(w, int) or w < 0 or w > 6:
                    return False, "weekdays 元素必须是 0-6（0=周一 … 6=周日）"

    elif ttype == T_RANDOM:
        th = cfg.get("threshold")
        try:
            th = float(th)
        except (TypeError, ValueError):
            return False, "随机型 threshold 必须是数字"
        if not (0.0 <= th <= 1.0):
            return False, "随机型 threshold 必须在 [0, 1] 范围内"

    elif ttype == T_COOLDOWN:
        hours = cfg.get("hours", 0)
        minutes = cfg.get("minutes", 0)
        try:
            hours, minutes = int(hours), int(minutes)
        except (TypeError, ValueError):
            return False, "冷却型的 hours/minutes 必须是整数"
        if hours < 0 or minutes < 0:
            return False, "冷却型的 hours/minutes 不能为负"
        if hours == 0 and minutes == 0:
            return False, "冷却时长必须大于 0"

    return True, ""


def _is_valid_hhmm(s: Any) -> bool:
    if not isinstance(s, str):
        return False
    m = re.fullmatch(r"\d{1,2}:\d{2}", s.strip())
    if not m:
        return False
    h, mm = s.split(":")
    return 0 <= int(h) <= 23 and 0 <= int(mm) <= 59


def _is_valid_cron(expr: str) -> bool:
    if not _HAS_CRONITER:
        return False
    try:
        # croniter.is_valid 是官方提供的静态校验
        return bool(croniter.is_valid(expr))
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────
# 主动触发器：下次触发时间
# ─────────────────────────────────────────────────────────────────────

def _interval_seconds(cfg: Dict[str, Any]) -> int:
    return int(cfg.get("days", 0)) * 86400 + int(cfg.get("hours", 0)) * 3600 + int(cfg.get("minutes", 0)) * 60


def _next_fire_strictly_after(trigger: Dict[str, Any], after_ts: float) -> Optional[float]:
    """返回严格大于 after_ts 的下一个主动触发点时间戳；被动型或无可行点返回 None。"""
    ttype = trigger.get("type")
    cfg = trigger.get("config", {})

    if ttype == T_INTERVAL:
        interval = _interval_seconds(cfg)
        if interval <= 0:
            return None
        base_dt = _parse_iso(cfg.get("base_time", ""))
        if base_dt is None:
            return None
        base_ts = _ts(base_dt)
        if after_ts < base_ts:
            return base_ts
        elapsed = after_ts - base_ts
        # 严格大于 after_ts 的下一个点
        n = int(math.floor(elapsed / interval)) + 1
        return base_ts + n * interval

    elif ttype == T_CRON:
        if not _HAS_CRONITER:
            return None
        expr = cfg.get("expr", "")
        try:
            # 以 after_ts 的本地 datetime 作为起点，取下一个
            cr = croniter(expr, _dt(after_ts + 1e-6))
            return _ts(cr.get_next(datetime))
        except Exception:
            return None

    return None


def _cooldown_ready_ts(cfg: Dict[str, Any], task_last_success: float) -> float:
    """冷却型最早可触发时刻 = 上次成功时间 + 冷却时长（冷却为 0 时视为已就绪）。"""
    cooldown = int(cfg.get("hours", 0) or 0) * 3600 + int(cfg.get("minutes", 0) or 0) * 60
    if cooldown <= 0:
        return 0.0
    return float(task_last_success or 0.0) + cooldown


def _next_window_open(cfg: Dict[str, Any], after_ts: float) -> Optional[float]:
    """返回严格晚于 after_ts 的下一个区间开启时刻；weekdays 限制下无可行日返回 None。"""
    start = cfg.get("start")
    if not _is_valid_hhmm(start):
        return None
    sh, sm = map(int, str(start).split(":"))
    weekdays = cfg.get("weekdays")
    day = _dt(after_ts)
    for _ in range(8):  # 一周内任意 weekday 组合，最多向后 8 天必覆盖
        if not weekdays or day.weekday() in weekdays:
            open_ts = day.replace(hour=sh, minute=sm, second=0, microsecond=0).timestamp()
            if open_ts > after_ts:
                return open_ts
        day += timedelta(days=1)
    return None


def _windows_all_open(windows: List[Dict[str, Any]], ts: float) -> bool:
    """所有区间触发器在 ts 时刻是否都已开启（无区间触发器视为已开启）。"""
    return all(_window_tof(tg.get("config", {}) or {}, ts) for tg in windows)


def _advance_window_cursor(windows: List[Dict[str, Any]], ts: float) -> Optional[float]:
    """把游标推进到「最早可能让所有区间同时开启」的时刻；无可行时刻返回 None。"""
    candidates = []
    for tg in windows:
        cfg = tg.get("config", {}) or {}
        if _window_tof(cfg, ts):
            continue
        nxt = _next_window_open(cfg, ts)
        if nxt is None:
            return None
        candidates.append(nxt)
    return max(candidates) if candidates else None


def next_trigger_preview(triggers: List[Dict[str, Any]],
                         now_ts: float,
                         task_last_success: float = 0.0) -> Optional[float]:
    """
    计算触发器组的下一次真正触发时刻（严格晚于 now_ts）。

    五类触发器里只有随机型不可预测；interval / cron / window / cooldown 均可稳定
    推算未来时刻，因此这里把非随机被动约束一并纳入：返回「任一主动触发器到点」
    且「所有区间已开启、所有冷却已到期」的最早时刻。随机型不参与计算。
    无主动触发器、或约束无解（如多个区间交集为空）返回 None。
    """
    actives = [tg for tg in triggers if tg.get("type") in ACTIVE_TYPES]
    if not actives:
        return None
    windows = [tg for tg in triggers if tg.get("type") == T_WINDOW]

    # 冷却约束：所有 cooldown 都到期才是可行下界（+1e-6 保证结果严格晚于 now_ts）
    cursor = now_ts + 1e-6
    for tg in triggers:
        if tg.get("type") == T_COOLDOWN:
            cursor = max(cursor, _cooldown_ready_ts(tg.get("config", {}) or {}, task_last_success))

    for _ in range(_PREVIEW_MAX_STEPS):
        point = None
        for tg in actives:
            nf = _next_fire_strictly_after(tg, cursor - 1e-6)
            if nf is not None and (point is None or nf < point):
                point = nf
        if point is None:
            return None
        if _windows_all_open(windows, point):
            return point
        nxt = _advance_window_cursor(windows, point)
        if nxt is None or nxt <= cursor:
            return None
        cursor = nxt
    return None


# ─────────────────────────────────────────────────────────────────────
# 单个触发器的未来触发点预览
# 逐个复用 _next_fire_strictly_after 推进，避免与调度主循环的计算逻辑漂移。
# 仅主动型（interval / cron）有「触发点」概念，故只对其求值。
# ─────────────────────────────────────────────────────────────────────

def future_fires(trigger: Dict[str, Any], now_ts: float, count: int = 3) -> List[float]:
    """返回主动触发器未来 count 个触发点时间戳（严格大于 now_ts）；被动型或无可行点返回空列表。"""
    fires: List[float] = []
    cursor = now_ts
    for _ in range(count):
        nf = _next_fire_strictly_after(trigger, cursor)
        if nf is None:
            break
        fires.append(nf)
        cursor = nf
    return fires


# ─────────────────────────────────────────────────────────────────────
# 被动触发器：实时真值
# ─────────────────────────────────────────────────────────────────────

def passive_tof(trigger: Dict[str, Any], now_ts: float, task_last_success: float) -> bool:
    """被动触发器实时求值。对主动触发器返回 False（不应被此函数调用）。"""
    ttype = trigger.get("type")
    cfg = trigger.get("config", {}) or {}

    if ttype == T_WINDOW:
        return _window_tof(cfg, now_ts)
    if ttype == T_RANDOM:
        try:
            threshold = float(cfg.get("threshold", 0.0))
        except (TypeError, ValueError):
            threshold = 0.0
        return _random.random() < threshold
    if ttype == T_COOLDOWN:
        cooldown = int(cfg.get("hours", 0) or 0) * 3600 + int(cfg.get("minutes", 0) or 0) * 60
        if cooldown <= 0:
            return True
        return max(0.0, now_ts - float(task_last_success or 0.0)) >= cooldown
    return False


def _window_tof(cfg: Dict[str, Any], now_ts: float) -> bool:
    start = cfg.get("start", "00:00")
    end = cfg.get("end", "23:59")
    weekdays = cfg.get("weekdays")

    now = _dt(now_ts)
    # 星期检查：weekdays 为空/None 视为不限制
    if weekdays:
        # Python weekday(): 0=周一 … 6=周日；与配置约定一致
        if now.weekday() not in weekdays:
            return False

    try:
        sh, sm = map(int, str(start).split(":"))
        eh, em = map(int, str(end).split(":"))
    except Exception:
        return False

    cur_min = now.hour * 60 + now.minute
    s_min = sh * 60 + sm
    e_min = eh * 60 + em

    # 区间端点精确到分钟；包含端点
    if s_min <= e_min:
        # 同日区间，如 08:00-20:00
        return s_min <= cur_min <= e_min
    else:
        # 跨天区间，如 22:00-06:00，解析为 [22:00, 23:59] ∪ [00:00, 06:00]
        return cur_min >= s_min or cur_min <= e_min


# ─────────────────────────────────────────────────────────────────────
# 合并评估：一次轮询的核心
# ─────────────────────────────────────────────────────────────────────

def evaluate_task(triggers: List[Dict[str, Any]],
                  now_ts: float,
                  task_last_success: float,
                  active_fire_at: Optional[float] = None) -> Dict[str, Any]:
    """
    一次轮询中对单个任务进行合并评估。固定语义：

      主动型之间取"或"：任一到点即视为时间条件成立
      被动型之间取"且"：全部为真时任务才执行
      主动与被动之间取"且"：即"（任一主动到点）且（所有被动为真）"

    流程：
      1. 校验触发器；非法则直接返回不触发
      2. 收集到点的主动触发器（无主动触发器或有主动但均未到点 → 不触发）
      3. 实时求值所有被动触发器
      4. should_run = 至少一个主动到点 and 所有被动为真

    active_fire_at 为本轮等待实际到达的主动触发点；未传入时不产生主动触发。

    返回 dict:
      {
        "should_run": bool,                 # 是否应执行任务
        "has_active_fire": bool,            # 是否有主动触发器到点
        "trigger_tof": {id: bool, ...},     # 各触发器本次评估的 ToF（用于日志）
      }
    """
    result = {
        "should_run": False,
        "has_active_fire": False,
        "trigger_tof": {},
    }

    # 先校验触发器；非法则直接返回（不触发）
    for tg in triggers:
        if not validate_trigger(tg)[0]:
            return result

    # 只有当前 worker 等待的 deadline 才能产生主动触发，不扫描历史时间点。
    fired_ids = set()
    if active_fire_at is not None and math.isfinite(float(active_fire_at)):
        for tg in triggers:
            if tg.get("type") not in ACTIVE_TYPES:
                continue
            next_fire = _next_fire_strictly_after(tg, float(active_fire_at) - 1e-6)
            if next_fire is not None and abs(next_fire - float(active_fire_at)) <= 1e-3:
                fired_ids.add(tg["id"])

    if not fired_ids:
        result["trigger_tof"] = {tg["id"]: False for tg in triggers if tg.get("type") in ACTIVE_TYPES}
        return result

    result["has_active_fire"] = True

    # 构造 tof_map：主动型按本轮 deadline 是否属于该触发器，被动型实时求值
    tof_map: Dict[int, bool] = {}
    passives_ok = True
    for tg in triggers:
        tid = tg["id"]
        if tg.get("type") in ACTIVE_TYPES:
            tof_map[tid] = tid in fired_ids
        else:
            tof = passive_tof(tg, now_ts, task_last_success)
            tof_map[tid] = tof
            passives_ok = passives_ok and tof

    result["trigger_tof"] = tof_map
    result["should_run"] = passives_ok
    return result


# ─────────────────────────────────────────────────────────────────────
# 提示词模板渲染（仅用于标准化的 <scheduled_task> 包裹模板）
# ─────────────────────────────────────────────────────────────────────

_PARAM_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def render_template(text: str, params: Dict[str, Any], now_ts: float) -> str:
    """
    将 {{key}} 形式的占位符替换为 params[key]，供固定包裹模板使用。
    内置参数：
      time -> YYYY年MM月DD日 星期X HH:MM:SS（含年月日、周几、时分秒）
    调用方通过 params 传入 {{task_prompt}}（任务提示词）与 {{holiday_clause}}（节假日子句）。
    替换值不再二次扫描，因此用户提示词中若出现 {{...}} 会被原样保留（用户提示词已不支持参数）。
    若 text 不是字符串则原样返回。
    """
    if not isinstance(text, str):
        return text

    # 注入内置参数（不覆盖调用方传入的同名参数）
    now = _dt(now_ts)
    builtin = {
        "time": now.strftime("%Y年%m月%d日 ") + "星期" + "一二三四五六日"[now.weekday()] + now.strftime(" %H:%M:%S"),
    }
    merged = dict(builtin)
    if isinstance(params, dict):
        merged.update(params)

    def _repl(m: re.Match) -> str:
        key = m.group(1)
        if key in merged:
            return str(merged[key])
        return m.group(0)  # 未知占位符保留原样

    return _PARAM_RE.sub(_repl, text)
