"""
scheduler_core.py — 增强计划任务插件的核心调度逻辑（纯函数，无 AstrBot 依赖）

本模块只负责：
  1. 触发器配置校验
  2. 主动触发器的下次触发时间计算与"是否到点"判定
  3. 被动触发器的实时真值（ToF, Trigger-or-False）求值
  4. 逻辑规则表达式（+ 表示或，* 表示且，支持括号）的解析、校验、求值
  5. 一次轮询中"合并评估"算法：收集所有到点主动触发器 → 评估逻辑规则

所有时间戳均为本地时间的 Unix 秒（与 AstrBot 现有插件保持一致）。
所有函数对非法输入返回安全默认值并尽量给出错误信息，绝不抛出未捕获异常。

触发器类型约定：
  interval  主动-周期型：从 base_time（基准时间）起，每 days+hours+minutes 累加间隔触发一次
  cron      主动-cron型：标准 5 段 cron 表达式（分 时 日 月 周）
  window    被动-区间型：每天 start~end 且当天星期几在 weekdays 中时为真
  random    被动-随机型：每次被引用时采样 U(0,1) < threshold 为真
  cooldown  被动-冷却型：now - task_last_success >= 冷却时长 为真
"""

from __future__ import annotations

import re
import math
import random as _random
from datetime import datetime
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

ACTIVE_TYPES = (T_INTERVAL, T_CRON)      # 主动型：自己到点触发
PASSIVE_TYPES = (T_WINDOW, T_RANDOM, T_COOLDOWN)  # 被动型：被逻辑规则引用时求值
ALL_TYPES = ACTIVE_TYPES + PASSIVE_TYPES


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


def _latest_fire_at_or_before(trigger: Dict[str, Any], now_ts: float) -> Optional[float]:
    """返回 <= now_ts 的最近一个主动触发点；若不存在（如 now 早于基准）返回 None。"""
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
        if now_ts < base_ts:
            return None
        elapsed = now_ts - base_ts
        n = int(math.floor(elapsed / interval))
        return base_ts + n * interval

    elif ttype == T_CRON:
        if not _HAS_CRONITER:
            return None
        expr = cfg.get("expr", "")
        try:
            cr = croniter(expr, _dt(now_ts + 1e-6))
            return _ts(cr.get_prev(datetime))
        except Exception:
            return None

    return None


def check_active_fire(trigger: Dict[str, Any], now_ts: float) -> Tuple[bool, Optional[float]]:
    """
    判断主动触发器在本次轮询窗口是否到点。
    返回 (是否触发, 触发点时间戳)。
    触发点时间戳用于更新 last_fired，确保不会重复触发也不会补发历史。

    策略：
      - 取 last_fired 之后的下一个触发点 candidate
      - 若 candidate 存在且 candidate <= now_ts，则触发；
        触发点取 <= now_ts 的最近触发点（避免宕机后连续补发）
      - 否则不触发
    """
    if trigger.get("type") not in ACTIVE_TYPES:
        return False, None

    last_fired = float(trigger.get("last_fired", 0.0) or 0.0)
    candidate = _next_fire_strictly_after(trigger, last_fired)
    if candidate is None or candidate > now_ts:
        return False, None

    # 在 (last_fired, now] 内有触发点被跨越；取最近的一个作为本次触发点
    fire_point = _latest_fire_at_or_before(trigger, now_ts)
    if fire_point is None:
        fire_point = candidate
    # 保证 fire_point 严格大于 last_fired（否则不应触发）
    if fire_point <= last_fired:
        return False, None
    return True, fire_point


def next_trigger_preview(triggers: List[Dict[str, Any]], now_ts: float) -> Optional[float]:
    """
    计算所有主动触发器中、基于 now_ts 的最近未来触发点，供页面预览。
    仅考虑主动触发器；被动触发器无法预测，故不参与。
    返回时间戳；若无主动触发器或无未来触发点返回 None。
    """
    best: Optional[float] = None
    for tg in triggers:
        if tg.get("type") not in ACTIVE_TYPES:
            continue
        nf = _next_fire_strictly_after(tg, now_ts)
        if nf is not None and nf > now_ts:
            if best is None or nf < best:
                best = nf
    return best


def next_fire_over_all(tasks: Dict[str, Any], now_ts: float) -> Optional[float]:
    """
    遍历所有任务的所有主动触发器，返回全局最近的下一个触发点（严格大于 now_ts）。
    供自适应调度主循环计算 sleep 时长。停用的任务、被动触发器不参与。
    """
    best: Optional[float] = None
    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        if not task.get("enabled", True):
            continue
        for tg in task.get("triggers", []):
            if not isinstance(tg, dict):
                continue
            if tg.get("type") not in ACTIVE_TYPES:
                continue
            lf = float(tg.get("last_fired", 0.0) or 0.0)
            nf = _next_fire_strictly_after(tg, lf)
            if nf is not None and nf > now_ts:
                if best is None or nf < best:
                    best = nf
    return best


