from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "chatroom.db"
BACKUPS = ROOT / "backups"
LOGS = ROOT / "logs"
# 头像图片目录。必须和 app.py 里的 AVATAR_DIR 指向同一个地方
# （app.py 刻意不 import，两边各写一份；改一边就要改另一边）。
AVATARS = ROOT / "avatars"
PORT = int(os.getenv("CHAT_PORT", "9000"))
# 只有这些映像名 + 正监听本服务端口，才认定是聊天室服务，避免误杀同机的别的程序。
SERVER_IMAGE_NAMES = {"python.exe", "pythonw.exe"}
# ★ 调 netstat / tasklist / taskkill 时必须带上它。控制台窗口是以 pythonw 跑的（没有控制台），
#   不带这个标志的话 Windows 会给每个控制台子进程新建一个 conhost 黑窗 ——
#   状态轮询每 1.5 秒一次，就是屏幕上每 1.5 秒闪一个黑框。
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# 项目外那两个自动备份目录各保留几份（会一直堆积，必须给上限）。
# 两个前缀各留各的：清空聊天记录不会把重置备份挤掉。
RESET_BACKUP_KEEP = 5
CLEAR_BACKUP_KEEP = 5
# 「还原备份」前自动备份的保留份数（三族各留各的，互不挤占）。
RESTORE_BACKUP_KEEP = 5
# ★ 控制台「成员列表」里的重置密码把学生密码统一重置成这个。想换一个，只改这一行。
RESET_PASSWORD = "123456"

# 「成员列表」要读的 users 列 —— 显式白名单。
#
# ★★ 绝对不要改成 SELECT *：users 表里有 password_hash。图省事用了 *，将来表里再加一列，
#    就会顺手把密码哈希带进控制台界面、也带进导出的 CSV。名单和导出都只认这一份清单。
# ★ 这里只写「想读的列」，「库里实际有哪些列」要取交集（见 list_members）——
#   users 表是靠 _ensure_column() 一列列补上去的，老库里任何一列都可能还没补上。
MEMBER_COLUMNS = (
    "id", "username", "nickname", "role", "avatar", "level", "exp", "coins", "charm",
    "created_at", "real_name", "class_name", "bio", "badge_id", "violations",
    "muted_until", "kicked_until", "last_income_at", "inventory",
    "pet_type", "pet_hunger", "pet_thirst", "pet_cleanliness", "pet_updated_at",
)

# ★ 基础礼物（鲜花 / 掌声 / 飞吻 / 礼花）**不在 shop_items 表里** —— 它们是 app.py 里的
#   STARTER_ITEMS 常量，所以控制台这边必须留一份名字，否则成员列表上只会显示 basic_flower。
#   这两份名字天生重复（控制台不 import app.py）。改 app.py 的 STARTER_ITEMS 时要同步改这里：
#   tests/full_check.py 的 L1 会拿两边逐条比对，名字对不上就直接报红，忘不了。
KNOWN_ITEM_NAMES = {
    "basic_flower": "鲜花",
    "basic_applause": "掌声",
    "basic_kiss": "飞吻",
    "basic_fireworks": "礼花",
}

# 角色 → 中文。★ 与 app.py 的 role 取值保持一致（admin / user / guest）。
ROLE_NAMES = {"admin": "管理员", "user": "普通用户", "guest": "游客"}

# 「成员详情」和导出的 CSV **共用同一份字段清单** —— 一处定义、两处显示，
# 不会出现「窗口里看得到、导出的 CSV 里却没有」。
# 键名必须是 list_members() 真的会放进去的键；测试会核对这一点。
MEMBER_FIELDS = (
    ("昵称", "nickname"),
    ("账号", "username"),
    ("角色", "role_text"),
    ("等级", "level"),
    ("经验", "exp"),
    ("金币", "coins"),
    ("魅力", "charm"),
    ("真实姓名", "real_name"),
    ("班级", "class_name"),
    ("个性签名", "bio"),
    ("注册时间", "created_at"),
    ("最后发言", "last_spoke"),
    ("发言条数", "messages"),
    ("全站禁言", "muted_until_text"),
    ("禁入冷却", "kicked_until_text"),
    ("本房间禁言", "room_muted_text"),
    ("房主房间", "room_owner_text"),
    ("宠物（上次记录）", "pet_text"),
    ("物品栏", "items_text"),
    ("账号编号", "id"),
)


def setup_output() -> None:
    """让中文提示在 Windows 终端中使用 UTF-8 输出。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass


def listening_pids() -> set[int]:
    """只认本地地址正好以 :PORT 结尾且处于 LISTENING 的行，避免 :9000 误匹配到 :90001 之类的端口。"""
    if os.name != "nt":
        return set()
    result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, encoding="oem", errors="ignore", creationflags=NO_WINDOW)
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        if "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if len(parts) < 5 or not parts[1].endswith(f":{PORT}") or not parts[-1].isdigit():
            continue
        pids.add(int(parts[-1]))
    return pids


def pid_image_name(pid: int) -> str:
    """查进程映像名（小写），查不到返回空串。

    只认「以 .exe 结尾」的 CSV 首字段 —— 中文系统查不到进程时 tasklist 会输出
    「信息: 没有运行的任务匹配指定标准。」，那不是映像名。
    """
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
        capture_output=True, text=True, encoding="oem", errors="ignore", creationflags=NO_WINDOW,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return ""
    first = lines[0].split(",")[0].strip().strip('"').lower()
    return first if first.endswith(".exe") else ""


def service_pids() -> tuple[set[int], set[int]]:
    """返回 (确认是聊天室服务的 PID, 只是占了端口但映像名不像 Python 的 PID)。

    两道闸门缺一不可：端口独一无二，映像名保证不是同机别的程序。
    """
    ours: set[int] = set()
    others: set[int] = set()
    for pid in listening_pids():
        if pid_image_name(pid) in SERVER_IMAGE_NAMES:
            ours.add(pid)
        else:
            others.add(pid)
    return ours, others


def service_running() -> bool:
    return bool(service_pids()[0])


def port_responds() -> bool:
    """能不能连上本机端口（比 netstat 更贴近"网页打得开吗"）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.6)
    try:
        return probe.connect_ex(("127.0.0.1", PORT)) == 0
    except OSError:
        return False
    finally:
        probe.close()


