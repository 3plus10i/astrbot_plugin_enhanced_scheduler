"""Task and log persistence; all disk writes are serialized."""
import asyncio
import copy
import os
import re
import json
import time
import base64
import hashlib
from typing import Any, Dict, List, Optional, Tuple
from astrbot.api import logger

DATA_FILE_NAME = "tasks.json"
# 运行日志：每次触发/跳过都记一条，条目多且与任务定义无关，单独存一个文件
RUN_LOG_FILE_NAME = "run_logs.json"
# LLM 调用日志：一条记录一个文件（首行元数据 + 次行正文），图片按内容寻址单独存放
LLM_LOG_DIR_NAME = "llm_logs"
LLM_REC_DIR_NAME = "rec"
LLM_IMG_DIR_NAME = "img"
LEGACY_LLM_LOG_FILE_NAME = "llm_calls.jsonl"
# 旧版默认「独立 AI 系统提示」；该包裹文本已迁到任务提示词外层，迁移时与之一致的旧值清空
LEGACY_STANDALONE_SYSTEM_PROMPT = "<proactive_trigger>这是由计划任务自动触发的一次会话，现在时间是 {{time}}。请根据用户的指令进行回复。你的回复将被直接发送给用户。</proactive_trigger>"

class TaskStore:
    def __init__(self, data_dir, config, core):
        self.config = config
        self.core = core
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.data_file = os.path.join(data_dir, DATA_FILE_NAME)
        self.run_log_file = os.path.join(data_dir, RUN_LOG_FILE_NAME)
        self.llm_log_dir = os.path.join(data_dir, LLM_LOG_DIR_NAME)
        self.llm_rec_dir = os.path.join(self.llm_log_dir, LLM_REC_DIR_NAME)
        self.llm_img_dir = os.path.join(self.llm_log_dir, LLM_IMG_DIR_NAME)
        self.legacy_llm_log_file = os.path.join(data_dir, LEGACY_LLM_LOG_FILE_NAME)
        os.makedirs(self.llm_rec_dir, exist_ok=True)
        os.makedirs(self.llm_img_dir, exist_ok=True)
        self._lock = asyncio.Lock()
        self.data = {"tasks": {}}
        self.run_logs = []
        self._load_data_sync()
        self._load_run_logs_sync()
        self._migrate_targets()
        self._llm_seq = self._scan_llm_seq_io()
        self._migrate_legacy_llm_logs()

    @property
    def tasks(self):
        return self.data["tasks"]

    async def io(self, fn, *args):
        # Cancelling to_thread does not stop its thread. Keep the lock until it ends.
        async with self._lock:
            work = asyncio.create_task(asyncio.to_thread(fn, *args))
            return await self._finish_io(work)

    @staticmethod
    async def _finish_io(work):
        cancelled = False
        try:
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    cancelled = True
            return work.result()
        finally:
            if cancelled:
                raise asyncio.CancelledError()

    async def save(self, include_logs=True):
        async with self._lock:
            data = copy.deepcopy(self.data)
            logs = copy.deepcopy(self.run_logs) if include_logs else None
            work = asyncio.create_task(asyncio.to_thread(self._save_snapshot, data, logs))
            await self._finish_io(work)

    def _save_snapshot(self, data, logs):
        self._write_json(self.data_file, data)
        if logs is not None:
            self._write_json(self.run_log_file, logs)

    @staticmethod
    def _write_json(path, data):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _log_retention(self) -> int:
        try:
            v = int(self.config.get("log_retention", 100))
            return max(10, v)
        except (TypeError, ValueError):
            return 100

    def _llm_log_retention(self) -> int:
        try:
            v = int(self.config.get("llm_log_retention", 10))
            return max(10, v)
        except (TypeError, ValueError):
            return 10

    def _load_data_sync(self):
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if not isinstance(loaded, dict):
                    raise ValueError("任务文件顶层必须是对象")
                self.data = loaded
            except Exception as e:
                raise ValueError(f"读取任务文件失败，保留原文件: {e}") from e
        if not isinstance(self.data.get("tasks", {}), dict):
            raise ValueError("任务文件中的 tasks 必须是对象")
        self.data.setdefault("tasks", {})
        for task_id, task in list(self.tasks.items()):
            if not isinstance(task, dict):
                self.tasks[task_id] = {
                    "id": task_id, "name": task_id, "enabled": False,
                    "target": "", "triggers": [], "content": {},
                    "migration_notice": "旧任务格式错误，请重新配置", "invalid_data": task,
                }
        # 数据迁移：
        #   use_llm(bool) -> mode(fixed/standalone/conversation)
        #   取消逻辑组合 -> 删除 logic_expr（改由"主动或/被动且"固定语义）
        #   新增内容开关 time_aware；旧版默认独立 AI 系统提示（包裹文本）清空
        for task in self.data.get("tasks", {}).values():
            if not isinstance(task, dict):
                continue
            task.pop("logic_expr", None)
            content = task.get("content")
            if isinstance(content, dict):
                content["mode"] = _resolve_mode(content)
                content.pop("use_llm", None)
                if not isinstance(content.get("time_aware"), bool):
                    content["time_aware"] = True
                if not isinstance(content.get("holiday_aware"), bool):
                    content["holiday_aware"] = False
                if content.get("system_prompt") == LEGACY_STANDALONE_SYSTEM_PROMPT:
                    content["system_prompt"] = ""
        # 数据迁移：旧版 interval 触发器无 base_time -> 补为创建日 0 点
        for task in self.data.get("tasks", {}).values():
            if not isinstance(task, dict):
                continue
            try:
                created = float(task.get("created_at", 0.0) or 0.0)
            except (ValueError, TypeError):
                created = 0.0
            ref_ts = created if created > 0 else time.time()
            for tg in task.get("triggers", []) if isinstance(task.get("triggers"), list) else []:
                if not isinstance(tg, dict):
                    continue
                if tg.get("type") == "interval":
                    cfg = tg.get("config")
                    if isinstance(cfg, dict) and not str(cfg.get("base_time", "")).strip():
                        cfg["base_time"] = self.core.day_start_iso(ref_ts)

    def _migrate_targets(self):
        for task in self.tasks.values():
            if "targets" not in task:
                continue
            old = task.pop("targets")
            first = old[0] if isinstance(old, list) and old else ""
            task["target"] = first.strip() if isinstance(first, str) else ""
            if not isinstance(old, list) or len(old) > 1:
                task["enabled"] = False
                task["migration_notice"] = "旧多目标任务已停用，仅保留第一个发送目标，请编辑保存后重新启用"

    def _load_run_logs_sync(self):
        """加载运行日志；同时把旧版存在 tasks.json 里的 logs 迁移到独立文件。"""
        if os.path.exists(self.run_log_file):
            try:
                with open(self.run_log_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, list):
                    self.run_logs = [e for e in loaded if isinstance(e, dict)]
            except Exception as e:
                logger.error(f"[EnhancedScheduler] 读取运行日志失败，使用空日志: {e}")
        legacy = self.data.pop("logs", None)
        if legacy is None:
            return
        if isinstance(legacy, list):
            for entry in legacy:
                if isinstance(entry, dict):
                    self.append_log(entry)
        self._write_json(self.data_file, self.data)
        self._write_json(self.run_log_file, self.run_logs)
        logger.info(f"[EnhancedScheduler] 运行日志已从 tasks.json 迁移到 {RUN_LOG_FILE_NAME}")

    def append_log(self, entry: dict):
        """追加一条运行日志（仅内存）；持久化由调用方在一个批次结束时统一执行。"""
        self.run_logs.append(entry)
        # 截断保留最近 N 条
        retention = self._log_retention()
        if len(self.run_logs) > retention:
            del self.run_logs[: len(self.run_logs) - retention]

    def _write_llm_record_io(self, entry: dict) -> Tuple[str, dict]:
        """写入一条记录文件：抽离图片 -> 首行元数据 + 次行完整正文。返回 (文件名, 元数据)。"""
        body, images = _extract_images(entry, self.llm_img_dir)
        name = self._next_llm_record_name(entry)
        body_line = json.dumps(body, ensure_ascii=False)
        meta = _build_llm_meta(entry, name, images, len(body_line.encode("utf-8")))
        path = os.path.join(self.llm_rec_dir, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
            f.write(body_line + "\n")
        os.replace(tmp, path)
        return name, meta

    def _next_llm_record_name(self, entry: dict) -> str:
        """记录文件名 = 请求时间（毫秒精度，定宽）+ 任务名 + 递增序号。
        时间前缀定宽保证字典序即时间序，序号保证同毫秒同名不冲突。"""
        ts = float(entry.get("ts") or 0.0) or time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
        ms = int(round((ts - int(ts)) * 1000)) % 1000
        seq = self._llm_seq
        self._llm_seq += 1
        return f"{stamp}.{ms:03d}_{_safe_filename_part(str(entry.get('task_name') or 'task'))}_{seq:06d}.json"

    def _scan_llm_seq_io(self) -> int:
        """扫描现有记录文件名，取下一个可用序号（避免重启后重名）。"""
        max_seq = -1
        for name in self._list_llm_records_io():
            m = re.search(r"_(\d{6})\.json$", name)
            if m:
                max_seq = max(max_seq, int(m.group(1)))
        return max_seq + 1

    def _migrate_legacy_llm_logs(self):
        """把旧版单文件 llm_calls.jsonl 一次性拆分为逐条记录文件，完成后改名备份。"""
        if not os.path.exists(self.legacy_llm_log_file):
            return
        migrated = 0
        try:
            with open(self.legacy_llm_log_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(entry, dict):
                        self._write_llm_record_io(entry)
                        migrated += 1
            os.replace(self.legacy_llm_log_file, self.legacy_llm_log_file + ".migrated")
            logger.info(f"[EnhancedScheduler] 已迁移 {migrated} 条旧版 LLM 日志到逐条记录文件")
        except Exception as e:
            logger.error(f"[EnhancedScheduler] 迁移旧版 LLM 日志失败: {e}")

    def _list_llm_records_io(self) -> List[str]:
        """列出记录文件名，最新在前。"""
        try:
            names = [n for n in os.listdir(self.llm_rec_dir) if n.endswith(".json")]
        except Exception:
            return []
        names.sort(reverse=True)
        return names

    def _llm_record_path(self, name: str) -> Optional[str]:
        """白名单校验记录文件名后返回路径，防路径穿越。"""
        if not name or not _REC_NAME_RE.match(name):
            return None
        return os.path.join(self.llm_rec_dir, name)

    def _trim_llm_records_io(self):
        """超过 2 倍保留量时裁剪到最近 retention 条，并回收不再被引用的图片。"""
        retention = self._llm_log_retention()
        names = self._list_llm_records_io()
        if len(names) <= retention * 2:
            return
        for name in names[retention:]:
            try:
                os.remove(os.path.join(self.llm_rec_dir, name))
            except Exception:
                pass
        self._gc_llm_images_io()

    def _gc_llm_images_io(self):
        """删除未被任何存活记录引用的图片（存活集合取自记录文件首行元数据）。"""
        alive = set()
        for name in self._list_llm_records_io():
            meta = self._read_llm_meta_io(name)
            for img in (meta or {}).get("images") or []:
                fn = img.get("name") if isinstance(img, dict) else None
                if fn:
                    alive.add(fn)
        try:
            for fn in os.listdir(self.llm_img_dir):
                if fn not in alive:
                    try:
                        os.remove(os.path.join(self.llm_img_dir, fn))
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"[EnhancedScheduler] 回收 LLM 日志图片失败: {e}")

    def _read_llm_meta_io(self, name: str) -> Optional[dict]:
        """只读记录文件首行元数据（列表与预览用，不解析正文）。"""
        path = self._llm_record_path(name)
        if path is None:
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                meta = json.loads(f.readline())
            return meta if isinstance(meta, dict) else None
        except Exception:
            return None

    def _llm_log_page_io(self, offset: int, limit: int) -> Tuple[int, List[dict]]:
        """按文件名倒序取一页元数据。返回 (总数, 当页元数据列表)。"""
        names = self._list_llm_records_io()
        metas = []
        for name in names[offset : offset + limit]:
            meta = self._read_llm_meta_io(name)
            if meta is not None:
                metas.append(meta)
        return len(names), metas

    def _read_llm_record_io(self, name: str) -> Optional[Tuple[dict, dict]]:
        """读取单条记录：返回 (元数据, 正文)；文件不存在或已被裁剪返回 None。"""
        path = self._llm_record_path(name)
        if path is None or not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                meta = json.loads(f.readline())
                body = json.loads(f.readline())
            if not isinstance(meta, dict) or not isinstance(body, dict):
                return None
            return meta, body
        except Exception as e:
            logger.error(f"[EnhancedScheduler] 读取 LLM 记录失败 {name}: {e}")
            return None

    def _read_llm_image_io(self, name: str) -> Optional[str]:
        """按内容寻址读取单张图片并转成 data URL（前端按 md5 缓存复用）。

        文件名以 md5 为准：请求的文件名不存在时，回退到图片池中同一 md5 的实际文件
        （扩展名以磁盘上的为准），避免历史标记里的扩展名与文件名不一致时取不到图。
        """
        if not _IMG_NAME_RE.match(name or ""):
            return None
        path = os.path.join(self.llm_img_dir, name)
        if not os.path.exists(path):
            path = self._find_llm_image_io(name.split(".", 1)[0]) or ""
            if not path:
                return None
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except Exception:
            return None
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:{_image_mime(os.path.basename(path))};base64,{encoded}"

    def _find_llm_image_io(self, md5: str) -> Optional[str]:
        """按 md5 在图片池里找实际文件；不存在返回 None。"""
        if not re.fullmatch(r"[0-9a-f]{32}", md5 or ""):
            return None
        try:
            for fn in os.listdir(self.llm_img_dir):
                if fn.startswith(md5 + "."):
                    return os.path.join(self.llm_img_dir, fn)
        except Exception:
            pass
        return None

    def _clear_llm_logs_io(self):
        try:
            for directory in (self.llm_rec_dir, self.llm_img_dir):
                for fn in os.listdir(directory):
                    try:
                        os.remove(os.path.join(directory, fn))
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"[EnhancedScheduler] 清空 LLM 日志失败: {e}")

    async def append_llm_log(self, entry):
        await self.io(self._append_llm_log_io, copy.deepcopy(entry))

    def _append_llm_log_io(self, entry):
        self._write_llm_record_io(entry)
        self._trim_llm_records_io()


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