def has_pending_fire(tasks: Dict[str, Any], now_ts: float) -> bool:
    """
    是否存在"已到点但尚未处理"的主动触发器（即 next_after(last_fired) <= now）。
    典型场景：任务配置刚变更（如改间隔），last_fired 继承旧值，新的触发点可能已过。
    此时应让调度循环立即 tick 处理，而非等待兜底周期。
    """
    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        if not task.get("enabled", True):
            continue
        for tg in task.get("triggers", []):
            if not isinstance(tg, dict):
                continue
            if tg.get("type") not in ACTIVE_TYPES:
                continue
            lf = float(tg.get("last_fired", 0.0) or 0.0)
            nf = _next_fire_strictly_after(tg, lf)
            if nf is not None and nf <= now_ts:
                return True
    return False


# ─────────────────────────────────────────────────────────────────────
# 被动触发器：实时真值
# ─────────────────────────────────────────────────────────────────────

def passive_tof(trigger: Dict[str, Any], now_ts: float, task_last_success: float) -> bool:
    """被动触发器实时求值。对主动触发器返回 False（不应被此函数调用）。"""
    ttype = trigger.get("type")
    cfg = trigger.get("config", {})

    if ttype == T_WINDOW:
        return _window_tof(cfg, now_ts)

    if ttype == T_RANDOM:
        try:
            th = float(cfg.get("threshold", 0.0))
        except (TypeError, ValueError):
            th = 0.0
        if th <= 0.0:
            return False
        if th >= 1.0:
            return True
        return _random.random() < th

    if ttype == T_COOLDOWN:
        hours = int(cfg.get("hours", 0))
        minutes = int(cfg.get("minutes", 0))
        cooldown = hours * 3600 + minutes * 60
        if cooldown <= 0:
            return True
        return (now_ts - float(task_last_success or 0.0)) >= cooldown

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
# 逻辑规则表达式：解析、校验、求值
# 表达式语法：数字（触发器 id） + 表示或 * 表示且 支持括号
# 优先级：* 高于 +（类比乘除 vs 加减）
# ─────────────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"\s*(\d+|[()+*])")


def _tokenize(expr: str) -> Optional[List[str]]:
    """将表达式切分为 token 列表；非法字符返回 None。"""
    tokens: List[str] = []
    pos = 0
    while pos < len(expr):
        if expr[pos].isspace():
            pos += 1
            continue
        m = _TOKEN_RE.match(expr, pos)
        if not m or m.start() != pos:
            return None
        tokens.append(m.group(1))
        pos = m.end()
    return tokens


class _Parser:
    """递归下降解析器，同时用于校验与求值。
    解析的同时直接求值（传入 tof_map）；校验时传入空 map 并忽略结果。"""

    def __init__(self, tokens: List[str], tof_map: Dict[int, bool], collect_ids: bool = False):
        self.tokens = tokens
        self.pos = 0
        self.tof_map = tof_map
        self.collect_ids = collect_ids
        self.ids: set = set()

    def _peek(self) -> Optional[str]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _next(self) -> Optional[str]:
        tok = self._peek()
        if tok is not None:
            self.pos += 1
        return tok

    # 所有 parse_* 方法返回 Optional[bool]：None 表示解析失败，否则为该子表达式的真值
    def parse_term(self):
        # term := factor ('*' factor)*
        val = self.parse_factor()
        if val is None:
            return None
        while self._peek() == "*":
            self._next()
            rhs = self.parse_factor()
            if rhs is None:
                return None
            val = val and rhs
        return val

    def parse_expr_v(self):
        # expr := term ('+' term)*
        val = self.parse_term()
        if val is None:
            return None
        while self._peek() == "+":
            self._next()
            rhs = self.parse_term()
            if rhs is None:
                return None
            val = val or rhs
        return val

    def parse_factor(self):
        tok = self._peek()
        if tok is None:
            return None
        if tok == "(":
            self._next()
            val = self.parse_expr_v()
            if val is None:
                return None
            if self._peek() != ")":
                return None
            self._next()
            return val
        if tok.isdigit():
            self._next()
            tid = int(tok)
            if self.collect_ids:
                self.ids.add(tid)
            return self.tof_map.get(tid, False)
        return None