def lan_ip() -> str:
    """本机在局域网里的地址；查不到就回落 127.0.0.1。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.settimeout(0.5)
        probe.connect(("10.255.255.255", 1))  # 只让系统选路由，不真的发包
        address = probe.getsockname()[0]
        if address and not address.startswith("127."):
            return address
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address and not address.startswith("127."):
                return address
    except OSError:
        pass
    return "127.0.0.1"


def start_server(log_path: Path | None = None) -> subprocess.Popen:
    """以无窗口子进程启动聊天室，输出重定向到日志文件（管道没人读会把服务勒死，文件最稳）。"""
    log_path = log_path or (LOGS / "server.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists() and log_path.stat().st_size > 2 * 1024 * 1024:
        try:
            log_path.replace(log_path.with_name(log_path.name + ".1"))
        except OSError:
            pass
    interpreter = Path(sys.executable)
    console = interpreter.with_name("python.exe")
    if console.exists():
        interpreter = console
    env = {**os.environ, "CHAT_PORT": str(PORT), "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    env.pop("PYTHONUTF8", None)
    handle = open(log_path, "ab")
    try:
        process = subprocess.Popen(
            [str(interpreter), str(ROOT / "app.py")],
            cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
            stdout=handle, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    finally:
        handle.close()
    return process


def stop_server(quiet: bool = False) -> int:
    ours, others = service_pids()
    stopped = 0
    for pid in sorted(ours):
        result = subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True, text=True, encoding="oem", errors="ignore", creationflags=NO_WINDOW)
        if result.returncode == 0:
            stopped += 1
    # 停完回读一次，别只信 taskkill 的返回码。
    left, _ = service_pids()
    if not quiet:
        if stopped:
            print(f"已强制关闭 {stopped} 个占用 {PORT} 端口的聊天室进程。")
        else:
            print(f"没有找到正在监听 {PORT} 端口的聊天室进程。")
        if left:
            print(f"警告：仍有 {len(left)} 个进程监听 {PORT} 端口，请稍后重试。")
        for pid in sorted(others):
            print(f"跳过 PID {pid}：它占着 {PORT} 端口但映像名不是 Python，不敢动它。")
    return stopped


def _snapshot_database(source: Path, target: Path) -> None:
    """SQLite 在线备份：会把还躺在 -wal 里的最新写入一起带上。

    直接复制主库文件是不行的 —— 最新写入可能还没落进主库，拷出来是个空库。
    """
    live = sqlite3.connect(source)
    snapshot = sqlite3.connect(target)
    try:
        live.backup(snapshot)
    finally:
        snapshot.close()
        live.close()


def _copy_avatars(dest: Path) -> int:
    """把头像图片复制进备份目录，返回复制了几张。

    ★ 头像文件放在项目根的 avatars/ 里（不在 data/ 下）。只备数据库的话，
      还原之后所有人的头像都会变回没图 —— 所以备份必须连它们一起备。
    README.txt 是目录自带的使用说明，不算用户图片，跳过。
    """
    if not AVATARS.is_dir():
        return 0
    files = [
        path for path in sorted(AVATARS.iterdir(), key=lambda item: item.name.lower())
        if path.is_file() and path.name.lower() != "readme.txt"
    ]
    if not files:
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    for path in files:
        shutil.copy2(path, dest / path.name)
    return len(files)


def create_backup(folder: Path) -> tuple[bool, str]:
    """备份数据库 + 头像图片，产出 backups/chatroom_时间/ 一个目录。

    服务运行中也能做（数据库走 SQLite 在线备份）。同一秒里连点两次不会撞名，
    第二份自动带 -2 后缀。目录里附一份「还原说明.txt」，照着做或者用控制台的
    「备份与恢复」点一下都行。
    """
    if not DB.exists():
        return False, "没有找到聊天数据库，请先启动一次聊天室。"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = folder / f"chatroom_{stamp}"
    index = 2
    while target.exists():
        target = folder / f"chatroom_{stamp}-{index}"
        index += 1
    try:
        target.mkdir(parents=True)
        _snapshot_database(DB, target / DB.name)
        images = _copy_avatars(target / AVATARS.name)
        (target / "还原说明.txt").write_text(
            "局域网互动聊天室 · 手工备份\n"
            f"备份时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"备份来源：{DB.parent}\n"
            "备份内容：聊天数据库（账号、金币、等级、道具、聊天记录）"
            + (f"、头像图片 {images} 张\n\n" if images else "（这个聊天室还没有头像图片）\n\n")
            + "怎么还原：\n"
            "打开「聊天室控制台.bat」，点「备份与恢复」，在列表里选中这一份，\n"
            "点「还原选中的备份」即可 —— 控制台会自动停服务、先把你现在的数据\n"
            "另存一份、再覆盖回来，不用手工拷文件。\n\n"
            "注意：这是按时间点整体回退 —— 备份之后新产生的聊天记录和账号都会消失。\n",
            encoding="utf-8",
        )
    except (sqlite3.Error, OSError) as error:
        return False, f"备份失败：{error}"
    return True, f"备份完成：{target}" + (f"（含头像图片 {images} 张）" if images else "")


def backup_database() -> int:
    ok, message = create_backup(BACKUPS)
    print(message)
    return 0 if ok else 1


def reset_backup_prefix() -> str:
    """★ 结尾那条下划线必须留在前缀里：它让前缀不可能匹配到项目本体。"""
    return f"{ROOT.name}_重置备份_"


def clear_backup_prefix() -> str:
    """清空聊天记录前的自动备份前缀。与「重置备份」分开，两边的保留策略互不干扰。"""
    return f"{ROOT.name}_清空记录备份_"


def restore_backup_prefix() -> str:
    """还原备份前的自动备份前缀。第三族，同样各留各的。"""
    return f"{ROOT.name}_还原前备份_"


def purge_old_backups(prefix: str, keep: int, protect: str = "") -> str:
    """项目外的自动备份会一直堆积，只按前缀保留最近 keep 份。

    清理只是收尾：删不掉只回报，绝不影响"备份已经成功"这个结论。
    """
    parent = ROOT.parent
    try:
        names = os.listdir(parent)
    except OSError:
        return ""
    candidates: list[tuple[str, Path]] = []
    for name in names:
        if not name.startswith(prefix) or name == protect:
            continue
        rest = name[len(prefix):]
        # 8 位日期戳闸门：挡住"同前缀但不同用途"的目录，也挡掉项目本体。
        if len(rest) < 8 or not rest[:8].isdigit():
            continue
        path = parent / name
        if path.is_dir():
            candidates.append((name, path))
    candidates.sort(key=lambda item: item[0], reverse=True)  # 新 → 旧
    budget = keep - (1 if protect else 0)
    removed: list[str] = []
    failed: list[str] = []
    for name, path in reversed(candidates[budget:]):  # 从旧到新删
        try:
            shutil.rmtree(path)
            removed.append(name)
        except OSError as error:
            failed.append(f"{name}（{error}）")
    lines = []
    if removed:
        lines.append("已清理过期的自动备份：" + "、".join(removed))
    if failed:
        lines.append("以下旧备份没能清理（可以手动删除）：" + "；".join(failed))
    return "\n".join(lines)


def _keep_count(keep: int | None, fallback: int) -> int:
    """keep 在调用时才读常量（写成默认参数会在定义时就绑死，改了常量不生效）。"""
    try:
        return max(1, int(keep if keep is not None else fallback))
    except (TypeError, ValueError):
        return fallback


def purge_old_reset_backups(keep: int | None = None, protect: str = "") -> str:
    return purge_old_backups(reset_backup_prefix(), _keep_count(keep, RESET_BACKUP_KEEP), protect)


def purge_old_clear_backups(keep: int | None = None, protect: str = "") -> str:
    return purge_old_backups(clear_backup_prefix(), _keep_count(keep, CLEAR_BACKUP_KEEP), protect)


def purge_old_restore_backups(keep: int | None = None, protect: str = "") -> str:
    return purge_old_backups(restore_backup_prefix(), _keep_count(keep, RESTORE_BACKUP_KEEP), protect)


def _backup_data(tag: str, purpose: str) -> tuple[str, str, str]:
    """把整个 data 目录备份到【项目外面】。返回 (备份目录, 错误, "")。

    tag     —— 目录名前缀里的标签，如「重置备份」「清空记录备份」
    purpose —— 写进「还原说明.txt」的用途说明
    备份失败就返回错误，调用方必须中止、一个文件都不删。
    """
    parent = ROOT.parent
    prefix = f"{ROOT.name}_{tag}_"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = parent / f"{prefix}{stamp}"
    index = 2
    while target.exists():  # 同一秒内连点两次也不会撞名
        target = parent / f"{prefix}{stamp}-{index}"
        index += 1
    source = ROOT / "data"
    try:
        target.mkdir(parents=True)
        if source.is_dir():
            # 数据库走 SQLite 在线备份（只拷主库会拷到空库），其余文件直接复制。
            shutil.copytree(source, target / "data", ignore=shutil.ignore_patterns("chatroom.db*"))
            if DB.exists():
                live = sqlite3.connect(DB)
                snapshot = sqlite3.connect(target / "data" / DB.name)
                try:
                    live.backup(snapshot)
                finally:
                    snapshot.close()
                    live.close()
        (target / "还原说明.txt").write_text(
            f"局域网互动聊天室 · {purpose}\n"
            f"备份时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"备份来源：{source}\n"
            "备份内容：data 目录（聊天数据库、注册账号、头像图片等）\n\n"
            "还原步骤：\n"
            "1. 双击「聊天室控制台.bat」，点「停止服务」。\n"
            "2. 把本目录里 data 文件夹的内容，覆盖回项目里的 data 文件夹。\n"
            "3. 回到控制台点「启动服务」。\n\n"
            "注意：这是按时间点整体回退——备份之后新产生的聊天记录和账号都会消失。\n",
            encoding="utf-8",
        )
    except Exception as error:  # noqa: BLE001 - 备份失败必须中止，不区分异常类型
        return "", f"自动备份失败，已中止（没有删除任何数据）：{error}", ""
    return str(target), "", ""


def _note_cleanup(directory: str, cleanup: str) -> None:
    """把清理了哪些旧备份写进新备份目录，以后翻到它也知道当时的收尾动作。"""
    if not cleanup:
        return
    try:
        (Path(directory) / "本次自动清理.txt").write_text(cleanup + "\n", encoding="utf-8")
    except OSError:
        pass


def backup_before_reset() -> tuple[str, str, str]:
    """重置前的自动备份，落在【项目外面】。返回 (备份目录, 错误, 清理说明)。"""
    directory, error, _ = _backup_data("重置备份", "重置前自动备份")
    if error:
        return "", error, ""
    cleanup = purge_old_reset_backups(protect=Path(directory).name)
    _note_cleanup(directory, cleanup)
    return directory, "", cleanup


def backup_before_clear() -> tuple[str, str, str]:
    """清空聊天记录前的自动备份，落在【项目外面】。返回 (备份目录, 错误, 清理说明)。"""
    directory, error, _ = _backup_data("清空记录备份", "清空聊天记录前自动备份")
    if error:
        return "", error, ""
    cleanup = purge_old_clear_backups(protect=Path(directory).name)
    _note_cleanup(directory, cleanup)
    return directory, "", cleanup


# 项目外自动备份的三种用途：(目录名前缀里的标签, 列表里显示的名字)。
# 加一种就要同时改这里和对应的 *_backup_prefix()。
AUTO_BACKUP_TAGS = (
    ("重置备份", "点「重置数据」前的自动备份"),
    ("清空记录备份", "点「清空聊天记录」前的自动备份"),
    ("还原前备份", "上次还原前的自动备份"),
)


def _auto_backup_dirs() -> list[tuple[str, Path]]:
    """扫项目外面，列出三族自动备份目录。返回 [(显示名, 目录)]。"""
    parent = ROOT.parent
    try:
        names = os.listdir(parent)
    except OSError:
        return []
    found: list[tuple[str, Path]] = []
    for name in names:
        for tag, label in AUTO_BACKUP_TAGS:
            prefix = f"{ROOT.name}_{tag}_"
            if not name.startswith(prefix):
                continue
            rest = name[len(prefix):]
            # 8 位日期戳闸门：挡住「同前缀但不同用途」的目录（也挡住项目本体）。
            if len(rest) < 8 or not rest[:8].isdigit():
                break
            path = parent / name
            if path.is_dir():
                found.append((label, path))
            break
    return found


def _backup_item(db: Path, group: Path, avatars, label: str) -> dict:
    """把一个备份整理成列表项。时间统一按数据库文件的修改时间来取。"""
    try:
        stat = db.stat()
        mtime, size = stat.st_mtime, stat.st_size
    except OSError:
        mtime, size = 0.0, 0
    return {
        "db": db,
        "group": group,
        "avatars": avatars,
        "label": label,
        "mtime": mtime,
        "size": size,
        "when": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "时间未知",
    }


def list_backups() -> list[dict]:
    """列出所有可以还原的备份，最新的排最前面。

    三个来源：
      · backups/chatroom_时间/      —— 「立即备份」生成的（数据库 + 头像）
      · backups/chatroom_时间.db    —— 老版本生成的单文件备份，仍然认
      · 项目外 <项目名>_XXX备份_时间/ —— 重置 / 清空 / 还原前自动生成的
    """
    items: list[dict] = []
    for label, path in _auto_backup_dirs():
        db = path / "data" / DB.name
        if db.exists():
            items.append(_backup_item(db, path, None, label))
    try:
        entries = list(BACKUPS.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if entry.is_dir():
            db = entry / DB.name
            if not db.exists():
                continue
            avatars = entry / AVATARS.name
            items.append(_backup_item(db, entry, avatars if avatars.is_dir() else None, "手工备份"))
        elif entry.is_file() and entry.suffix.lower() == ".db" and entry.name.startswith("chatroom_"):
            items.append(_backup_item(entry, entry, None, "手工备份（旧格式，不含头像）"))
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


def _verify_database(path: Path) -> tuple[bool, str]:
    """先复制到临时目录再打开看一眼，确认它是个能用的聊天室数据库。

    坏文件绝不能拿去覆盖现库 —— 那是唯一一份数据，盖了就没了。
    ★ 直接以只读方式打开 WAL 库要 -shm 配合，容易误报「打不开」，所以先拷进
      %TEMP% 再验（临时目录也在 %TEMP%，那里是原生删除行为）。
    """
    try:
        with tempfile.TemporaryDirectory(prefix="chatroom_verify_") as temp:
            probe_path = Path(temp) / "probe.db"
            shutil.copy2(path, probe_path)
            with closing(sqlite3.connect(probe_path)) as probe:
                names = {row[0] for row in probe.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
    except (sqlite3.Error, OSError) as error:
        return False, f"备份里的数据库打不开（{error}）"
    if "users" not in names:
        return False, "备份里的数据库不像是聊天室的库（没有 users 表）"
    return True, ""


def _cover_avatars(source: Path) -> int:
    """把备份里的头像图片覆盖回 avatars/，返回覆盖了几张。

    ★ 只覆盖、不删除：项目里原有的、这份备份里没有的图片保持不动。
      删用户文件是不可逆的，而多出来的头像只是列表里多几个选项，不影响使用。
    """
    AVATARS.mkdir(parents=True, exist_ok=True)
    copied = 0
    for path in sorted(source.iterdir(), key=lambda item: item.name.lower()):
        if path.is_file():
            shutil.copy2(path, AVATARS / path.name)
            copied += 1
    return copied


def restore_backup(group: Path) -> tuple[bool, str]:
    """把某一份备份还原回项目。返回 (是否成功, 说明)。

    顺序是死的，一步都不能换：
      校验 → 停服务 → 先把当前状态另存到项目外（失败即中止）
      → 数据库先写成临时文件 → 旧库三件套整体挪进隔离目录（预检 + 可回滚）
      → 换名就位 → 收回隔离目录 → 覆盖头像 → 如实回报。

    ★ 只覆盖 data/chatroom.db 而不处理 -wal/-shm 是不行的：SQLite 会把旧日志
      重放到新库上，表现成「还原了但数据还是老的」，甚至把库弄坏。
    ★★ 旧库是「整体改名让开位置」，不是挨个删：预检不过就一个字节都不动；
      改名改到一半失败会把已挪走的放回原处 —— 所以「当前数据没有任何改动」
      这句话是真的，而不是写死在提示里。
    """
    db_source = group / DB.name
    if not db_source.exists():
        db_source = group / "data" / DB.name
    if not db_source.exists():
        return False, f"这个备份里没有找到数据库文件：{group}"

    ok, error = _verify_database(db_source)
    if not ok:
        return False, f"{error}，已中止，当前数据没有任何改动。"

    # 还原前先把当前状态另存一份到项目外：万一选错了备份，还能退回来。
    safety_dir, backup_error, _ = _backup_data("还原前备份", "还原备份前自动备份")
    if backup_error:
        return False, backup_error
    cleanup = purge_old_restore_backups(protect=Path(safety_dir).name)
    _note_cleanup(safety_dir, cleanup)

    stop_server(quiet=True)

    # 先写成 .restoring 临时文件：这一步失败的话，旧库一个字节都还没动。
    staging = DB.with_name(DB.name + ".restoring")
    try:
        DB.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db_source, staging)
    except OSError as error:
        return False, f"把备份写进项目失败：{error}\n当前数据没有任何改动。"

    # 旧库三件套整体挪走（预检 + 失败可回滚）。确认它让开位置之后，才允许新库上台。
    moved, detail, trash = _step_aside_db_set()
    if not moved:
        remove_file_with_report(staging)
        if trash is None:
            return False, (
                detail
                + "\n\n请先点「停止服务」，再重新还原一次。本次还原没有生效，"
                "当前数据没有任何改动。"
                f"\n\n（刚才为稳妥另存的那一份在：{safety_dir}）"
            )
        return False, (
            detail
            + "\n\n请先点「停止服务」，再重新还原一次。本次还原没有生效。"
            f"\n\n（刚才为稳妥另存的那一份在：{safety_dir}）"
        )

    try:
        staging.replace(DB)
    except OSError as error:
        back_failed = _put_db_set_back(trash) if trash else ""
        if not back_failed:
            return False, (
                f"新库没能就位：{error}\n"
                "旧库已经放回原处，当前数据没有任何改动，可以再点一次还原。"
            )
        return False, (
            f"新库没能就位：{error}\n"
            f"旧库也没能放回原位：\n{back_failed}\n"
            f"旧库文件现在在：{trash}　请把它们挪回 {DB.parent}，"
            f"或用「{safety_dir}」里的还原前备份恢复。"
        )

    notes = f"\n\n· 还原前你的数据已另存到：\n{safety_dir}"
    if cleanup:
        notes += f"\n\n{cleanup}"
    if trash:
        notes += _drop_trash(trash)
    avatar_source = group / AVATARS.name
    if avatar_source.is_dir():
        try:
            copied = _cover_avatars(avatar_source)
        except OSError as error:
            notes += f"\n· 头像图片没能还原：{error}（数据库已经还原好了）"
        else:
            notes += f"\n· 头像图片已还原（{copied} 张，只覆盖不删除）"
    else:
        notes += "\n· 这份备份里没有头像图片，项目里的头像保持原样。"
    return True, "还原完成。现在点「启动服务」就能用了。" + notes


def restore_command(argv: list[str]) -> int:
    items = list_backups()
    if not items:
        print("没有找到任何可以还原的备份。先在控制台点一次「立即备份」。")
        return 1
    print("可以还原的备份（最新的排最前面）：")
    for index, item in enumerate(items, 1):
        tail = "，含头像" if item["avatars"] else ""
        print(f"  {index}. {item['when']}　{item['label']}　（{max(1, item['size'] // 1024)} KB{tail}）")
    choice = input(f"输入要还原的编号（1-{len(items)}），其他内容取消：").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(items):
        print("已取消，数据保持不变。")
        return 0
    picked = items[int(choice) - 1]
    print(f"\n即将还原：{picked['when']}　{picked['label']}\n来源：{picked['group']}")
    answer = input("还原会覆盖现在的数据（会先自动另存一份）。输入 Y 确认：").strip().lower()
    if answer != "y":
        print("已取消，数据保持不变。")
        return 0
    ok, message = restore_backup(picked["group"])
    print(message)
    return 0 if ok else 1


def remove_file_with_report(path: Path) -> str:
    """删文件；删不掉就把原因和"还在不在盘上"一起报出来，绝不静默。"""
    if not path.exists():
        return ""
    try:
        path.unlink()
    except OSError as error:
        return f"{path.name}：{error}（{'文件仍在盘上' if path.exists() else '文件已不在'}）"
    if path.exists():
        return f"{path.name}：调用删除后文件仍在盘上"
    return ""


def _db_sidecars() -> tuple[Path, ...]:
    """旧库「三件套」：主库 + WAL 的两个附属文件。

    ★ 必须三个一起处理。只覆盖 data/chatroom.db 而不删 -wal/-shm 是不行的：
      SQLite 会把旧日志重放到新库上，表现成「还原了但数据还是老的」，甚至把库弄坏。
    """
    return (DB, Path(f"{DB}-wal"), Path(f"{DB}-shm"))


def _preflight_clear(paths: tuple[Path, ...], attempts: int = 5, delay: float = 0.4) -> str:
    """在**一个字节都还没动**之前，先确认这些文件真的挪得动。通过返回空串。

    手法是同路径改名 `os.rename(p, p)`：文件空着就是空操作（通过），
    被别的进程占着就抛 OSError —— 而且**不会改动任何内容**。

    ★ 为什么要有这一步：服务是 taskkill /F 强杀的，进程没了不代表句柄马上释放
      （Windows 上偶尔要几十到几百毫秒，所以带重试）。在正式动手之前先探，
      探不过就直接返回 —— 只有这样，「当前数据没有任何改动」这句话才是真的。
    """
    for path in paths:
        if not path.exists():
            continue
        detail = ""
        for attempt in range(max(1, attempts)):
            try:
                os.rename(path, path)
                detail = ""
                break
            except OSError as error:
                detail = f"{path.name}：{error}"
                if attempt < attempts - 1:
                    time.sleep(delay)
        if detail:
            return detail
    return ""


def _step_aside_db_set() -> tuple[bool, str, Path | None]:
    """把旧库三件套**整体挪进隔离目录**（而不是挨个删掉）。
    返回 (是否已让开位置, 失败说明, 隔离目录)。

    ★★ 为什么不是直接删：删除是一个文件一个文件做的，删到一半失败就是
      「主库没了、-wal 还在」—— 这比对数据还糟（下次启动是个空库），
      而操作员收到的却是「重试一次」。改成整体改名之后：
        · 预检不过      → 一个字节都没动；
        · 改到一半失败  → 把已改名的**改回原处**，回到原样；
        · 三个都挪走了  → 才算旧库已让开位置。
      于是「要么成功、要么原样」，「没有任何改动」这句话才永远为真。

    返回的第三个值只在**回滚也失败**时不为 None（此时要如实告诉用户文件在哪）。
    """
    paths = tuple(path for path in _db_sidecars() if path.exists())
    if not paths:
        return True, "", None

    blocked = _preflight_clear(paths)
    if blocked:
        return False, ("以下旧库文件现在动不了（最常见原因：服务没停下来，文件被它占着）：\n"
                       + blocked), None

    try:
        trash = Path(tempfile.mkdtemp(prefix=".restore_trash_", dir=ROOT))
    except OSError as error:
        return False, f"建临时隔离目录失败：{error}", None

    moved: list[Path] = []
    for path in paths:
        try:
            path.rename(trash / path.name)
        except OSError as error:
            back_failed = []
            for done in moved:  # 回滚：把已经挪走的改回原位
                try:
                    done.rename(DB.parent / done.name)
                except OSError as back_error:
                    back_failed.append(f"{done.name}：{back_error}")
            if back_failed:
                return False, (f"挪走旧库文件失败：{path.name}：{error}\n"
                               "而且回滚也没能完成，旧库文件现在在隔离目录里，请手工挪回 "
                               f"{DB.parent}：\n{trash}\n" + "\n".join(back_failed)), trash
            shutil.rmtree(trash, ignore_errors=True)
            return False, (f"挪走旧库文件失败：{path.name}：{error}\n"
                           "（已把挪走的部分原样放回，当前数据没有任何改动。）"), None
        moved.append(trash / path.name)
    return True, "", trash


def _put_db_set_back(trash: Path) -> str:
    """把隔离目录里的旧库文件放回原位。返回放不回去的说明（空串 = 全部放回）。"""
    failed = []
    for item in sorted(trash.iterdir()):
        try:
            item.rename(DB.parent / item.name)
        except OSError as error:
            failed.append(f"{item.name}：{error}")
    if not failed:
        shutil.rmtree(trash, ignore_errors=True)
    return "\n".join(failed)


def _drop_trash(trash: Path) -> str:
    """新库已经就位之后，把隔离目录收掉。

    删不掉**不算还原失败**（旧库已经让开位置、新库能用了），只如实说一句文件在哪。
    """
    try:
        shutil.rmtree(trash)
    except OSError as error:
        return (f"\n· 旧库文件暂存在 {trash}，没能自动清掉（{error}）。"
                "它在项目下但不在 data 里，不影响使用，可以手工删除。")
    return ""


def perform_reset() -> tuple[bool, str]:
    """重置数据：先备份（失败即中止）→ 停服务 → 把旧库整体挪走 → 如实回报还剩什么。"""
    backup_dir, error, cleanup = backup_before_reset()
    if error:
        return False, error
    stop_server(quiet=True)
    # 与「还原备份」走同一条路径：先预检，再整体挪进隔离目录 —— 挪到一半失败会把文件
    # 放回原处，所以「当前数据没有删除」这句话才有依据（旧写法是挨个删，删一半就半毁）。
    moved, detail, trash = _step_aside_db_set()
    notes = f"\n\n本次数据已备份到：\n{backup_dir}"
    if cleanup:
        notes += f"\n\n{cleanup}"
    if not moved:
        warning = ("\n\n请先停止服务，再重新执行一次。本次重置没有生效，当前数据没有删除。"
                   if trash is None else
                   f"\n\n请先停止服务，再重新执行一次。旧库文件现在在：{trash}")
        return False, detail + warning + notes
    if trash:
        notes += _drop_trash(trash)
    return True, "重置完成，下次启动时会重新创建默认管理员账号。\n管理员账号：admin    密码：admin123" + notes


def message_count() -> tuple[int | None, str]:
    """看一眼有多少条聊天记录可清。返回 (条数, 错误)；表还不存在时条数为 None。"""
    try:
        with closing(open_db()) as db:
            if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'").fetchone() is None:
                return None, ""
            return db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], ""
    except sqlite3.Error as error:
        return None, (
            f"读取聊天记录失败：{error}\n"
            "常见原因：服务正在写入、数据库一时被锁住了，稍等几秒再点一次即可；没有任何数据被改动。"
        )


def perform_clear_messages() -> tuple[bool, str]:
    """清空聊天记录：先备份（失败即中止）→ 只删 messages 表的行 → 如实回报。

    只动聊天记录（含私聊、礼品/道具播报、活动播报）。
    注册账号、金币、等级、道具、房间、商城、进行中的红包/竞猜、房间活跃榜全部保留
    —— 那些要「重置数据」才会清掉。
    """
    if not DB.exists():
        return False, "没有找到聊天数据库，请先在控制台点「启动服务」跑一次，再来清空。"
    total, error = message_count()
    if error:
        return False, error
    if total is None:
        return True, "数据库里还没有聊天记录表（服务可能没启动过），没有需要清理的内容。"
    if total == 0:
        return True, "聊天记录本来就是空的，没有需要清理的内容（也没有生成备份）。"
    backup_dir, backup_error, cleanup = backup_before_clear()
    if backup_error:
        return False, backup_error
    notes = f"\n\n本次数据已备份到：\n{backup_dir}"
    if cleanup:
        notes += f"\n\n{cleanup}"
    try:
        with closing(open_db()) as db:
            deleted = db.execute("DELETE FROM messages").rowcount
            db.commit()
            # 删除只是把数据页标成空闲，文件体积不会自己变小。VACUUM 需要独占锁，
            # 服务跑着时多半拿不到 —— 拿不到不影响「记录已经清空」这个结论。
            try:
                db.execute("VACUUM")
                vacuumed = True
            except sqlite3.Error:
                vacuumed = False
    except sqlite3.Error as error:
        return False, (
            f"清空没能完成：{error}\n"
            "常见原因：服务正在写入、数据库一时被锁住了，稍等几秒再点一次即可；"
            "刚才的删除没有生效，聊天记录一条都没少。" + notes
        )
    return True, (
        f"已清空聊天记录（删除 {deleted} 条，清空前共 {total} 条）。\n"
        "· 注册账号、金币、等级、道具、房间活跃榜都保留，学生不用重新注册。\n"
        "· 学生浏览器里还留着旧记录，让他们按一次 F5 刷新就看不到旧消息了。\n"
        + ("· 数据库文件空间已回收。" if vacuumed else "· 数据库文件体积暂时没变小（服务运行中无法整理），不影响使用。")
        + notes
    )


def clear_messages_command(assume_yes: bool = False) -> int:
    if not assume_yes:
        print("警告：这会删除全部聊天记录（含私聊、礼品与活动播报）。")
        print("注册账号、金币、等级、道具不受影响（那些要 reset 才会清掉）。")
        answer = input("请输入 Y 确认清空，其他内容取消：").strip().lower()
        if answer != "y":
            print("已取消，聊天记录保持不变。")
            return 0
    ok, message = perform_clear_messages()
    print(message)
    return 0 if ok else 1


def reset_database(assume_yes: bool = False) -> int:
    if not assume_yes:
        print("警告：这会删除所有注册账号和聊天记录（操作前会自动备份到项目外面）。")
        answer = input("请输入 Y 确认初始化，其他内容取消：").strip().lower()
        if answer != "y":
            print("已取消初始化，原数据保持不变。")
            return 0
    ok, message = perform_reset()
    print(message)
    return 0 if ok else 1


def hash_password(password: str, salt: bytes | None = None) -> str:
    """★ 必须与 app.py 的 hash_password() 逐字节一致：PBKDF2-HMAC-SHA256、16 字节随机盐、
    12 万轮、存成「盐hex$摘要hex」。

    manage.py 刻意不 import app.py —— 控制台要在第三方依赖还没装好时就能打开。
    改动 app.py 的哈希算法时，这里必须跟着改，否则重置出来的密码学生登不上去。
    """
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        salt_hex, digest_hex = encoded.split("$", 1)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 120_000)
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def open_db() -> sqlite3.Connection:
    """控制台直连数据库。app.py 已经把库设成 WAL，读写可以并发，服务跑着也能改。"""
    db = sqlite3.connect(DB, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def list_students() -> list[dict]:
    """列出全部学生账号（不含管理员），新注册的排前面。

    库还没建出来（一次都没启动过）时返回空列表，不是错误。
    """
    if not DB.exists():
        return []
    # ★ 必须用 closing()：sqlite3 连接的 `with` 只管事务提交，不会关闭连接，
    #   不关就会在 Windows 上一直占着 .db 文件不放（重置数据时删不掉）。
    with closing(open_db()) as db:
        rows = db.execute(
            "SELECT username, nickname, created_at FROM users "
            "WHERE role IS NULL OR role <> 'admin' "
            "ORDER BY created_at DESC, id DESC"
        ).fetchall()
    return [{"username": row["username"], "nickname": row["nickname"], "created_at": row["created_at"]} for row in rows]


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    """这张表实际有哪些列。表不存在时返回空集合，不抛异常。"""
    try:
        return {str(row["name"]) for row in db.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _stamp(value: object) -> str:
    """unix 秒 → 「2026-10-05 22:40」。空值 / 负数 / 脏数据一律给空串。"""
    try:
        seconds = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    try:
        return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def _until_text(value: object, label: str) -> str:
    """把「到期时间戳」翻译成人话。

    ★ 一定要跟**现在**比：时间戳还在未来才是「还在禁言」，已经过去了就是「早就没在禁」。
      只把时间戳原样显示出来，老师会以为学生此刻还被禁着 —— 这类「看不出状态的原始值」
      比不显示更误事。
    """
    stamp = _stamp(value)
    if not stamp:
        return "—"
    try:
        remaining = float(value) - time.time()
    except (TypeError, ValueError):
        return stamp
    if remaining <= 0:
        return f"已结束（{stamp}）"
    minutes = int(remaining // 60)
    left = f"{minutes // 60} 小时 {minutes % 60} 分钟" if minutes >= 60 else f"{minutes} 分钟"
    return f"{label}中，到 {stamp}（还剩 {left}）"


def _inventory_counts(raw: object) -> dict[str, int]:
    """物品栏在库里是一个 JSON 字符串：{"物品 id": 数量}。

    两种形态都要认：新版是「id → 数量」的对象；很老的账号（以及建表默认值 '[]'）是
    「id 的数组」。解析不出来就当空物品栏 —— 这只是给人看的，不该因为一条脏数据
    让整个成员列表打不开。
    """
    if isinstance(raw, dict):
        data: object = raw
    elif isinstance(raw, (str, bytes)) and raw:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    else:
        return {}

    if isinstance(data, list):
        counts: dict[str, int] = {}
        for item in data:
            key = str(item)
            counts[key] = counts.get(key, 0) + 1
        return counts
    if isinstance(data, dict):
        out: dict[str, int] = {}
        for key, value in data.items():
            try:
                count = int(value)
            except (TypeError, ValueError):
                continue
            if count > 0:
                out[str(key)] = count
        return out
    return {}


def _item_name(item_id: str, catalog: dict[str, str]) -> str:
    """物品 id → 中文名。查不到就原样显示 id（看得见 id 也比显示空白强）。"""
    if item_id in catalog:
        return catalog[item_id]
    if item_id in KNOWN_ITEM_NAMES:
        return KNOWN_ITEM_NAMES[item_id]
    # 学生自己上传的头像，物品栏里就存成 /avatars/xxx.jpg 这种路径。
    if item_id.startswith("/avatars/"):
        return f"头像 {Path(item_id).stem}"
    return item_id


def _pet_text(row: dict) -> str:
    """宠物状态。★ 库里的饱食度是「上次落库那一刻」的值，之后会随时间掉，
    所以这里如实标注是上次记录，不假装是实时值（实时值要靠 app.py 的衰减公式算）。"""
    kind = str(row.get("pet_type") or "").strip()
    if not kind:
        return "—"
    parts = []
    for label, key in (("饥饿度", "pet_hunger"), ("口渴度", "pet_thirst"), ("清洁度", "pet_cleanliness")):
        try:
            parts.append(f"{label} {float(row.get(key) or 0):.0f}")
        except (TypeError, ValueError):
            parts.append(f"{label} ?")
    return f"{kind}（" + "，".join(parts) + "）"


def list_members() -> list[dict]:
    """列出**全部注册账号**（含管理员），能读到的信息一次给全。

    和 list_students() 的分工：那个只给「昵称 / 账号 / 注册时间」，是给命令行和旧弹窗的
    极简名单；这个给控制台的「成员列表」窗口，另外补上物品栏明细、宠物状态、禁言与踢出到期、
    发言条数与最后发言时间、在哪些房间是房主。

    几处刻意的设计：
    · **只 SELECT MEMBER_COLUMNS 白名单**（绝不含 password_hash）；
    · 取「白名单 ∩ 库里实际存在的列」—— 老库缺列只让那一栏显示「—」，不是整个窗口读不出来；
    · 附属表（shop_items / rooms / room_owners / room_mutes / messages）缺一张就跳过一张，
      新库刚建出来还没播种时也能用；
    · 游客（guest）本来就没有数据库记录，天然不在这里出现。
    """
    if not DB.exists():
        return []

    with closing(open_db()) as db:
        columns = _table_columns(db, "users")
        if not columns:
            return []
        wanted = [name for name in MEMBER_COLUMNS if name in columns]
        rows = db.execute(f"SELECT {', '.join(wanted)} FROM users ORDER BY id").fetchall()

        catalog: dict[str, str] = {}
        if {"id", "name"} <= _table_columns(db, "shop_items"):
            catalog = {str(row["id"]): str(row["name"]) for row in db.execute("SELECT id, name FROM shop_items")}

        room_names: dict[str, str] = {}
        if {"id", "name"} <= _table_columns(db, "rooms"):
            room_names = {str(row["id"]): str(row["name"]) for row in db.execute("SELECT id, name FROM rooms")}

        def room_label(room_id: object) -> str:
            key = str(room_id)
            return room_names.get(key, key)

        owns: dict[str, list[str]] = {}
        if {"room_id", "user_id"} <= _table_columns(db, "room_owners"):
            for row in db.execute("SELECT room_id, user_id FROM room_owners"):
                owns.setdefault(str(row["user_id"]), []).append(room_label(row["room_id"]))

        room_muted: dict[str, list[str]] = {}
        if {"room_id", "user_id"} <= _table_columns(db, "room_mutes"):
            for row in db.execute("SELECT room_id, user_id, until FROM room_mutes"):
                room_muted.setdefault(str(row["user_id"]), []).append(
                    f"{room_label(row['room_id'])}（{_stamp(row['until']) or '—'}）")

        counts: dict[str, int] = {}
        last_spoke: dict[str, str] = {}
        if _table_exists(db, "messages"):
            for row in db.execute(
                "SELECT sender_id, COUNT(*) AS n, MAX(created_at) AS last FROM messages GROUP BY sender_id"
            ):
                counts[str(row["sender_id"])] = int(row["n"] or 0)
                last_spoke[str(row["sender_id"])] = str(row["last"] or "")

    members: list[dict] = []
    for row in rows:
        data: dict = {name: row[name] for name in wanted}
        user_id = str(data.get("id") or "")
        role = str(data.get("role") or "user")
        data["role"] = role
        data["role_text"] = ROLE_NAMES.get(role, role)

        items = _inventory_counts(data.pop("inventory", ""))
        data["items"] = [{"id": key, "name": _item_name(key, catalog), "count": value}
                         for key, value in sorted(items.items())]
        data["items_text"] = "、".join(f"{item['name']} ×{item['count']}" for item in data["items"]) or "—"

        data["muted_until_text"] = _until_text(data.get("muted_until"), "全站禁言")
        data["kicked_until_text"] = _until_text(data.get("kicked_until"), "禁入冷却")
        data["room_muted_text"] = "、".join(room_muted.get(user_id, [])) or "—"
        data["room_owner_text"] = "、".join(owns.get(user_id, [])) or "—"
        data["pet_text"] = _pet_text(data)
        data["messages"] = counts.get(user_id)
        data["last_spoke"] = last_spoke.get(user_id, "") or "—"
        data["coins"] = data.get("coins")
        members.append(data)
    return members


def member_cell(value: object) -> str:
    """一个单元格 / 一行详情的文本。

    ★ 以 = + - @ 开头的内容前面补一个 ' —— Excel 打开 CSV 时会把它们当**公式**执行，
      这是「CSV 注入」。昵称、个性签名都是学生自己填的，不能当可信内容。
      （表头、物品名这些我们自己写的内容不受影响：它们不会以这些字符开头。）
    """
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "是" if value else "否"
    elif isinstance(value, float):
        text = f"{value:.0f}"
    else:
        text = str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def members_csv(rows: list[dict]) -> str:
    """成员列表 → CSV 文本。表头与「成员详情」用的是同一份 MEMBER_FIELDS，栏位不会对不上。

    交给 csv 模块处理引号 / 逗号 / 换行，不自己拼字符串。
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([header for header, _ in MEMBER_FIELDS])
    for row in rows:
        writer.writerow([member_cell(row.get(key)) for _, key in MEMBER_FIELDS])
    return buffer.getvalue()


