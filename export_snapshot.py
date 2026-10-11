"""双击导出当前项目的 ZIP 快照，使用 Git 的忽略规则筛选文件。"""
import os
from pathlib import Path
import stat
import subprocess
import sys
from datetime import datetime
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def _git(project_dir: Path, *args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(project_dir), *args],
        input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode not in (0, 1) or (result.returncode == 1 and args[0] != "check-ignore"):
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip() or "Git 命令执行失败")
    return result


def snapshot_files(project_dir: Path) -> list[str]:
    """包含未提交修改及未被忽略的新文件，排除已删除文件与 Git 元数据。"""
    git_root = os.fsdecode(_git(project_dir, "rev-parse", "--show-toplevel").stdout).strip()
    if Path(git_root).resolve() != project_dir:
        raise RuntimeError("请把本脚本放在项目 Git 仓库的根目录")
    paths = set(_git(project_dir, "ls-files", "--cached", "--others", "--exclude-standard", "-z").stdout.split(b"\0"))
    paths.discard(b"")
    if not paths:
        return []
    # --no-index 让已跟踪但后来命中忽略规则的文件也不进入快照。
    ignored = set(_git(project_dir, "check-ignore", "--no-index", "--stdin", "-z",
                       input_bytes=b"\0".join(sorted(paths)) + b"\0").stdout.split(b"\0"))
    files = []
    for raw_path in sorted(paths - ignored):
        name = os.fsdecode(raw_path)
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or any(part.lower() == ".git" for part in relative.parts):
            continue
        source = project_dir / relative
        # 符号链接保存链接自身；目录（例如子模块）及已删除文件跳过。
        if source.is_symlink() or source.is_file():
            files.append(name)
    return files


def export_snapshot(project_dir: Path) -> tuple[Path, int]:
    project_dir = project_dir.resolve()
    files = snapshot_files(project_dir)
    if not files:
        raise RuntimeError("项目中没有可导出的文件")
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    stem = f"{project_dir.name}-{stamp}"
    number = 1
    while True:
        filename = stem + (f"-{number}" if number > 1 else "") + ".zip"
        output = project_dir.parent / filename
        try:
            stream = output.open("xb")
            break
        except FileExistsError:
            number += 1
    try:
        with stream, ZipFile(stream, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
            for name in files:
                source = project_dir / name
                if source.is_symlink():
                    info = ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    info.compress_type = ZIP_DEFLATED
                    archive.writestr(info, os.fsencode(os.readlink(source)))
                else:
                    archive.write(source, arcname=name)
    except BaseException:
        stream.close()
        output.unlink(missing_ok=True)
        raise
    return output, len(files)


def main() -> int:
    try:
        output, count = export_snapshot(Path(__file__).resolve().parent)
        print(f"已导出 {count} 个文件。\n快照：{output}")
        return 0
    except FileNotFoundError:
        print("导出失败：找不到 Git 或项目文件，请确认已安装 Git，且导出期间未删除文件。", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    exit_code = main()
    if os.name == "nt" and sys.stdin.isatty():
        try:
            input("按回车关闭窗口……")
        except (EOFError, KeyboardInterrupt):
            pass
    raise SystemExit(exit_code)