def validate_logic_expr(expr: str, valid_ids: List[int]) -> Tuple[bool, str]:
    """校验逻辑表达式：语法合法 + 所有引用的 id 都存在于 valid_ids。"""
    if not isinstance(expr, str) or not expr.strip():
        return False, "逻辑规则不能为空"

    tokens = _tokenize(expr)
    if tokens is None:
        return False, "逻辑规则包含非法字符（仅允许数字、+、*、括号）"
    if not tokens:
        return False, "逻辑规则不能为空"

    # 先收集引用的 id（用全 False 的 tof_map 跑一遍解析）
    collector = _Parser(tokens, {}, collect_ids=True)
    if collector.parse_expr_v() is None or collector.pos != len(tokens):
        return False, "逻辑规则语法错误（检查括号匹配与运算符位置）"

    referenced = collector.ids
    valid_set = set(valid_ids)
    missing = referenced - valid_set
    if missing:
        return False, f"逻辑规则引用了不存在的触发器 id: {sorted(missing)}"
    if not referenced:
        return False, "逻辑规则必须至少引用一个触发器"

    return True, ""


def eval_logic_expr(expr: str, tof_map: Dict[int, bool]) -> bool:
    """对逻辑表达式求值。表达式应已通过 validate_logic_expr 校验。
    出于健壮性，任何解析异常都返回 False。"""
    tokens = _tokenize(expr)
    if tokens is None:
        return False
    parser = _Parser(tokens, tof_map)
    val = parser.parse_expr_v()
    if val is None:
        return False
    return bool(val)


# ─────────────────────────────────────────────────────────────────────
# 合并评估：一次轮询的核心
# ─────────────────────────────────────────────────────────────────────

def evaluate_task(triggers: List[Dict[str, Any]],
                  logic_expr: str,
                  now_ts: float,
                  task_last_success: float) -> Dict[str, Any]:
    """
    一次轮询中对单个任务进行合并评估。

    流程：
      1. 对每个主动触发器调用 check_active_fire，收集到点的主动触发器及其触发点
      2. 若无任何主动触发器到点，返回不触发（无需评估被动触发器）
      3. 构造 tof_map：到点的主动触发器=True，未到点的主动触发器=False，被动触发器实时求值
      4. eval_logic_expr 求逻辑结果
      5. 返回结构化结果（含各触发器 ToF、逻辑结果、需更新的 last_fired）

    返回 dict:
      {
        "should_run": bool,                 # 逻辑规则是否通过（是否应执行任务）
        "has_active_fire": bool,            # 是否有主动触发器到点
        "trigger_tof": {id: bool, ...},     # 各触发器本次评估的 ToF（用于日志）
        "fired_points": {id: float, ...},   # 到点主动触发器的触发点时间戳（用于更新 last_fired）
      }
    """
    result = {
        "should_run": False,
        "has_active_fire": False,
        "trigger_tof": {},
        "fired_points": {},
    }

    # 先校验触发器与逻辑表达式；非法则直接返回（不触发）
    valid_ids = []
    id_to_trigger: Dict[int, Dict[str, Any]] = {}
    for tg in triggers:
        ok, _ = validate_trigger(tg)
        if not ok:
            return result
        tid = tg.get("id")
        valid_ids.append(tid)
        id_to_trigger[tid] = tg

    ok, _ = validate_logic_expr(logic_expr, valid_ids)
    if not ok:
        return result

    # 收集到点的主动触发器
    fired_points: Dict[int, float] = {}
    for tg in triggers:
        if tg.get("type") not in ACTIVE_TYPES:
            continue
        fired, fp = check_active_fire(tg, now_ts)
        if fired:
            fired_points[tg["id"]] = fp

    if not fired_points:
        # 没有主动触发器到点，不触发；但仍记录各触发器 ToF 供调用方决定是否写日志
        # （通常无主动到点时不写日志，由调用方判断）
        result["trigger_tof"] = {tg["id"]: False for tg in triggers if tg.get("type") in ACTIVE_TYPES}
        return result

    result["has_active_fire"] = True
    result["fired_points"] = fired_points

    # 构造 tof_map
    tof_map: Dict[int, bool] = {}
    for tg in triggers:
        tid = tg["id"]
        ttype = tg.get("type")
        if ttype in ACTIVE_TYPES:
            tof_map[tid] = tid in fired_points
        else:
            tof_map[tid] = passive_tof(tg, now_ts, task_last_success)

    result["trigger_tof"] = tof_map
    result["should_run"] = eval_logic_expr(logic_expr, tof_map)
    return result


# ─────────────────────────────────────────────────────────────────────
# 动态参数模板渲染
# ─────────────────────────────────────────────────────────────────────

_PARAM_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def render_template(text: str, params: Dict[str, Any], now_ts: float) -> str:
    """
    将 {{key}} 形式的占位符替换为 params[key]。
    内置参数（params 中由调用方预先注入）：
      time      -> YYYY-MM-DD HH:MM:SS
      date      -> YYYY-MM-DD
      weekday   -> 周一/周二...
    若 text 不是字符串则原样返回。
    """
    if not isinstance(text, str):
        return text

    # 注入内置参数（不覆盖调用方传入的同名参数）
    now = _dt(now_ts)
    builtin = {
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "weekday": "周" + "一二三四五六日"[now.weekday()],
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