def reset_student_password(username: str, new_password: str = "") -> tuple[bool, str]:
    """把某一个学生的密码重置掉。只动一行 users 记录，其他学生不受影响。

    ★ 不需要停服务：app.py 登录时是实时查库校验的，写完学生下一次登录立刻生效。
    """
    username = (username or "").strip()
    password = new_password or RESET_PASSWORD
    if not username:
        return False, "没有选中任何学生。"
    if not DB.exists():
        return False, "没有找到聊天数据库，请先启动一次聊天室。"

    encoded = hash_password(password)
    # 自检闸门：造出来的哈希必须能通过自己的校验函数，绝不允许把学生锁在门外。
    if not verify_password(password, encoded):
        return False, "生成的新密码没能通过自检，已中止（数据库没有任何变化）。"

    try:
        with closing(open_db()) as db:
            row = db.execute("SELECT nickname, role FROM users WHERE username = ?", (username,)).fetchone()
            if row is None:
                return False, f"没有找到账号「{username}」，可能已经被删掉了。"
            if (row["role"] or "") == "admin":
                return False, f"「{username}」是管理员账号，这里只重置学生密码。"
            db.execute("UPDATE users SET password_hash = ? WHERE username = ?", (encoded, username))
            db.commit()
            # 回读一次：不看 commit 有没有报错，看库里现在到底存的是什么。
            stored = db.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
    except sqlite3.Error as error:
        return False, f"重置失败：{error}"

    if stored is None or not verify_password(password, stored["password_hash"]):
        return False, "写入后回读校验没有通过，数据库可能没改成功，请再试一次。"
    nickname = row["nickname"] or username
    return True, f"「{nickname}」（{username}）的密码已重置为 {password}，现在就能用它登录。"