_B64_IMG_RE = re.compile(r"data:image/[\w.+-]+;base64,[A-Za-z0-9+/=_-]+")
# 记录文件名白名单（含中文/字母/数字/下划线/连字符/点），防路径穿越
_REC_NAME_RE = re.compile(r"^[\w.\-]+\.json$")
# 图片文件名白名单：<md5>.<ext>
_IMG_NAME_RE = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]{2,5}$")
_EXT_RE = re.compile(r"^[a-z0-9]{2,5}$")
_IMAGE_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}


def _safe_filename_part(text: str, limit: int = 30) -> str:
    """把任务名清理成文件名安全片段（保留中文/字母/数字/下划线/连字符）。"""
    s = re.sub(r"[^\w-]+", "_", text).strip("_")
    return s[:limit] or "task"


def _image_ext(mime: str) -> str:
    ext = mime.split("/", 1)[-1].split(";")[0].strip().lower()
    return ext if _EXT_RE.match(ext) else "bin"


def _image_mime(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower()
    return _IMAGE_MIME_BY_EXT.get(ext, f"image/{ext}")


def _extract_images(obj, img_dir: str) -> Tuple[Any, List[dict]]:
    """递归抽出记录中的 base64 图片：落盘为 img/<md5>.<ext>（已存在则复用，实现去重），
    原文替换为 [img:<md5>.<ext>:<字节数>] 占位标记。解码失败的段落原样保留，不丢数据。"""
    images: List[dict] = []
    return _walk_extract_images(obj, img_dir, images), images


def _walk_extract_images(obj, img_dir: str, images: List[dict]):
    if isinstance(obj, str):
        if "data:image/" not in obj:
            return obj

        def _sub(m):
            data_url = m.group(0)
            mime, _, b64 = data_url.partition(";base64,")
            if not b64:
                return data_url
            try:
                # 兼容 url-safe base64 字符集并按需补齐 padding
                raw = base64.b64decode(b64.translate(str.maketrans("-_", "+/")) + "=" * (-len(b64) % 4))
            except Exception:
                return data_url
            if not raw:
                return data_url
            md5 = hashlib.md5(raw).hexdigest()
            name = f"{md5}.{_image_ext(mime)}"
            path = os.path.join(img_dir, name)
            if not os.path.exists(path):
                try:
                    with open(path, "wb") as f:
                        f.write(raw)
                except Exception:
                    return data_url
            if not any(i["name"] == name for i in images):
                images.append({"name": name, "md5": md5, "size": len(raw)})
            return f"[img:{name}:{len(raw)}]"

        return _B64_IMG_RE.sub(_sub, obj)
    if isinstance(obj, dict):
        return {k: _walk_extract_images(v, img_dir, images) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_extract_images(v, img_dir, images) for v in obj]
    return obj


def _llm_preview(entry: dict) -> str:
    """卡片预览：优先请求提示词，其次回复正文，最后错误信息；压成单行并截断。"""
    req = entry.get("request") or {}
    resp = entry.get("response") or {}
    raw = req.get("prompt") or resp.get("completion_text") or entry.get("error") or ""
    return " ".join(str(raw).split())[:200]


def _build_llm_meta(entry: dict, name: str, images: List[dict], text_bytes: int) -> dict:
    """记录文件首行元数据：列表与未展开卡片预览所需的全部轻量字段。"""
    resp = entry.get("response") or {}
    return {
        "name": name,
        "ts": entry.get("ts", 0),
        "time": entry.get("time", ""),
        "task_id": entry.get("task_id", ""),
        "task_name": entry.get("task_name", ""),
        "source": entry.get("source", "scheduled"),
        "mode": entry.get("mode", ""),
        "umo": entry.get("umo", ""),
        "ok": bool(entry.get("ok")),
        "duration_ms": entry.get("duration_ms", 0),
        "error": entry.get("error"),
        "usage": resp.get("usage") if isinstance(resp, dict) else None,
        "text_bytes": text_bytes,
        "image_bytes": sum(int(i.get("size") or 0) for i in images),
        "images": images,
        "preview": _llm_preview(entry),
    }