def reset_password_command(args: list[str]) -> int:
    """命令行入口：不带参数列出学生名单，带了就重置。控制台开不起来时的备用通道。"""
    if not args:
        students = list_students()
        if not students:
            print("还没有注册过学生账号。")
            return 0
        print(f"共 {len(students)} 个学生账号：")
        for student in students:
            print(f"  {student['username']}\t{student['nickname']}\t{student['created_at']}")
        print(f"\n用法：python manage.py passwd 用户名 [新密码]　（不写新密码就是 {RESET_PASSWORD}）")
        return 0
    ok, message = reset_student_password(args[0], args[1] if len(args) > 1 else "")
    print(message)
    return 0 if ok else 1


def start_info() -> int:
    print("===============================================")
    print("        局域网聊天室正在启动")
    print(f"        教师机本机：http://127.0.0.1:{PORT}")
    print(f"        学生机访问：http://{lan_ip()}:{PORT}")
    print("        管理员账号：admin    密码：admin123")
    print("===============================================")
    return 0


def status_info() -> int:
    ours, others = service_pids()
    if ours:
        print(f"服务运行中：PID {', '.join(str(pid) for pid in sorted(ours))}，端口 {PORT}")
    else:
        print(f"服务未运行（端口 {PORT} 空闲）。")
    for pid in sorted(others):
        print(f"注意：PID {pid} 占着 {PORT} 端口，但映像名不是 Python，没有处理它。")
    return 0


def main() -> int:
    setup_output()
    command = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if command == "start-info":
        return start_info()
    if command == "start":
        process = start_server()
        print(f"已启动聊天室服务（PID {process.pid}），学生机访问：http://{lan_ip()}:{PORT}")
        return 0
    if command == "status":
        return status_info()
    if command == "stop":
        return 0 if stop_server() >= 0 else 1
    if command == "backup":
        return backup_database()
    if command == "restore":
        return restore_command(sys.argv[2:])
    if command == "reset":
        return reset_database()
    if command == "clear-messages":
        return clear_messages_command()
    if command in {"passwd", "reset-password"}:
        return reset_password_command(sys.argv[2:])
    print("用法：python manage.py start-info | start | status | stop | backup | restore | reset | clear-messages | passwd [用户名] [新密码]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
