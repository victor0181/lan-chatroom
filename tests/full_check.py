"""局域网互动聊天室 · 全量检查（四层）

用法（在项目根目录，用虚拟环境的解释器跑）：

    .venv\\Scripts\\python.exe tests\\full_check.py               跑全部四层
    .venv\\Scripts\\python.exe tests\\full_check.py --only L1     只跑静态检查（1 秒内，改完文案先过这道）
    .venv\\Scripts\\python.exe tests\\full_check.py --only L1,L3  自己挑要跑哪几层
    .venv\\Scripts\\python.exe tests\\full_check.py --no-gui      没有桌面环境（远程/无头）时跳过控制台层
    .venv\\Scripts\\python.exe tests\\full_check.py --keep-temp   保留临时目录，失败后好翻现场

    L1 静态     语法编译 / 顶层重复定义 / bat 纯 ASCII / 前端产物引用 / 词库与 DEFAULT_WORDLIST 一致
    L2 单元     tests\\test_*.py 里的全部用例（新加测试文件会自动带上，不用改这里）
    L3 端到端   独立端口 + 临时数据目录起真实服务：HTTP 探针 + WebSocket 收发 + 违禁词 + 重置密码回归
    L4 控制台   主窗口按钮布局 + 重置密码弹窗

耗时参考（本机实测）：L1 不到 1 秒，L3 约 6 秒，L4 约 4 秒，**L2 约 40 秒**——
单元测试里要跑 PBKDF2 十二万轮和真实的等待，是最慢的一层。想看进度就看每层的耗时。

★ 三条硬保证：
  1. 全程不碰 data\\chatroom.db —— 服务和数据库都在临时目录里，跑完删掉；
  2. 不占用 9000 端口 —— 端口自动挑一个空闲的（也可以用 --port 指定）；
  3. 退出码 0=全过、1=有失败，可以直接拿去做发版闸门。

退出码：0 全过 / 1 有失败 / 2 参数或环境不对（比如没用虚拟环境的解释器）。
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import manage  # noqa: E402  （控制台/数据库的底层能力都用它，跟 launcher.py 同一个来源）

# 中文输出要能落到 Windows 终端里（cmd 默认 GBK，会炸）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # pragma: no cover - 老 Python 才没有
    pass

LINE = "-" * 64
RESULTS: list[tuple[str, str, str, str]] = []   # (层, 名称, PASS/FAIL/SKIP, 说明)
SECTION = {"name": "", "start": 0.0}
SECTION_SECONDS: dict[str, float] = {}          # 层名 -> 累计耗时（汇总时打出来，好看出哪层慢）


# ---------------------------------------------------------------- 记账小工具
def stop_section() -> None:
    if SECTION["name"]:
        SECTION_SECONDS[SECTION["name"]] = (
            SECTION_SECONDS.get(SECTION["name"], 0.0) + time.time() - SECTION["start"]
        )


def head(title: str) -> None:
    stop_section()
    SECTION["name"] = title
    SECTION["start"] = time.time()
    print(f"\n{LINE}\n{title}\n{LINE}", flush=True)


def check(name: str, ok: object, detail: str = "") -> bool:
    ok = bool(ok)
    RESULTS.append((SECTION["name"], name, "PASS" if ok else "FAIL", detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   -> {detail}" if detail else ""), flush=True)
    return ok


def skip(name: str, reason: str) -> None:
    """跳过不算通过也不算失败，但必须显式打出来 —— 悄悄不跑的检查等于没有检查。"""
    RESULTS.append((SECTION["name"], name, "SKIP", f"跳过：{reason}"))
    print(f"  [SKIP] {name}   -> {reason}", flush=True)


def source_files() -> list[Path]:
    """根目录 + tests 下的全部 .py。用发现而不是写死清单，新加文件自动纳入检查。"""
    found = sorted(ROOT.glob("*.py")) + sorted(TESTS.glob("*.py"))
    return [path for path in found if path.is_file()]


def free_port() -> int:
    """让系统给一个空闲端口，避免和真实服务（9000）或别人的测试撞车。"""
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


# ---------------------------------------------------------------- 污染核对
# ★ 这些东西是「老师自己维护的项目文件」，测试只许读、不许写。
#   本轮真踩过：test_announcement 的 init_db() 写在目录隔离之前，
#   于是一次跑测试就在项目根目录生成了一份 公告/lobby.txt。
#   靠人眼看是看不出来的（文件长得挺正常），所以拍快照 + 逐项比对。
POLLUTION_WATCH = ("公告.txt", "违禁词库.txt", "公告")


def pollution_snapshot() -> dict[str, int | None]:
    """给这几处拍一张「文件清单 + 修改时间」的快照。None 表示当时不存在。"""
    snapshot: dict[str, int | None] = {}

    def mark(path: Path) -> None:
        try:
            snapshot[path.relative_to(ROOT).as_posix()] = path.stat().st_mtime_ns
        except OSError:
            snapshot[path.relative_to(ROOT).as_posix()] = None

    announce_dir = ROOT / "公告"
    if announce_dir.is_dir():
        for item in sorted(announce_dir.glob("*.txt")):
            mark(item)
    else:
        mark(announce_dir)
    mark(ROOT / "公告.txt")
    mark(ROOT / "违禁词库.txt")
    return snapshot


def pollution_diff(before: dict[str, int | None]) -> list[str]:
    """比对快照，返回「新增 / 消失 / 被改写」三项人话描述（空列表=干净）。"""
    after = pollution_snapshot()
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    touched = sorted(name for name in set(before) & set(after) if before[name] != after[name])
    report: list[str] = []
    if added:
        report.append("多出：" + "、".join(added))
    if removed:
        report.append("少了：" + "、".join(removed))
    if touched:
        report.append("被改写：" + "、".join(touched))
    return report


# ================================================================ L1
def layer_static() -> dict:
    head("L1 · 静态检查")
    sources: dict[str, bytes] = {}

    for path in source_files():
        rel = path.relative_to(ROOT).as_posix()
        data = path.read_bytes()
        sources[rel] = data
        try:
            compile(data, str(path), "exec")
            check(f"语法编译 {rel}", True)
        except SyntaxError as error:
            check(f"语法编译 {rel}", False, f"第 {error.lineno} 行 {error.msg}")

    for rel, data in sources.items():
        try:
            tree = ast.parse(data)
        except SyntaxError:  # 上面已经报过了，这里不重复刷屏
            continue
        seen: set[str] = set()
        duplicated: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in seen:
                    duplicated.append(node.name)
                seen.add(node.name)
        check(f"{rel} 顶层无重复定义", not duplicated,
              ("重复：" + "、".join(duplicated)) if duplicated else "")

    bats = sorted(ROOT.glob("*.bat"))
    check("根目录有 .bat 启动脚本", bool(bats), "" if bats else "一个都没有")
    for path in bats:
        raw = path.read_bytes()
        bad = sum(1 for byte in raw if byte > 127)
        check(f"{path.name} 全 ASCII（混中文会让整行报错）", not bad,
              f"{bad} 个非 ASCII 字节" if bad else f"{len(raw)} 字节")

    index_html = ROOT / "static" / "dist" / "index.html"
    if check("前端产物 static/dist/index.html 存在", index_html.exists(),
             "缺失，需要先 npm run build" if not index_html.exists() else ""):
        html = index_html.read_text(encoding="utf-8", errors="replace")
        assets = sorted(set(re.findall(r"assets/([\w.\-]+)", html)))
        check("前端产物 index.html 引用了资源", bool(assets), str(assets))
        for asset in assets:
            target = ROOT / "static" / "dist" / "assets" / asset
            check(f"产物存在 assets/{asset}", target.exists(),
                  f"{target.stat().st_size} 字节" if target.exists() else "缺失，需要 npm run build")

    # ---- 词库与代码内置的默认词库必须一字不差 ----
    default_text = ""
    if "app.py" in sources:
        tree = ast.parse(sources["app.py"])
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "DEFAULT_WORDLIST" for t in node.targets):
                default_text = ast.literal_eval(node.value)
                break
    check("app.py 里能找到 DEFAULT_WORDLIST", bool(default_text))

    words_file = ROOT / "违禁词库.txt"
    file_text = ""
    if check("违禁词库.txt 存在", words_file.exists(), "" if words_file.exists() else "缺失，启动服务时会自动生成"):
        raw = words_file.read_bytes()
        check("违禁词库.txt 带 UTF-8 BOM", raw.startswith(b"\xef\xbb\xbf"),
              "没有 BOM（记事本另存为 ANSI 会把中文词写坏）" if not raw.startswith(b"\xef\xbb\xbf") else "")
        file_text = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n").strip()
        normalized_default = default_text.replace("\r\n", "\n").strip()
        same = file_text == normalized_default
        detail = ""
        if not same and normalized_default:
            got = file_text.split("\n")
            want = normalized_default.split("\n")
            diff = [f"第 {i + 1} 行：文件={got[i][:24]!r} / 内置={want[i][:24]!r}"
                    for i in range(min(len(got), len(want))) if got[i] != want[i]]
            detail = f"文件 {len(got)} 行 / 内置 {len(want)} 行；" + "；".join(diff[:3])
        check("违禁词库.txt 与 app.py DEFAULT_WORDLIST 逐行一致", same, detail)

    # ---- 房间公告：每个房间一份 txt，内容由老师自己维护 ----
    # 只确认「目录在、文件记事本能正常打开、老文件没被删」——
    # 内容随老师改，一个字都不许在这里写死（写死就等于「老师改公告，测试变红」）。
    announce_dir = ROOT / "公告"
    announce_txts = sorted(announce_dir.glob("*.txt")) if announce_dir.is_dir() else []
    check("房间公告：公告\\ 目录里至少有一份 txt（启动服务会自动生成）",
          bool(announce_txts),
          "、".join(item.name for item in announce_txts) if announce_txts
          else "缺失，启动服务时会自动生成")
    if announce_txts:
        no_bom = [item.name for item in announce_txts if not item.read_bytes().startswith(b"\xef\xbb\xbf")]
        check(f"公告目录里 {len(announce_txts)} 个 txt 都带 UTF-8 BOM", not no_bom,
              ("没有 BOM（记事本另存为 ANSI 会把中文写坏）：" + "、".join(no_bom)) if no_bom else "")
    # 老版本那份全局「公告.txt」：迁移是「搬一份到 公告/lobby.txt」，原文件保留不删。
    legacy_notice = ROOT / "公告.txt"
    if legacy_notice.exists():
        check("老的 公告.txt 还在（迁移只搬不改，留给你翻查）", True,
              f"{legacy_notice.stat().st_size} 字节")

    # ---- 公告的两条结构判据：按房间、且目录可被测试隔离 ----
    # 这两条都是「坏了不报错、只是静默出事」的类型，所以必须由静态检查钉住：
    #   · 退回全站一个文件 → 房主改公告会波及所有房间
    #   · 目录不能重定向   → 跑一次测试就往老师正在用的 公告\ 里写文件（本轮真踩过）
    # ★ 一律看**真实代码**：字符串匹配会被注释和文档说明骗过去
    #   （代码里写着「本目录可用 CHAT_ANNOUNCE_DIR 换掉」的注释，一样能骗过 `in` 判断）。
    if "app.py" in sources:
        announce_text = sources["app.py"]
        if isinstance(announce_text, bytes):
            announce_text = announce_text.decode("utf-8", "replace")
        announce_tree = ast.parse(announce_text)

        # ① 目录必须从环境变量取（测试才能重定向）
        dir_expr = ""
        path_body = ""
        for node in ast.walk(announce_tree):
            if isinstance(node, ast.Assign) and any(
                    getattr(target, "id", "") == "ANNOUNCE_DIR" for target in node.targets):
                dir_expr = ast.unparse(node.value)
            if isinstance(node, ast.FunctionDef) and node.name == "announcement_path":
                path_body = "\n".join(ast.unparse(item) for item in node.body)
        check("★ 公告目录可以重定向（ANNOUNCE_DIR 从环境变量取）",
              "CHAT_ANNOUNCE_DIR" in dir_expr,
              f"ANNOUNCE_DIR = {dir_expr or '（没找到）'}　—— 测试没法隔离，"
              "跑一次检查就会往你正在用的「公告」目录里写文件")
        check("★ 公告文件按房间号拼（不是全站共用一个文件）",
              "room_id" in path_body and "{safe}" in path_body,
              f"announcement_path 的实现：{path_body or '（没找到）'}")

    # ---- 互动中心模板里的类名必须在样式表里有定义 ----
    # 改布局时最容易犯的错就是模板写 panel-cta、样式里写成 panel-center：
    # 不报错、不崩溃，只是「怎么改都没反应」，肉眼很难发现。这里钉死。
    act_vue = ROOT / "frontend/src/RoomActivities.vue"
    style_css = ROOT / "frontend/src/style.css"
    if act_vue.exists() and style_css.exists():
        vue_text = act_vue.read_text(encoding="utf-8")
        css_text = style_css.read_text(encoding="utf-8")
        used = sorted({
            name for match in re.finditer(r'class="([^"]*)"', vue_text)
            for name in match.group(1).split() if not name.startswith((":", "{"))
        })
        defined = set(re.findall(r"\.([A-Za-z][\w-]*)", css_text))
        orphan = [name for name in used if name not in defined]
        check(f"互动中心模板的 {len(used)} 个类都有样式定义", not orphan,
              ("无样式：" + "、".join(orphan)) if orphan else "")

    # ---- UPDATE users 用到的列必须真实存在 ----
    # 改了 SQL 列清单却忘了往迁移清单里加列，只会在运行期炸 500（而且是发言那条路径）。
    if "app.py" in sources:
        app_text = sources["app.py"]
        if isinstance(app_text, bytes):          # sources 里存的是原始字节
            app_text = app_text.decode("utf-8", "replace")
        real_columns: set[str] = set()
        block = re.search(r"CREATE TABLE IF NOT EXISTS users\s*\((.*?)\);", app_text, re.S)
        if block:
            for line in block.group(1).splitlines():
                token = line.strip().split(" ")[0].strip(",")
                if token and token.upper() not in ("PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"):
                    real_columns.add(token)
        real_columns |= set(re.findall(r'\("users",\s*"(\w+)"', app_text))   # 后加列的迁移清单
        used_columns: set[str] = set()
        for match in re.finditer(r"UPDATE users SET (.+?) WHERE", app_text, re.S):
            clause = match.group(1)
            # persist_session 的 SET 子句是用 f-string 拼出来的（列清单抽成了常量），
            # 正则看不到 —— 那种情况交给下面那条 PERSISTED_USER_COLUMNS 检查，
            # 这里跳过，别把 "{', '.join(columns)}" 误报成缺列。
            if "{" in clause:
                continue
            used_columns |= {piece.strip().split("=")[0].strip()
                             for piece in clause.split(",") if piece.strip()}
        persisted = re.search(r"PERSISTED_USER_COLUMNS\s*=\s*\((.*?)\)", app_text, re.S)
        if persisted:
            used_columns |= set(re.findall(r'"(\w+)"', persisted.group(1)))
        # 「改密码」是按需追加的，不在常量里，单独收一下
        used_columns |= set(re.findall(r'columns\.append\("(\w+)"\)', app_text))
        missing = sorted(used_columns - real_columns)
        check(f"UPDATE users 用到的 {len(used_columns)} 列都在表结构里", not missing,
              ("缺列（SQL 里写了但没加迁移）：" + "、".join(missing)) if missing else "")

    # ---- 资料字段的公开边界：在线列表里谁看得见什么 ----
    # public() 会在每次在线列表刷新时广播给全房间，「装进 public()」就等于「全房间免费可见」。
    #   · 真实姓名 / 班级 / 账号名 → 花 1 金币才能看，混进来等于收费形同虚设
    #   · 个性签名 → 它本来就是给人看的（要显示在在线列表里），必须在这儿；
    #     少了它列表上不会有任何报错，只会静默变空
    #   · 房主标识 owner / 按房间禁言 muted_until → 同理，都是「名单上该看得见」的栏位
    if "app.py" in sources:
        app_bytes = sources["app.py"]
        app_tree = ast.parse(app_bytes if isinstance(app_bytes, str) else app_bytes.decode("utf-8", "replace"))
        paid_fields = {"real_name", "class_name", "username"}
        leaked: list[str] = []
        constants: set[str] = set()
        attributes: set[str] = set()          # public() 里调用了哪些 self.xxx
        for node in ast.walk(app_tree):
            if isinstance(node, ast.ClassDef) and node.name == "Session":
                for func in node.body:
                    if isinstance(func, ast.FunctionDef) and func.name == "public":
                        for inner in ast.walk(func):
                            if isinstance(inner, ast.Constant):
                                constants.add(str(inner.value))
                                if inner.value in paid_fields:
                                    leaked.append(str(inner.value))
                            elif isinstance(inner, ast.Attribute):
                                attributes.add(inner.attr)
        check("Session.public() 里没有付费资料字段（真实姓名/班级/账号名）",
              not leaked,
              ("被免费广播出去了：" + "、".join(sorted(set(leaked)))) if leaked
              else "这几项只能走 self_public() 与 /api/profile/view")
        check("★ Session.public() 里带着个性签名（在线列表要显示它）",
              "bio" in constants,
              "签名没进 public() —— 在线列表上将看不到任何人的签名（不报错，只静默消失）")
        # ★ 房主标识：在线列表上那个金色「房主」小徽章就靠这一栏。
        #   漏了它界面上不报错，只是谁都永远不是房主，右键菜单也不会出现房主那两项。
        check("★ Session.public() 里带着房主标识 owner（在线列表要显示「房主」徽章）",
              "owner" in constants,
              "owner 没进 public() —— 在线列表上没人会显示房主，房主自己也看不到管理菜单")
        # ★ 禁言必须「按房间算」：直接读 self.muted_until 会漏掉房主施加的本房间禁言，
        #   症状是「在 A 房被禁言，名单上却不显示禁言中」。所以这里钉死必须走 muted_until_in()。
        check("★ Session.public() 里的 muted_until 是按房间算的（走 muted_until_in）",
              "muted_until_in" in attributes,
              "写成了 self.muted_until —— 房主施加的本房间禁言不会显示在名单上（全站禁言才显示）")

    # ---- 「查看对方资料」必须走收费接口 ----
    profile_vue = ROOT / "frontend/src/App.vue"
    if profile_vue.exists():
        vue_text = profile_vue.read_text(encoding="utf-8")
        check("在线列表菜单里有「查看资料」，且调用的是收费接口 /api/profile/view",
              "userAction('profile')" in vue_text and "/api/profile/view" in vue_text,
              "菜单项或收费接口对不上 —— 查看资料变成了免费")
        check("★ 在线列表条目里渲染了 onlineUser.bio（签名要在名单里显示出来）",
              "onlineUser.bio" in vue_text,
              "在线列表模板没渲染签名 —— 服务端广播了，界面上照样看不到")

        # ---- 历史请求的竞态闸门 ----
        # loadHistory() 是异步的：请求发出后用户可能立刻切房间 / 进出私聊，晚到的旧响应会把
        # 新画面整块覆盖（表现：公共房间里显示着私聊记录）。所以覆盖 messages 之前必须先比一次
        # 「自增序号」。★ 这里要锁的是**比较本身**，不是「出现过 historySeq 这个词」——
        #   `const seq = ++historySeq` 里也有这个词，只查它等于没查（本项目踩过的同型假绿）。
        history_body = ""
        history_block = re.search(r"async function loadHistory\(\)\s*\{(.*?)\n\}", vue_text, re.S)
        if history_block:
            history_body = history_block.group(1)
        seq_guard = re.search(r"if\s*\(\s*seq\s*!==\s*historySeq\s*\)\s*return", history_body)
        overwrite_at = history_body.find("messages.value =")
        check("★ loadHistory 覆盖消息列表**之前**先比对请求序号（防晚到的旧响应盖掉新画面）",
              bool(seq_guard) and overwrite_at >= 0 and seq_guard.start() < overwrite_at,
              "少了「先比序号再赋值」这道闸门 —— 快速切房间 / 进出私聊时，晚到的历史会覆盖当前画面")

        # ---- 在线列表条目的排版：用户类型要挨着昵称（原来它独占一行，一个条目 4 行）----
        # 模板里加了类名、样式表里却没有对应规则时，徽章会静默退化成一行斜体裸字
        # —— 不报错、不崩，只是变难看。两边各拴一条。
        style_file = ROOT / "frontend/src/style.css"
        css_text = style_file.read_text(encoding="utf-8") if style_file.exists() else ""
        item_at = vue_text.find('class="user-item"')
        # ★ 截取范围用 online-dot（条目自己的结尾标记）来定，不要用 user-bio：
        #   一旦哪天签名那一行被删掉，user-bio 找不到 → 片段变空 → 这条「紧挨着昵称」
        #   会跟着一起变红，看着像排版坏了，其实只是切片切错了。
        #   （本轮改签名时真踩过：牙齿脚本里删掉签名，这条判据跟着误红。）
        dot_at = vue_text.find('class="online-dot"', item_at) if item_at >= 0 else -1
        item_fragment = vue_text[item_at:dot_at] if item_at >= 0 and dot_at > item_at else ""
        role_at = item_fragment.find('class="user-role"')
        name_row_end = item_fragment.find("</b>")
        check("★ 在线列表里用户类型紧挨着昵称（写在昵称那一行内，不再独占一行）",
              role_at >= 0 and name_row_end >= 0 and role_at < name_row_end,
              "用户类型没写在昵称行内 —— 条目又会变回 4 行" if item_fragment
              else "没找到在线列表条目模板（user-item / online-dot）")
        check("★ 用户类型徽章的样式存在（.user-info .user-role）",
              ".user-info .user-role{" in css_text,
              "模板里有 user-role 类名、样式表里没有对应规则 —— 徽章会静默退化成一行裸字")

        # ---- 房主：名单上的标识 + 右键菜单 + 公告编辑入口 ----
        # 三处都是「模板改了、另一端没跟上就静默失效」的类型：
        #   · 模板渲染 owner、样式没写 → 徽章变一行裸字
        #   · 菜单项写了、后端动作没接 → 点了没反应
        #   · 公告能编辑、保存却不带 room_id → 房主一改就把公告写到别的房间去了
        check("★ 在线列表渲染了房主标识（onlineUser.owner）",
              "onlineUser.owner" in vue_text,
              "模板没渲染 owner —— 服务端广播了房主身份，名单上照样看不出来")
        check("★ 房主徽章的样式存在（.user-info .user-owner）",
              ".user-info .user-owner{" in css_text,
              "模板里有 user-owner 类名、样式表里没有对应规则 —— 会静默退化成一行斜体裸字")
        check("★ 右键菜单里有房主管理项，且只有房主/管理员看得到",
              "isOwner && !isAdmin" in vue_text and "userAction('mute')" in vue_text
              and "userAction('unmute')" in vue_text,
              "房主的「本房间禁言 / 解除禁言」菜单项缺失或没做身份限制")
        check("★ 任命房主的动作带上了 room_id（房主是「某个房间」的房主，不能只传人）",
              "wasOwner ? 'unset_owner' : 'set_owner'" in vue_text and "room_id: currentRoomId.value" in vue_text,
              "发送 set_owner 时没带当前房间号 —— 会把房主身份授到别的房间去")
        check("★ 公告编辑保存走 POST /api/announcement，并带上 room_id",
              "saveAnnouncement" in vue_text and "method: 'POST'" in vue_text
              and "room_id: currentRoomId.value" in vue_text and "announcementDraft" in vue_text,
              "编辑入口或带 room_id 的保存逻辑对不上 —— 房主改公告会写到别的房间")
        check("★ 公告读取也带 room_id（GET /api/announcement?room_id=）",
              "/api/announcement?room_id=" in vue_text,
              "读公告没带房间号 —— 换了房间还是显示上一间房的公告")
        check("★ 房主在页面上改的公告，别人能立刻看到（announcement_updated 广播）",
              "announcement_updated" in vue_text,
              "前端没接 announcement_updated —— 房主改完，同房间其他人要刷新页面才看得到")

        # ---- 班级下拉的选项：前端和后端各写了一份，必须逐项一致 ----
        # 这是「改一处漏一处」的经典位置：不同步的症状是「界面上能选、保存却被服务端拒绝」，
        # 不报错、不崩，只是默默失败 —— 所以拿一条静态检查拴住。
        app_text = sources.get("app.py") or ""
        if isinstance(app_text, bytes):
            app_text = app_text.decode("utf-8", "replace")
        backend_grade_src = re.search(r"PROFILE_GRADE_OPTIONS\s*=\s*\(([^)]*)\)", app_text)
        backend_class_src = re.search(
            r'PROFILE_CLASS_OPTIONS\s*=\s*tuple\(f"\{index\}班"\s*for index in range\(1,\s*(\d+)\)\)', app_text)
        front_grade_src = re.search(r"const GRADE_OPTIONS = \[([^\]]*)\]", vue_text)
        front_class_src = re.search(r"const CLASS_OPTIONS = \[([^\]]*)\]", vue_text)
        backend_grades = re.findall(r'"([^"]*)"', backend_grade_src.group(1)) if backend_grade_src else []
        backend_classes = [f"{index}班" for index in range(1, int(backend_class_src.group(1)))] if backend_class_src else []
        front_grades = re.findall(r"'([^']*)'", front_grade_src.group(1)) if front_grade_src else []
        front_classes = re.findall(r"'([^']*)'", front_class_src.group(1)) if front_class_src else []
        check("班级下拉的「年级」选项前后端一致",
              bool(backend_grades) and front_grades == backend_grades,
              f"前端 {front_grades} / 后端 {backend_grades}")
        check("班级下拉的「班级号」选项前后端一致",
              bool(backend_classes) and front_classes == backend_classes,
              f"前端 {front_classes} / 后端 {backend_classes}")

    # ---- 每一个发言入口都必须过统一的预检 ----
    # 断的是「关系」不是「存在」：screen_outgoing 写出来却没用上，等于没写。
    # 改动前 message / private_message 各自手抄了一份判空（零宽字符那个洞就是这么来的：
    # strip() 去不掉 \u200b，normalize 之后才现形，两处都没复查），现在统一走 screen_outgoing。
    # 将来再加发言入口（弹幕、语音转文字…）漏接这条预检时，这里会立刻报红。
    if "app.py" in sources:
        app_text = sources["app.py"]
        if isinstance(app_text, bytes):
            app_text = app_text.decode("utf-8", "replace")
        speak_entries = ('elif message_type == "message":',
                         'elif message_type == "private_message":')
        ungated: list[str] = []
        for header in speak_entries:
            if header not in app_text:
                ungated.append(header + "（入口不见了？）")
                continue
            start = app_text.index(header)
            # 截到下一条同缩进的 elif，作为这个分支的完整函数体
            nxt = app_text.find("\n            elif ", start + 1)
            branch = app_text[start:nxt if nxt != -1 else len(app_text)]
            if "screen_outgoing(session" not in branch:
                ungated.append(header)
        check(f"{len(speak_entries)} 个发言入口都走了 screen_outgoing 预检",
              not ungated,
              ("没走预检：" + "、".join(ungated)) if ungated else "")

    # ---- 房主权限清单：谁能干什么，写死在两个常量里，这里逐条核对 ----
    # 这是本功能最要紧的一条边界：房主**不能**自己任命房主（否则一个人当了官就能拉一群官）。
    # 把清单抽成常量就是为了让这条检查能一眼看明白，散在 if/elif 里没法核对。
    if "app.py" in sources:
        app_text = sources["app.py"]
        if isinstance(app_text, bytes):
            app_text = app_text.decode("utf-8", "replace")

        def action_list(const_name: str) -> set[str]:
            found = re.search(rf"{const_name}\s*=\s*\(([^)]*)\)", app_text)
            return set(re.findall(r'"([^"]*)"', found.group(1))) if found else set()

        owner_actions = action_list("OWNER_ACTIONS")
        admin_only = action_list("ADMIN_ONLY_ACTIONS")
        check("★ 房主的权限清单只有「本房间禁言 / 解除」（OWNER_ACTIONS）",
              owner_actions == {"mute", "unmute"},
              f"实际：{sorted(owner_actions)}　—— 房主多出别的权限就意味着越权")
        check("★ 任命 / 撤销房主属于管理员专属动作（房主不能自己拉人当官）",
              {"set_owner", "unset_owner"} <= admin_only and not ({"set_owner", "unset_owner"} & owner_actions),
              f"OWNER_ACTIONS={sorted(owner_actions)} / ADMIN_ONLY={sorted(admin_only)}")
        check("★ 全站禁言（mute_all / unmute_all）也是管理员专属",
              {"mute_all", "unmute_all"} <= admin_only and not ({"mute_all", "unmute_all"} & owner_actions),
              "房主能全站禁言的话，「只影响本房间」这条底线就破了")
        check("★ 未知动作被挡下（不会静默当成某个已有动作执行）",
              "if action not in OWNER_ACTIONS + ADMIN_ONLY_ACTIONS" in app_text,
              "管理动作入口少了白名单前置校验")

        # ---- 两张新表必须在初始化 / 迁移清单里 ----
        tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", app_text))
        missing_tables = sorted({"room_owners", "room_mutes"} - tables)
        check("★ room_owners / room_mutes 两张表都在建表清单里", not missing_tables,
              ("缺表（加进 SQL 却忘了写 CREATE TABLE，运行期才会炸）：" + "、".join(missing_tables))
              if missing_tables else f"共 {len(tables)} 张表")

        # ---- 「按房间禁言」必须只写 room_mutes，不能顺手把全站禁言也改了 ----
        # 混进去的症状：房主在 A 房禁言某人，那人在 B、C、D 房全都说不了话 —— 当天很难发现。
        check("★ 房主禁言只写本房间（room_mutes），不碰全站禁言的 users.muted_until",
              "set_room_mute(room_id" in app_text and "persist_global_mute" in app_text
              and "全站禁言" in app_text,
              "按房间禁言与全站禁言的落库路径混在一起了")
        # ---- 全站禁言的落库不该按 role 白名单 ----
        # 原来是 `if target.role == "user":`（白名单的反面）：将来多任何一种带身份的人，
        # 对他全站禁言就会变成「内存里生效、重启就失效」——不报错、当天也看不出来。
        # 现在改成「只跳过游客」。这里把那个函数的**代码**抠出来核对（注释与文档字符串不算，
        # 因为文档字符串里就写着那段旧代码用来对照，拿去匹配等于自己把自己判红）。
        persist_code = ""
        try:
            for node in ast.walk(ast.parse(app_text)):
                if isinstance(node, ast.FunctionDef) and node.name == "persist_global_mute":
                    body = node.body
                    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                        body = body[1:]          # 去掉文档字符串，只看真正的代码
                    persist_code = "\n".join(ast.unparse(item) for item in body)
                    break
        except SyntaxError:
            pass
        check("★ 全站禁言的落库不再按 role 白名单（白名单的反面会漏掉新身份）",
              bool(re.search(r"""role\s*==\s*['"]guest['"]""", persist_code))
              and not re.search(r"""role\s*==\s*['"]user['"]""", persist_code),
              "persist_global_mute() 应当「只跳过游客」。实际代码："
              + (persist_code.replace("\n", " ") if persist_code else "没找到这个函数"))

        # ---- 更新公告要广播两件事，一件都不能少 ----
        #   · announcement_updated：刷新左侧面板与公告弹窗的**内容**
        #   · system：往聊天区插一条「谁改了公告」的**提示**
        # 只发前者的话功能不出错、面板也真的变了 —— 但在打字的学生根本不会发现公告被改过。
        # 两条都必须带 room_id：漏了就变成「改一个房间的公告、所有房间都收到提示」。
        # 用 AST 抠真实调用（注释里也写着这些字符串，直接 in 判断会被自己的注释骗过去）。
        announce_calls = ""
        try:
            for node in ast.walk(ast.parse(app_text)):
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "update_announcement":
                    announce_calls = "\n".join(ast.unparse(item) for item in node.body)
                    break
        except SyntaxError:
            pass
        has_refresh = bool(re.search(r"""['"]announcement_updated['"]""", announce_calls))
        has_notice = bool(re.search(r"""['"]system['"]""", announce_calls))
        room_scoped = announce_calls.count("room_id=room_id")
        check("★ 更新公告同时广播「刷新内容」与「聊天区提示」两条",
              has_refresh and has_notice,
              "只广播了其中一条 —— 要么别人看不到新公告，要么没人知道公告被改了"
              if not (has_refresh and has_notice)
              else f"announcement_updated={has_refresh} / system={has_notice}")
        check("★ 这两条广播都限定在本房间（room_id=room_id）",
              room_scoped >= 2,
              "少了房间限定，本房间的公告提示会撒到所有房间去。实际调用："
              + (announce_calls.replace("\n", " ")[:160] if announce_calls else "没找到这个函数")
              if room_scoped < 2 else f"带房间限定的广播 {room_scoped} 条")

    # ---- 活动消息分档：哪些留在聊天区、哪些收进折叠带 ----
    # 判据只有一处（app.activity_tier），前端不靠文案前缀去猜 —— 那样改一个 emoji 就全乱。
    # 这块最要紧的是「漏配时往哪边倒」：没分类的玩法必须退化成「不折叠」；
    # 反过来（兜底成折叠）会让消息被收进带子里，学生不点开就等于没看见。
    if "app.py" in sources:
        app_text = sources["app.py"]
        if isinstance(app_text, bytes):
            app_text = app_text.decode("utf-8", "replace")
        stream_block = re.search(r"ACTIVITY_STREAM_KINDS\s*=\s*frozenset\(\{([^}]*)\}\)", app_text)
        stream_kinds = set(re.findall(r'"([^"]*)"', stream_block.group(1))) if stream_block else set()
        hot = {"redpack_claim", "prediction_vote", "idiom", "lottery"}
        missing_hot = sorted(hot - stream_kinds)

        check("★ 流水档的活动类型集中在一处（ACTIVITY_STREAM_KINDS）",
              bool(stream_kinds),
              f"清单里共 {len(stream_kinds)} 类流水活动" if stream_kinds
              else "没找到这个常量 —— 档位散落各处的话，「哪些活动会折叠」就没法一眼看全")
        check("★ 刷屏最凶的四类都在流水档里（抢红包 / 投票 / 接龙 / 抽奖）",
              not missing_hot,
              ("漏了这几类，它们会继续一条条刷屏：" + "、".join(missing_hot)) if missing_hot
              else "抢红包、竞猜投票、接龙、抽奖都在")
        check("★ 大喇叭不在流水档（花钱买的曝光不能被折叠）",
              "horn" not in stream_kinds,
              "大喇叭没进流水档 ✓" if "horn" not in stream_kinds
              else "大喇叭被收进折叠带 —— 那 10 金币买来的曝光就白花了")

        def calls_with_tier(func_name: str) -> bool:
            """某个函数体里有没有真的调用 with_activity_tier（注释不算，注释骗过一次了）。"""
            try:
                tree = ast.parse(app_text)
            except SyntaxError:
                return False
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                    return any(isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                               and inner.func.id == "with_activity_tier" for inner in ast.walk(node))
            return False

        live_tier, history_tier = calls_with_tier("save_message"), calls_with_tier("row_to_message")
        check("★★ 实时广播与刷新后的历史走同一道分档（save_message / row_to_message 都调它）",
              live_tier and history_tier,
              "两条路都过 ✓" if (live_tier and history_tier) else
              f"save_message={live_tier} / row_to_message={history_tier} —— 少了任一条，"
              "活动消息的档位在「刚收到」与「刷新之后」就不一致："
              "表现是刚玩时折叠得好好的、按一下 F5 全散开，不报错、只静默失效")

    # ---- 前端折叠带 ----
    if "app.py" in sources:
        feed_vue = ROOT / "frontend/src/App.vue"
        feed_css = ROOT / "frontend/src/style.css"
        if feed_vue.exists():
            feed_vue_text = feed_vue.read_text(encoding="utf-8")
            feed_css_text = feed_css.read_text(encoding="utf-8") if feed_css.exists() else ""
            grouped = 'v-for="group in feed"' in feed_vue_text
            # 必须匹配「赋值表达式里的比较」，不能只判断子串在不在 ——
            # 这段逻辑上面的注释里就写着 `activity_tier === 'stream'` 这串字，
            # 用 in 判断的话，把真正的判据改歪了它照样绿（本轮牙齿验证真踩到了）。
            by_tier = bool(re.search(r"=\s*[^\n;]*\bactivity_tier\s*===\s*['\"]stream['\"]", feed_vue_text))
            expandable = "toggleStream" in feed_vue_text and "group.items" in feed_vue_text
            styled = ".activity-bundle{" in feed_css_text
            check("★ 聊天区按分组渲染（模板里出现 group in feed）",
                  grouped,
                  "已按分组渲染 ✓" if grouped else "模板还是逐条渲染 —— 折叠带不会出现，活动消息照旧一条占一行")
            check("★ 折叠的判据是服务端下发的 activity_tier，不是前端猜文案前缀",
                  by_tier,
                  "判据取自服务端 ✓" if by_tier else "前端若按 🎰🧧📜 这些前缀去猜，改一个图标就会全乱")
            check("★ 折叠带能点开看明细（toggleStream + 逐条列出）",
                  expandable,
                  "可展开 ✓" if expandable else "只能折叠、点不开的话，学生就真的看不到内容了 —— 那还不如不折叠")
            check("★ 折叠带有对应样式（.activity-bundle）",
                  styled,
                  "样式齐 ✓" if styled else "模板里有类名、样式表却没规则 —— 折叠带会静默退化成一行裸文字")

        # ---- 「等人回应」的活动不许折起来（2026-10-03）----
        # 真心话 / 大冒险折进折叠带 = 别人根本不知道该接话，题就废了。
        # 折叠带省下的那点版面，换不来「有人回应」这个前提。
        check("★★ 真心话 / 大冒险不进折叠档（折起来就没人接话了）",
              "truth" not in stream_kinds and "dare" not in stream_kinds,
              "留在聊天区单行显示 ✓" if ("truth" not in stream_kinds and "dare" not in stream_kinds)
              else "被折进折叠带了 —— 别人看不见，也就没法参与")

        # ---- 活动消息上的「去参与」入口 ----
        # 判据只看 ACTIVITY_JOIN 这一张表 + 模板是否真的用了它。
        # 名单和按钮必须同时存在：只写名单不渲染 = 白写；只渲染不查名单 = 每个结果消息都长按钮。
        join_map = re.search(r"ACTIVITY_JOIN\s*=\s*\{([^}]*)\}", feed_vue_text)
        join_kinds = set(re.findall(r"(\w+)\s*:\s*'\w+'", join_map.group(1))) if join_map else set()
        want_join = {"game_start", "truth", "dare", "redpack_create", "prediction_start"}
        # 结果类不该有按钮：事已办完，再放一个只会让人白点一趟。
        no_join = {"lottery", "wheel", "game_win", "redpack_claim", "prediction_vote", "horn", "prompt_answer"}
        check("★ 等人动手的五类活动都登记了参与入口（开局 / 真心话 / 大冒险 / 红包 / 竞猜）",
              want_join <= join_kinds,
              f"缺 {sorted(want_join - join_kinds)}" if not want_join <= join_kinds else
              "五类都能一键跳到互动中心 ✓")
        check("★ 结果类活动不显示参与按钮（免得白点一趟）",
              not (no_join & join_kinds),
              f"{sorted(no_join & join_kinds)} 不该有按钮" if no_join & join_kinds else
              "结果类一律不给按钮 ✓")
        check("★ 参与按钮真的渲染在活动消息上（模板用 joinTabFor 决定显示与否）",
              "joinTabFor(group.message)" in feed_vue_text and "joinActivity(group.message)" in feed_vue_text,
              "按钮已挂在活动消息分支 ✓" if "joinTabFor(group.message)" in feed_vue_text
              else "模板里没有 joinTabFor —— 名单配好了但界面不显示")
        check("★ 点参与能落到对应页签（打开弹窗并切 tab，不是只开弹窗）",
              "openActivities(tab)" in feed_vue_text and "ACTIVITY_JOIN_LABEL" in feed_vue_text,
              "跳转带 tab ✓" if "openActivities(tab)" in feed_vue_text
              else "只打开了互动中心 —— 进去还要自己找页签")
        check("★ 参与按钮有对应样式（.activity-join-button）",
              ".activity-join-button{" in feed_css_text,
              "样式齐 ✓" if ".activity-join-button{" in feed_css_text
              else "模板里有类名、样式表没规则 —— 按钮会退化成浏览器默认灰按钮")

    # ---- 真心话 / 大冒险：题目公开、全房间可答、实名公开 ----
    if "interactions.py" in sources:
        it_text = sources["interactions.py"]
        if isinstance(it_text, bytes):
            it_text = it_text.decode("utf-8", "replace")
        # ① 抽题必须变成「一个有 id 的活动」，而不是只往聊天区发一句话。
        #    判据锁的是 calls：真的调了 new_activity + store，光有提示文案不算。
        def branch_body(action: str) -> str:
            """抠出 handle_action 里 `elif action == "<action>"` 那个分支的真实源码。

            ⚠️ 必须用 AST 定位到分支体内，**不能**在整份源码里搜子串：
            handle_action 是个大函数，所有分支平铺在一起 ——
            在全文里搜 `new_activity` 会把 game_start 那处一起算上，
            搜 `session.user_id in activity["answers"]` 会撞上 prediction_vote 那处。
            这种判据会把「真心话分支改坏了」判成绿，正是牙齿验证 M1/M3 抓到的那两种假绿。
            """
            try:
                tree = ast.parse(it_text)
            except SyntaxError:
                return ""
            unparse = getattr(ast, "unparse", None)
            if unparse is None:
                return ""
            for node in ast.walk(tree):
                if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name == "handle_action"):
                    continue
                for inner in ast.walk(node):
                    if not isinstance(inner, ast.If) or not isinstance(inner.test, ast.Compare):
                        continue
                    source = unparse(inner.test)
                    # 形态一：elif action == "prompt_answer"
                    # ast.unparse 输出的是**单引号**（action == 'prompt_answer'），
                    # 正则必须两种引号都认，否则永远匹配不上（这次真踩了）。
                    quote = "'"
                    if re.search(rf'action\s*==\s*[{quote}"]{re.escape(action)}[{quote}"]', source):
                        return unparse(inner)
                    # 形态二：elif action in ("truth", "dare") —— 两种动作共用一段代码
                    if (action in ("truth", "dare")
                            and (f"'{action}'" in source or f'"{action}"' in source)
                            and ("'dare'" in source or '"dare"' in source)):
                        return unparse(inner)
            return ""

        draw_body = branch_body("truth")
        reply_body = branch_body("prompt_answer")

        prompt_makes_activity = ("new_activity(" in draw_body and "store(activity)" in draw_body
                                 and "PROMPT_SECONDS" in draw_body)
        check("★★ 抽真心话/大冒险会真的产生一个活动（题目进 room_activities，不是只发一句话）",
              prompt_makes_activity,
              "抽题会落库、别人才答得上 ✓" if prompt_makes_activity
              else "题目没进活动表 —— 别人根本没有 id 可以回答")

        # ② 必须有独立的提交动作，且奖励值只有 PROMPT_REWARD 一个来源（前端从 snapshot 读，不硬写）。
        has_prompt_answer = bool(reply_body)
        # 只数 prompt_answer 分支内的那一处 —— 别把别的分支里碰巧的 coins += 也算进来。
        reward_sites = len(re.findall(r"coins\s*\+=\s*PROMPT_REWARD", reply_body))
        reward_single_source = reward_sites == 1
        reward_in_snapshot = re.search(r'"prompt"\s*:\s*\{[^}]*"reward"\s*:\s*PROMPT_REWARD', it_text)
        check("★★ 有「提交回答」这个动作（prompt_answer），别人才答得上",
              has_prompt_answer,
              "prompt_answer 已就位 ✓" if has_prompt_answer
              else "没有 prompt_answer —— 题目发出来了没人能答")
        check("★ 回答的金币只加一次、且常量只有一处（防止答一次发两次）",
              reward_single_source,
              f"prompt_answer 分支里 coins += PROMPT_REWARD 出现了 {reward_sites} 处"
              if not reward_single_source else "发金币的地方只有一处 ✓")
        check("★ 奖励数值随 snapshot 下发（前端不硬写「+1 金币」）",
              bool(reward_in_snapshot),
              "snapshot.prompt.reward 已下发 ✓" if reward_in_snapshot
              else "snapshot 里没有 prompt.reward —— 前端只能硬写价格，改常量就会对不上")

        # ③ 防刷币闸门：一个房间同时只能有一题 + 每人每题只能答一次。
        #    这两条是奖励机制的生命线，少一条就等于开了印钞机。
        # 同上：判断「kind 里同时含 truth 与 dare」用的是**裸词**，不碰引号。
        one_prompt_per_room = ("activities(session.room_id)" in draw_body
                               and "truth" in draw_body and "dare" in draw_body
                               and "status" in draw_body and "open" in draw_body)
        check("★★ 一个房间同时只能有一道真心话/大冒险（否则可以狂抽题狂自答刷金币）",
              one_prompt_per_room,
              "闸门在 ✓" if one_prompt_per_room
              else "没有「同房间只能一题」的校验 —— 可以狂抽题、狂自己答，奖励变成印钞机")
        # 同样要锁在 prompt_answer 分支内：prediction_vote 里也有一模一样的写法。
        # ⚠️ 别在判据里写 activity["answers"]：ast.unparse 会把引号规范成单引号，
        #    写成双引号就会恒红（这次真踩了）。引号是 unparse 的实现细节，不该进判据。
        once_per_person = bool(reply_body) and bool(re.search(
            r'session\.user_id\s+in\s+activity\[[\'\"]answers[\'\"]\]', reply_body))
        check("★★ 每人每题只能回答一次（否则一个人能反复领同一题的金币）",
              once_per_person,
              "已挡住重复回答 ✓" if once_per_person
              else "没有「每人一次」的校验 —— 同一个人能反复答同一题领金币")

        # ④ 回答是聊天内容，必须走违禁词过滤（与付费的大喇叭相反）。
        reply_filtered = bool(re.search(
            r'moderate_content\(\s*text\s*\)', it_text)) and "violated" in it_text
        check("★ 提交的回答要走违禁词过滤（回答是聊天内容，不是付费内容）",
              reply_filtered,
              "回答会过滤 ✓" if reply_filtered
              else "回答没过 moderate_content —— 答题板成了绕过违禁词的旁路")

        # ⑤ 回答要实名公开：visible() 必须把 replies 明文下发，而不是只给计数。
        replies_exposed = bool(re.search(r'result\["replies"\]\s*=', it_text))
        check("★ 别人的回答实名公开下发（replies 明文，不是只给人数）",
              replies_exposed,
              "replies 已下发 ✓" if replies_exposed
              else "visible() 没下发 replies —— 大家答了但谁也看不到，等于白答")

    # ---- 真心话 / 大冒险的答题板界面 ----
    prompt_vue = ROOT / "frontend/src/RoomActivities.vue"
    if prompt_vue.exists():
        prompt_vue_text = prompt_vue.read_text(encoding="utf-8")
        # 锚点要够「组合」：只看 prompt-board 会漏 —— 改掉 v-if 后字面量仍在，
        # 而且 CSS 里也有同名类。把「答题板 section + 题目块 + 明细列表」绑在一起判，
        # 任何一处被拆掉都会红（牙齿 M9 抓到过假绿）。
        board = bool(re.search(
            r'<section[^>]*class="[^"]*prompt-board"[^>]*>(?:(?!</section>).)*prompt-question'
            r'(?:(?!</section>).)*prompt-replies',
            prompt_vue_text, re.S))
        # 结构在还不够：**必须真的绑到了题目数据**。答题板渲染出来、题目却是空的，
        # 一样是「看得见答不了」。这一条是牙齿 M9 逼出来的（它把 prompt 改成 hint 之后，
        # 只查类名的判据照样绿）。
        reads_prompt = bool(re.search(
            r'class="prompt-question"[^>]*>[^<]*\{\{[^}]*openPrompt\.prompt', prompt_vue_text))
        submit_ok = "act('prompt_answer'" in prompt_vue_text
        once_locked = "repliedPrompt(" in prompt_vue_text
        # 奖励值必须从 state.prompt 读，不能在模板里写死数字。
        reward_from_server = bool(re.search(r"state\.value\.prompt\?\.reward", prompt_vue_text))
        check("★ 真心话/大冒险有独立答题板（题目 + 已提交的回答列表）",
              board and reads_prompt,
              "答题板已就位、且绑到了题目数据 ✓" if board and reads_prompt
              else ("结构在但没绑 openPrompt.prompt —— 答题板会渲染出来却是空的"
                    if board else "模板里没有答题板 —— 别人答完了无处可看"))

        # ---- 回答框的宽度（2026-10-03 反馈：输入框太短，60 字只显示得下五六个）----
        # 判据查两件事：① 模板给答题表单挂了专属类，没跟猜谜答题共用那条 width:90px；
        #            ② 样式里那条类确实把宽度改成撑满（flex:1），不是只写了个类名。
        # 只查类名会被「模板写了类、样式没规则」骗过去 —— 那样输入框还是 90px。
        reply_form_class = bool(re.search(
            r'class="activity-form\s+prompt-reply-form"', prompt_vue_text))
        # 沿用本文件既有的读法（ROOT / "..." 再 read_text）——
        # 这块所在的作用域里没有 pathlib 的导入，直接写 pathlib.Path 会 NameError。
        reply_css = (ROOT / "frontend/src/style.css").read_text(encoding="utf-8")
        wide_rule = re.search(r"\.prompt-reply-form input\{([^}]*)\}", reply_css)
        wide_body = wide_rule.group(1) if wide_rule else ""
        # 必须真的改了宽度：flex:1（撑满）或 width:auto 至少有一个，且不能还留着 90px
        reply_wide = bool(wide_body) and ("flex:1" in wide_body or "width:auto" in wide_body)
        check("★ 回答框不再是 90px 窄条（专属类 + 撑满宽度，学生能看清自己写了什么）",
              reply_form_class and reply_wide,
              f"输入框已撑满（{wide_body[:46]}）✓" if reply_form_class and reply_wide
              else ("模板没挂 prompt-reply-form —— 还在共用猜谜答题那条 width:90px"
                    if not reply_form_class
                    else "模板挂了类但样式里没有 .prompt-reply-form input 规则 —— 宽度没变"))
        check("★ 答题板能提交回答（act('prompt_answer')）",
              submit_ok, "提交按钮已接上 ✓" if submit_ok else "没有提交动作，接不上线")
        check("★ 答过的题把输入框锁住（每人每题一次，界面别让人白等节流报错）",
              once_locked, "已答过会锁住 ✓" if once_locked else "没锁 —— 重复提交要等服务端报错才知道")
        check("★ 答题板的金币数从服务端读（模板不硬写「+1」）",
              reward_from_server,
              "读 state.prompt.reward ✓" if reward_from_server
              else "前端写死了金币数 —— 改后端常量界面就对不上")

    # ---- 复用数据库连接之后，动过 DB_PATH 的测试必须显式收尾 ----
    # get_db() 不再每次新开连接，测试若把 DB_PATH 指到临时目录却没在 tearDown 关掉，
    # Windows 上 TemporaryDirectory.cleanup() 会抛 [WinError 32]（本轮真踩过）。
    for test_path in sorted(TESTS.glob("test_*.py")):
        test_text = test_path.read_text(encoding="utf-8")
        if "DB_PATH" not in test_text:
            continue
        check(f"{test_path.name} 改了 DB_PATH，收尾调用了 close_db()",
              "close_db()" in test_text,
              "" if "close_db()" in test_text else "复用连接不关掉 → 临时目录里的 chatroom.db 删不掉")

    # ---- 控制台引用的 manage 能力必须真实存在（改了函数名忘了改控制台，按钮就点不动）----
    if "launcher.py" in sources and "manage.py" in sources:
        manage_tree = ast.parse(sources["manage.py"])
        defined = {
            node.name for node in manage_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        defined |= {
            target.id
            for node in manage_tree.body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)
        }
        referenced = sorted({
            node.attr for node in ast.walk(ast.parse(sources["launcher.py"]))
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "manage"
        })
        missing = [name for name in referenced if name not in defined]
        check(f"控制台引用的 manage 能力都存在（{len(referenced)} 个）", not missing,
              ("缺少：" + "、".join(missing)) if missing else "")

        # ---- 「成员列表」读的列绝不能带出密码哈希 ----
        # ★ 这条锁的是「拿整表当白名单」这类改法：谁把 list_members() 里的 MEMBER_COLUMNS
        #   换成 SELECT *（或在里面塞进 password_hash），这里立刻报红。
        member_columns = {str(name) for name in getattr(manage, "MEMBER_COLUMNS", ())}
        check("★ 成员列表的列白名单里没有 password_hash",
              "password_hash" not in member_columns, "MEMBER_COLUMNS 里出现了 password_hash")
        check("★ 成员列表没走「整表读取」",
              member_columns and not any("*" in name for name in member_columns),
              f"{sorted(member_columns)}")
        key_columns = {"id", "username", "nickname", "role", "created_at"}
        check("成员列表白名单覆盖了关键列", key_columns <= member_columns,
              f"缺少：{sorted(key_columns - member_columns)}")
        fields = list(getattr(manage, "MEMBER_FIELDS", ()))
        check("成员详情 / CSV 用同一份字段清单，且每项都是「表头 + 键」",
              fields and all(len(item) == 2 and all(isinstance(part, str) for part in item)
                             for item in fields),
              f"{fields[:3]}…")
        # 派生列 = list_members() 自己算出来的键（不在 users 表里）。字段清单里的键只允许
        # 来自「users 白名单 ∪ 派生列」，写错一个字母（last_sooke）在这里就拦住。
        member_derived = {"role_text", "items", "items_text", "muted_until_text", "kicked_until_text",
                          "room_muted_text", "room_owner_text", "pet_text", "last_spoke", "messages"}
        unknown_keys = {key for _, key in fields} - member_columns - member_derived
        check("★ 详情 / CSV 的字段键都来自「users 列 ∪ 派生列」", not unknown_keys,
              f"对不上的键：{sorted(unknown_keys)}")

        # ---- 控制台里的基础礼物名字必须与 app.py 的 STARTER_ITEMS 一致 ----
        # 这两份名字天生重复（控制台刻意不 import app.py）。不锁的话，改一边、另一边就
        # 悄悄过期，成员列表上会显示 basic_flower 而不是「鲜花」。
        starter: dict[str, str] = {}
        if "app.py" in sources:
            for node in ast.parse(sources["app.py"]).body:
                if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
                    continue
                if not any(isinstance(t, ast.Name) and t.id == "STARTER_ITEMS" for t in node.targets):
                    continue
                for key_node, value_node in zip(node.value.keys, node.value.values):
                    if not (isinstance(key_node, ast.Constant) and isinstance(value_node, ast.Dict)):
                        continue
                    name = ""
                    for inner_key, inner_value in zip(value_node.keys, value_node.values):
                        if (isinstance(inner_key, ast.Constant) and inner_key.value == "name"
                                and isinstance(inner_value, ast.Constant)):
                            name = str(inner_value.value)
                    starter[str(key_node.value)] = name
                break
        console_names = {str(k): str(v) for k, v in getattr(manage, "KNOWN_ITEM_NAMES", {}).items()}
        check("★ 控制台的基础礼物名字与 app.py 的 STARTER_ITEMS 一致",
              bool(starter) and starter == console_names,
              f"app.py={starter} 控制台={console_names}")

    # ---- 「清空聊天记录」只能动 messages 表，绝不能顺手把账号也删了 ----
    if "manage.py" in sources:
        manage_text = sources["manage.py"].decode("utf-8", "replace")
        clear_source = ""
        for node in ast.parse(sources["manage.py"]).body:
            if isinstance(node, ast.FunctionDef) and node.name == "perform_clear_messages":
                clear_source = ast.get_source_segment(manage_text, node) or ""
                break
        if check("manage.py 里能找到 perform_clear_messages", bool(clear_source)):
            touched_users = "DELETE FROM users" in clear_source
            check("清空只删 messages 表，不碰 users", "DELETE FROM messages" in clear_source and not touched_users,
                  "★ 清空路径里出现了 DELETE FROM users，账号会被一起删掉" if touched_users else "")
            check("清空前的自动备份走「清空记录备份」前缀", "backup_before_clear()" in clear_source,
                  "" if "backup_before_clear()" in clear_source else "没有先备份就删，出问题无法回退")

        # ---- 「还原备份」的顺序：先校验 → 先另存当前状态 → 旧库让开位置 → 才就位 ----
        # 这里一律用 ast 取真实调用、真实行号与真实字面量，不用字符串匹配 ——
        # 注释里写一句「不删 -wal 会怎样怎样」就能把字符串判据骗绿。
        restore_source = ""
        for node in ast.parse(sources["manage.py"]).body:
            if isinstance(node, ast.FunctionDef) and node.name == "restore_backup":
                restore_source = ast.get_source_segment(manage_text, node) or ""
                break
        if check("manage.py 里能找到 restore_backup", bool(restore_source)):
            restore_tree = ast.parse(restore_source)
            called = [
                node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                for node in ast.walk(restore_tree) if isinstance(node, ast.Call)
            ]
            check("★ 还原前先把当前状态另存一份（选错备份还能退回来）",
                  "_backup_data" in called,
                  "漏了 —— 还原是整点回退，没有这一步就没有后悔药")
            check("★ 还原前校验备份里的数据库能不能用",
                  "_verify_database" in called,
                  "漏了 —— 坏备份会直接盖掉唯一一份数据")

            def calls_in(tree: ast.AST, name: str) -> list[ast.Call]:
                return [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                        and (getattr(node.func, "id", "") == name
                             or getattr(node.func, "attr", "") == name)]

            # ★ 旧库三件套的清单只许有一处（_db_sidecars），并且还原与重置都要走它。
            #   清单抄成两份，迟早「改一处漏一处」——原来就是这么漏掉 -shm 的。
            sidecar_source = ""
            for node in ast.parse(sources["manage.py"]).body:
                if isinstance(node, ast.FunctionDef) and node.name == "_db_sidecars":
                    sidecar_source = ast.get_source_segment(manage_text, node) or ""
                    break
            # f"{DB}-wal" 会被解析成 JoinedStr；取它的常量片段，而不是搜整个源码文本。
            sidecar_suffixes = {
                part.value
                for node in ast.walk(ast.parse(sidecar_source or "pass"))
                if isinstance(node, ast.JoinedStr)
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            }
            check("★ 旧库三件套（主库 / -wal / -shm）的清单只有一处 _db_sidecars",
                  bool(sidecar_source) and "-wal" in sidecar_suffixes and "-shm" in sidecar_suffixes,
                  f"没找到 _db_sidecars，或它没把 -wal/-shm 列全：{sorted(sidecar_suffixes)}")
            check("★ 还原时连 -wal / -shm 一起处理（只覆盖主库会让旧日志重放到新库上）",
                  bool(calls_in(restore_tree, "_step_aside_db_set")),
                  "还原没有走「旧库整体让位」那条统一路径")

            # ★ 让位失败必须**在把新库放上去之前**中止；且失败要提前 return（回滚过的世界
            #   才能理直气壮地说「没有任何改动」）。比的是 ast 行号，不是字符串位置。
            aside = calls_in(restore_tree, "_step_aside_db_set")
            swaps = [node for node in ast.walk(restore_tree) if isinstance(node, ast.Call)
                     and getattr(node.func, "attr", "") == "replace"
                     and isinstance(node.func.value, ast.Name) and node.func.value.id == "staging"]
            guards = [node for node in ast.walk(restore_tree) if isinstance(node, ast.If)
                      and any(isinstance(inner, ast.Return) for inner in node.body)]
            if aside and swaps:
                aside_at = min(node.lineno for node in aside)
                swap_at = min(node.lineno for node in swaps)
                between = [g for g in guards if aside_at < g.lineno < swap_at]
                check("★ 旧库没让开位置就绝不硬把新库放上去（中间必须有提前 return）",
                      aside_at < swap_at and bool(between),
                      f"位置异常：让位@{aside_at} / 就位@{swap_at} / 中间守卫@{[g.lineno for g in guards]}")
            else:
                check("★ 旧库没让开位置就绝不硬把新库放上去（中间必须有提前 return）",
                      False, f"找不到让位调用或就位调用：aside={len(aside)} swap={len(swaps)}")

            # 重置数据也是删旧库，必须共用同一条路径（原来是各写一份挨个删的代码）。
            reset_source = ""
            for node in ast.parse(sources["manage.py"]).body:
                if isinstance(node, ast.FunctionDef) and node.name == "perform_reset":
                    reset_source = ast.get_source_segment(manage_text, node) or ""
                    break
            check("★ 重置数据与还原共用「旧库整体让位」的实现（别再各写一份）",
                  bool(reset_source) and bool(calls_in(ast.parse(reset_source), "_step_aside_db_set")),
                  "perform_reset 没有走 _step_aside_db_set —— 它又会退回「挨个删、删一半半毁」")

    # ---- 互动中心：花金币的玩法必须走统一闸门，不能直接读写 session.coins ----
    # 背景：管理员的金币是展示用的 ∞（self_public() 下发字符串 "∞"）。直接比 session.coins
    # 会出现「界面写着 ∞，却提示需要 5 金币」；直接 -= 还会把管理员余额扣成负数（控制台
    # 的成员列表是照库内真值显示的）。这里只锁「花出去」的方向，加币不在此列。
    if "interactions.py" in sources:
        games_text = sources["interactions.py"].decode("utf-8", "replace")
        handle_source = ""
        for node in ast.parse(sources["interactions.py"]).body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_action":
                handle_source = ast.get_source_segment(games_text, node) or ""
                break
        if check("interactions.py 里能找到 handle_action", bool(handle_source)):
            handle_tree = ast.parse(handle_source)
            direct_compare = [
                node.lineno for node in ast.walk(handle_tree)
                if isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute)
                and node.left.attr == "coins"
            ]
            direct_cut = [
                node.lineno for node in ast.walk(handle_tree)
                if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Sub)
                and isinstance(node.target, ast.Attribute) and node.target.attr == "coins"
            ]
            check("★ 互动中心判断余额一律走 can_spend_coins（管理员余额是 ∞，直接比会误拒）",
                  not direct_compare, f"还在直接比 session.coins 的行：{direct_compare}")
            check("★ 互动中心扣币一律走 spend_coins（直接 -= 会把管理员余额扣成负数）",
                  not direct_cut, f"还在直接扣 session.coins 的行：{direct_cut}")

    # ---- 收益闸门：只能有一处比较式，送礼的魅力必须走它（2026-10-03 第二轮） ----
    # 背景：送礼原本给了收礼方魅力，却**没有**过 SPEAK_INCOME_INTERVAL 那道闸门 ——
    # 两个人对送同一件礼物（道具绕一圈回到手里）就能无限刷，实测 10 个来回 = 双方各 +20。
    # 所以这里锁三件事：闸门判据唯一、save_message 与送礼都用它、魅力自增只有这一个出口。
    if "app.py" in sources:
        gate_tree = None
        try:
            gate_tree = ast.parse(app_text)
        except SyntaxError:
            gate_tree = None

        def gate_calls(func_name: str, callee: str) -> bool:
            if gate_tree is None:
                return False
            for node in ast.walk(gate_tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                    return any(isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                               and inner.func.id == callee for inner in ast.walk(node))
            return False

        # ① 比较式只有一处：整份源码里跟 last_income_at 有关的 Compare 只能有 1 个。
        gate_compares = 0
        defined_helpers: set[str] = set()
        charm_writer_funcs: set[str] = set()
        if gate_tree is not None:
            for node in ast.walk(gate_tree):
                if isinstance(node, ast.Compare):
                    attrs = {inner.attr for inner in ast.walk(node) if isinstance(inner, ast.Attribute)}
                    if "last_income_at" in attrs:
                        gate_compares += 1
            for node in ast.walk(gate_tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name in ("income_gate_open", "award_gift_charm"):
                    defined_helpers.add(node.name)
                # 谁在写 charm：AugAssign（+= ）指向 .charm 就记一笔
                if any(isinstance(inner, ast.AugAssign) and isinstance(inner.target, ast.Attribute)
                       and inner.target.attr == "charm" for inner in ast.walk(node)):
                    charm_writer_funcs.add(node.name)

        check("★ 收益闸门的比较式只有一处（income_gate_open 是唯一来源）",
              gate_compares == 1,
              f"出现了 {gate_compares} 处跟 last_income_at 有关的比较 —— 多于一处就会各算一套，"
              "魅力那条路当初就是因为「自己另写一份判断」才完全没有闸门")
        check("★ 闸门判据 income_gate_open / 魅力出口 award_gift_charm 都还在",
              defined_helpers == {"income_gate_open", "award_gift_charm"},
              f"实际找到：{sorted(defined_helpers)}")
        check("★★ 说话 / 送礼（save_message）与送礼魅力（award_gift_charm）共用同一道闸门",
              gate_calls("save_message", "income_gate_open") and gate_calls("award_gift_charm", "income_gate_open"),
              f"save_message={gate_calls('save_message', 'income_gate_open')} / "
              f"award_gift_charm={gate_calls('award_gift_charm', 'income_gate_open')} —— 少了任一条，"
              "那笔收益就能被反复触发（对送同一件礼物 = 零成本刷）")
        check("★ 魅力只有 award_gift_charm 一个写入点（送礼分支不许自己再 +charm）",
              charm_writer_funcs == {"award_gift_charm"},
              f"写 charm 的地方：{sorted(charm_writer_funcs) or '（没有）'}")

    # ---- 实时消息的「属于本画面」过滤（2026-10-03 第四轮） ----
    # 背景：切房间是「本地先切、服务端后切」——点下去到服务端真正把你移进新房间之间，
    # 旧房间的广播仍会推到这条连接上。原来 pushLive 只按切换后的 currentRoomId 记账、
    # **不看消息自身的 room_id**，这些消息就会在历史落地时被补进新房间（实测：切到游戏房
    # 之后，大厅里刚说的话出现在游戏房的画面里）。同型的还有两处：管理员删掉的消息会被
    # 在飞的历史响应摆回来；logout 没作废在飞请求，上一个账号的私聊写进了新账号的页面。
    # ⚠️ 这里是 **JS 源码，不能用 ast**（ast 是 Python 解析器，喂 JS 只会 SyntaxError，
    #    结果是「函数找不到 → 判据全红」）。改成：正则定位函数头 + 花括号配平取函数体。
    live_vue = ROOT / "frontend/src/App.vue"
    if live_vue.exists():
        live_blk = re.search(r"<script setup>(.*?)\r?\n</script>", live_vue.read_text(encoding="utf-8"), re.S)
        live_js = live_blk.group(1) if live_blk else ""

        def brace_block(open_at: int) -> str:
            """从 '{' 开始按花括号配平取到对应的 '}'（函数体 / if 块都用它切）。"""
            depth = 0
            for index in range(open_at, len(live_js)):
                if live_js[index] == "{":
                    depth += 1
                elif live_js[index] == "}":
                    depth -= 1
                    if depth == 0:
                        return live_js[open_at:index + 1]
            return ""

        def js_func(name: str, prefix: str = "function") -> str:
            match = re.search(rf"{prefix}\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", live_js)
            return brace_block(match.end() - 1) if match else ""

        live_filter_body = js_func("belongsHere")
        live_push_body = js_func("pushLive")
        live_remember_body = js_func("rememberDeleted")
        live_load_body = js_func("loadHistory", "async function")
        live_logout_body = js_func("logout", "async function")

        # ① 「属于当前画面」的判定只写在 belongsHere 里，pushLive 不许自己再比一次。
        filter_cmp = "room_id !== currentRoomId.value" in live_filter_body
        push_uses = "belongsHere(" in live_push_body
        push_own_cmp = "currentRoomId.value" in live_push_body
        check("★ 「实时消息属不属于当前画面」只在 belongsHere 里判定（pushLive 不许另写一份）",
              bool(live_js) and filter_cmp and push_uses and not push_own_cmp,
              f"belongsHere 自带房间比较={filter_cmp} / pushLive 走它={push_uses} / "
              f"pushLive 里又出现 currentRoomId={push_own_cmp} —— 两份判断迟早漏一处"
              "（送礼魅力当初就是这么漏掉闸门的）")

        # ② 礼物 / 用道具的动画也挂在同一道判据下（切房瞬间不许放旧房间的动画）。
        live_guard = "if (belongsHere(data.message)) {"
        live_guard_at = live_js.find(live_guard)
        live_guarded_body = brace_block(live_guard_at + len(live_guard) - 1) if live_guard_at >= 0 else ""
        live_anim_calls = len(re.findall(r"showItemAnimation\(", live_js))
        check("★ 礼物 / 用道具动画也走同一个 belongsHere（不许各写一份「属于当前画面」）",
              "showItemAnimation(" in live_guarded_body and live_anim_calls == 2,
              f"动画在守卫块里={'showItemAnimation(' in live_guarded_body} / "
              f"showItemAnimation( 出现 {live_anim_calls} 次（定义 1 + 调用 1）")

        # ③ 删除消息留「墓碑」，历史落地时按它过滤（否则「删掉之后历史一回来它又出现」）。
        # ★ 只查 `deletedIds.has(` 会假绿：loadHistory 里合并循环那处也有它，
        #   删掉**关键的那一处**（整块赋值的 filter）判据照样绿。所以锁死这一段的形态。
        live_delete_line = re.search(r"message_deleted[^\n]*rememberDeleted\(", live_js)
        live_history_filter = "!deletedIds.has(message.id)" in live_load_body
        check("★ 删除的消息会留「墓碑」，历史落地时按它过滤（防删完又复活）",
              "deletedIds.add(" in live_remember_body and live_history_filter
              and bool(live_delete_line),
              f"rememberDeleted 记墓碑={'deletedIds.add(' in live_remember_body} / "
              f"历史赋值处按墓碑过滤={live_history_filter} / "
              f"删除分支记一笔={bool(live_delete_line)}")

        # ④ logout 必须作废在飞的历史请求（把序号推高一格）。
        live_logout_bumps = bool(re.search(r"historySeq\s*\+=", live_logout_body))
        check("★ logout 会把 historySeq 推高一格（作废在飞的历史请求）",
              live_logout_bumps,
              "没作废 —— 上一个账号的私聊历史会写进新账号的页面（实测过）")

        # ⑤ loadHistory 自己接住失败，别把未处理拒绝抛给调用方。
        live_handled = ("catch (" in live_load_body or "catch(" in live_load_body) \
            and "聊天记录加载失败" in live_load_body
        check("★ loadHistory 内部接住失败（失败时给提示，不抛未处理的 Promise 拒绝）",
              live_handled,
              "直接往外抛 —— onopen / 切房 / room_joined 这些调用点都是「发了不管」，"
              "网络一断就是控制台一条红字 + 界面上毫无提示")

        # ⑥ 房间消息的实时入口只有 pushLive 一个：别处不许绕过它直接 push。
        #    （历史上绕过一次就丢过消息：历史请求在飞时直接 push 的会被整块覆盖。）
        live_push_sites = len(re.findall(r"messages\.value\.push\(", live_js))
        check("★ 往消息列表里 push 的地方只有两处（pushLive 内 + 历史合并），不许绕过统一入口",
              live_push_sites == 2,
              f"出现了 {live_push_sites} 处 —— 期望 2（pushLive 内 1 处 + loadHistory 合并 1 处）")

        # ⑦ 「在飞的历史请求」必须用**序号集合**，不许用「计数器 + 递减」。
        #    计数器版本踩过：logout 清零后，**旧账号**那次请求回来仍会 -1，把新账号刚登记的
        #    计数减回 0 —— 新账号历史还没落地就不再缓冲，刚冒出来的实时消息被历史整块覆盖
        #    （实测：换账号后消息先显示、再消失）。
        live_inflight_decl = bool(re.search(r"let\s+historyInFlight\s*=\s*new\s+Set\(\)", live_js))
        live_inflight_add = "historyInFlight.add(seq)" in live_load_body
        live_inflight_del = "historyInFlight.delete(seq)" in live_load_body
        live_inflight_bad = bool(re.search(r"historyInFlight\s*(?:\+=|-=|=\s*Math\.max)", live_js))
        check("★★ 「在飞的历史请求」用序号集合登记，不许用「计数器 + 递减」",
              live_inflight_decl and live_inflight_add and live_inflight_del and not live_inflight_bad,
              f"声明为 Set={live_inflight_decl} / add(seq)={live_inflight_add} / "
              f"delete(seq)={live_inflight_del} / 仍在用计数器加减={live_inflight_bad} —— "
              "旧账号的请求会误减新账号的计数（消息先显示、再消失）")

        # ⑧ logout 要把在飞集合清空（否则新账号继承上一位账号的在飞序号）。
        check("★ logout 会清空「在飞历史请求」集合",
              "historyInFlight.clear()" in live_logout_body,
              "没清空 —— 换个账号登进来时，上一位账号的在飞请求仍被当成「自己的」")

        # ⑨ 公告响应落地前必须比对房间。
        #    踩过：打开大厅公告 -> 切到游戏房 -> 旧公告响应回来时无条件
        #    `currentRoomId = result.room_id`，标题被改回大厅、可连接还在游戏房 ——
        #    新房间的消息被房间过滤挡掉，发出去的消息也和标题不是一间。
        live_announce_body = js_func("loadAnnouncement", "async function")
        live_announce_guard = "roomId !== currentRoomId.value" in live_announce_body
        check("★ 公告响应落地前先比对房间（防延迟返回的公告把 currentRoomId 改回旧房间）",
              live_announce_guard,
              "没守卫 —— 旧房间的公告响应会把 currentRoomId 改回去，"
              "标题与实际连接对不上（新房间的消息会被房间过滤挡掉）")

        # ⑩~⑮ 「会话代次」：只在本次会话里有效的异步响应，落地前必须核对出发时的代次。
        #     第六轮 Codex 报的 3 条 + 1 个同构入口，根因都是同一个 —— 响应落地时没有核对
        #     「还是不是发起它的那一次会话」。所以收口成一个 sessionEpoch + epochAlive()，
        #     而不是在每个入口各写一份比对（各写一份就会「改一处漏一处」）。
        #     踩过的三条：① 甲查看资料的响应回来把丙的昵称/身份/金币覆盖成甲的；
        #     ② 旧的自动登录任务跑完还给新账号 connect()，重复建连把人挤掉、误判「另一个窗口进入」；
        #     ③ 保存大厅公告的响应回来时人已切到游戏房，把大厅公告写进游戏房侧栏。
        live_epoch_decl = bool(re.search(r"let\s+sessionEpoch\s*=\s*0", live_js))
        live_epoch_alive = bool(re.search(
            r"function\s+epochAlive\s*\([^)]*\)\s*\{\s*return\s+started\s*===\s*sessionEpoch", live_js))
        check("★★ 「会话代次」只有一处判据：sessionEpoch + epochAlive()",
              live_epoch_decl and live_epoch_alive,
              f"sessionEpoch 声明={live_epoch_decl} / epochAlive 判据={live_epoch_alive} —— "
              "缺了它，下面几条守卫都无从谈起")

        # ⑪ enterSession 起了新的一代，并在 connect() **之前**核对代次
        #    （不核对就会用**旧 token**给新账号多建一条连接）。
        #    ⚠️ 比位置前必须先剥掉行注释：`stopReconnect()` 含 "connect" 子串，
        #       而注释里也会写 `connect()` —— 两个都会把位置定到 epochAlive 之前（实测假红）。
        def strip_js_comment(text: str) -> str:
            """把 // 行注释抹成**等长空格**：长度不变，所以位置索引依然可用。"""
            out = []
            for line in text.split("\n"):
                cut = line.find("//")
                if cut >= 0:
                    line = line[:cut] + " " * (len(line) - cut)
                out.append(line)
            return "\n".join(out)

        live_enter_body = js_func("enterSession", "async function")
        live_enter_code = strip_js_comment(live_enter_body)
        live_enter_bump = "sessionEpoch += 1" in live_enter_code
        live_connect_at = re.search(r"(?<![A-Za-z0-9_$])connect\s*\(\s*\)", live_enter_code)
        live_epoch_at = live_enter_code.find("epochAlive(epoch)")
        live_enter_guard = (bool(live_connect_at) and live_epoch_at >= 0
                            and live_epoch_at < live_connect_at.start())
        check("★★ enterSession 起新一代，且 connect() 之前先核对代次",
              live_enter_bump and live_enter_guard,
              f"推高代次={live_enter_bump} / connect 前有守卫={live_enter_guard} —— "
              "否则旧会话的初始化任务会给新账号重复建连")

        # ⑫ logout 也要推高代次：让上一个账号的在飞响应（查资料 / 存资料 / 初始化）当场作废。
        check("★ logout 会推高「会话代次」（作废上一个账号的在飞响应）",
              "sessionEpoch += 1" in live_logout_body,
              "没推高 —— 上一个账号查资料的响应回来会把新账号的昵称、身份、金币整组覆盖掉")

        # ⑬ 自动登录（页面带 token 打开）也必须走带代次守卫的 restoreSession，
        #    不许在 onMounted 里内联一段「await 完了就 connect()」。
        live_restore_body = js_func("restoreSession", "async function")
        live_restore_guard = live_restore_body.count("epochAlive(epoch)") >= 2
        check("★★ 自动登录走 restoreSession，且两个 await 之后都核对代次",
              bool(live_restore_body) and live_restore_guard
              and "if (token.value) restoreSession();" in live_js,
              f"函数体={bool(live_restore_body)} / 守卫数={live_restore_body.count('epochAlive(epoch)')} "
              f"/ onMounted 调用={('if (token.value) restoreSession();' in live_js)} —— "
              "内联版本会在旧任务跑完时给新账号重复建连，把人挤下线")

        # ⑭ 查资料 / 存资料两条路的响应都会写 user，都必须核对代次。
        live_view_body = js_func("viewProfile", "async function")
        live_save_body = js_func("saveProfile", "async function")
        live_profile_guards = ("epochAlive(epoch)" in live_view_body,
                               "epochAlive(epoch)" in live_save_body)
        check("★★ 查看资料 / 保存资料的响应落地前都核对代次（两条都会写 user）",
              all(live_profile_guards),
              f"viewProfile={live_profile_guards[0]} / saveProfile={live_profile_guards[1]} —— "
              "result.user 是**请求者自己**的资料，跨会话合并会把新账号整组覆盖")

        # ⑮ 保存公告：发起时锁定房间，落地时比对（读路径 loadAnnouncement 已有同样的守卫）。
        live_save_ann_body = js_func("saveAnnouncement", "async function")
        live_save_ann_ok = ("const roomId = currentRoomId.value" in live_save_ann_body
                            and live_save_ann_body.count("roomId !== currentRoomId.value") >= 2)
        check("★ 保存公告：发起时锁定房间，落地前比对（读路径已有守卫，写路径也必须有）",
              live_save_ann_ok,
              "漏了 —— 保存大厅公告后切到游戏房，响应落地时会把大厅公告写进游戏房侧栏")

    # ---- 只读接口的鉴权面（2026-10-03 二十人模拟才发现）----
    # /api/messages（公聊历史）与 /api/online（在线名单）曾经**完全不带 token**。
    # 后果不是「局域网里多个陌生人能看」这么轻 —— 真正要命的是「踢人只踢了写、没踢读」：
    # 被请出聊天室的人 IP 已经被 check_entry 记进冷却、再也换不到新 token，
    # 但他照样能用这两条把整个房间的聊天记录和在线名单拉走。同类只读接口
    # （/api/activities、/api/rankings）本来就是要 token 的，只有这两条漏了。
    # 判据必须用 AST 锁到「那个路由函数体内真的调了 user_from_token」：
    # 全文搜子串会被别处的调用和注释骗过（这个坑在 activity_tier 上已经踩过一次）。
    if "app.py" in sources:
        auth_text = sources["app.py"]
        if isinstance(auth_text, bytes):
            auth_text = auth_text.decode("utf-8", "replace")

        try:
            auth_tree = ast.parse(auth_text)
        except SyntaxError:
            auth_tree = None

        def route_calls_auth(route_path: str) -> bool:
            """带 @app.<verb>("<route_path>") 装饰器的那个函数体里，有没有真的调 user_from_token。"""
            if auth_tree is None:
                return False
            for node in ast.walk(auth_tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                decorated = any(
                    isinstance(deco, ast.Call) and deco.args
                    and isinstance(deco.args[0], ast.Constant) and deco.args[0].value == route_path
                    for deco in node.decorator_list
                )
                if not decorated:
                    continue
                return any(isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                           and inner.func.id == "user_from_token" for inner in ast.walk(node))
            return False

        for guarded_path in ("/api/messages", "/api/online", "/api/activities", "/api/rankings"):
            guarded = route_calls_auth(guarded_path)
            check(f"★ 只读接口 {guarded_path} 必须过 user_from_token",
                  guarded,
                  "过了 ✓" if guarded else
                  "没鉴权 —— 被踢掉的人（IP 已在 check_entry 冷却里、换不到新 token）"
                  "照样能把房间聊天记录 / 在线名单拉走")

    return {"wordlist_text": file_text}


# ================================================================ L2
def layer_unit() -> None:
    head("L2 · 单元测试")

    # tests/ 没有 __init__.py，unittest.discover() 会以「不是可导入包」直接报错，
    # 所以逐个文件显式加载（模块内部自己插了项目根到 sys.path，不依赖包名）。
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    loaded: list[str] = []
    for path in sorted(TESTS.glob("test_*.py")):
        rel = path.relative_to(ROOT).as_posix()
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            check(f"加载测试文件 {rel}", False, "无法构造模块 spec")
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[path.stem] = module
        try:
            spec.loader.exec_module(module)
        except Exception as error:  # noqa: BLE001 - 导入期就炸也要变成一条失败记录，而不是崩掉整个检查
            check(f"加载测试文件 {rel}", False, f"{type(error).__name__}: {error}")
            continue
        suite.addTests(loader.loadTestsFromModule(module))
        loaded.append(rel)
        check(f"加载测试文件 {rel}", True)

    total = suite.countTestCases()
    if not total:
        check("L2 有用例可跑", False, "tests/ 下一个 test_*.py 都没加载进来")
        return
    print(f"  共 {len(loaded)} 个文件 / {total} 个用例，运行中…", flush=True)
    outcome = unittest.TextTestRunner(verbosity=1, stream=sys.stdout, buffer=True).run(suite)
    check(f"全部用例通过（{total} 个）", outcome.wasSuccessful(),
          f"失败 {len(outcome.failures)} / 错误 {len(outcome.errors)}")
    for case, trace in list(outcome.failures) + list(outcome.errors):
        print(f"    x {case}", flush=True)
        print("      " + trace.strip().splitlines()[-1][:160], flush=True)


# ================================================================ L3
def layer_e2e(port: int, workspace: Path, wordlist_text: str) -> Path | None:
    """起真实服务做端到端。返回服务日志路径（失败时要翻）。"""
    head(f"L3 · 端到端（真实服务 + WebSocket，端口 {port}）")

    env = {**os.environ}
    # 沙箱/公司代理会把 127.0.0.1 的请求也劫走，拿到 502 还以为服务坏了。这里显式摘掉。
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    env.update({"CHAT_PORT": str(port), "CHAT_DATA_DIR": str(workspace),
                # ★ 公告目录也必须隔离：它是「每个房间一份 txt」，真实目录里是老师自己维护的公告。
                #   不隔离的话，跑一次全量检查就会在项目根目录生成/改写 公告\<房间>.txt。
                "CHAT_ANNOUNCE_DIR": str(workspace / "公告"),
                "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1"})   # 别在项目里撒 __pycache__
    env.pop("PYTHONUTF8", None)

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(method: str, path: str, payload: dict | None = None, token: str | None = None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Session-Token"] = token
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers, method=method)
        try:
            with opener.open(req, timeout=15) as response:
                raw = response.read().decode("utf-8")
                return response.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8")
            try:
                return error.code, json.loads(raw)
            except json.JSONDecodeError:
                return error.code, raw

    async def recv_until(ws, wanted: str, timeout: float = 8.0):
        """一直收，直到出现想要的 type；返回 (那条消息, 途中见过的全部 type)。"""
        deadline = time.time() + timeout
        seen: list[str] = []
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), max(0.2, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            data = json.loads(raw)
            seen.append(data.get("type"))
            if data.get("type") == wanted:
                return data, seen
        return None, seen

    async def recv_matching(ws, wanted: str, predicate, timeout: float = 8.0):
        """一直收，直到出现「type 对得上、而且内容满足 predicate」的那条。

        比 recv_until 严：房主那一段每步都会收到好几条广播（system / users / profile_updated），
        只按 type 抓很容易抓到上一步的残留 —— 那种断言会假绿。所以内容也要对得上。
        """
        deadline = time.time() + timeout
        seen: list[str] = []
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), max(0.2, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            data = json.loads(raw)
            seen.append(data.get("type"))
            if data.get("type") == wanted and predicate(data):
                return data, seen
        return None, seen

    async def drain(ws) -> int:
        """把缓冲区里已有的消息读干净（动作前清场，进一步降低抓到残留的概率）。"""
        count = 0
        while count < 40:
            try:
                await asyncio.wait_for(ws.recv(), 0.15)
                count += 1
            except Exception:  # noqa: BLE001 - 超时或断开都算「读干净了」
                break
        return count

    # ---- 挑一个「真的会被拦下来」的违规样本：直接问产品代码，别猜 ----
    # 词库是用户可以自己改的，硬编码「草泥马」早晚会因为人家删掉而假红。
    os.environ["CHAT_DATA_DIR"] = str(workspace)   # 必须在 import app 之前设，避免碰到真实 data 目录
    os.environ["CHAT_ANNOUNCE_DIR"] = str(workspace / "公告")
    import app as app_module  # noqa: PLC0415
    # L2 可能已经在本进程里 import 过 app，那时环境变量还没设 —— 直接改模块属性兜底，
    # 免得本进程里任何一次 room_announcement() 都写进老师正在用的「公告」目录。
    app_module.ANNOUNCE_DIR = workspace / "公告"

    sample_word = ""
    for line in wordlist_text.split("\n"):
        text = line.strip()
        if not text or text.startswith("#") or "<-" in text or "->" in text or text.endswith("="):
            continue
        if 2 <= len(text) <= 4 and all("\u4e00" <= ch <= "\u9fff" for ch in text) \
                and app_module.moderate_content(f"你这个{text}东西")[1]:
            sample_word = text
            break
    if sample_word:
        check(f"词库里挑出一个真能拦下的样本（「{sample_word}」）", True)
    else:
        skip("违禁词拦截链路", "词库里没有 2~4 字的纯中文词条可用")

    process = subprocess.Popen(
        [sys.executable, str(ROOT / "app.py")],
        cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    log_path = workspace / "server.log"
    log_lines: list[str] = []

    def drain_log() -> None:
        """把服务输出收干。管道不读会被写满，服务会卡死。"""
        if process.stdout is None:
            return
        for raw in process.stdout:
            log_lines.append(raw.decode("utf-8", errors="replace").rstrip("\n"))
        if log_lines:
            log_path.write_text("\n".join(log_lines), encoding="utf-8")

    try:
        ready = False
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(0.4)
            if process.poll() is not None:
                break
            try:
                opener.open(f"http://127.0.0.1:{port}/", timeout=2).close()
                ready = True
                break
            except Exception:  # noqa: BLE001 - 还没起来，继续等
                continue
        if not check(f"服务启动成功（独立端口 {port} + 临时数据目录）", ready):
            drain_log()
            return log_path

        status, _ = request("GET", "/")
        check("首页 / 返回 200", status == 200, f"HTTP {status}")

        status, body = request("GET", "/api/rooms")
        rooms = body.get("rooms", []) if isinstance(body, dict) else []
        check("房间列表 /api/rooms", status == 200 and len(rooms) >= 5, f"{len(rooms)} 个房间")

        status, body = request("GET", "/api/shop")
        items = body.get("items", []) if isinstance(body, dict) else []
        check("商城 /api/shop", status == 200 and len(items) >= 15, f"{len(items)} 件物品")

        status, body = request("GET", "/api/avatars")
        avatars = body.get("avatars", []) if isinstance(body, dict) else []
        check("头像 /api/avatars", status == 200 and bool(avatars), f"{len(avatars)} 个")

        status, body = request("GET", "/api/announcement")
        notice = body.get("announcement", "") if isinstance(body, dict) else ""
        check("公告 /api/announcement", status == 200 and bool(notice), f"HTTP {status}")
        check("下发的公告已剔除 # 说明行",
              bool(notice) and not any(line.lstrip().startswith("#") for line in notice.splitlines()),
              repr(notice[:80]))

        # ---- 公告按房间：换个房间号必须换一份内容 ----
        status, body = request("GET", "/api/announcement?room_id=games")
        games_notice = body.get("announcement", "") if isinstance(body, dict) else ""
        check("公告按房间取（?room_id=games）", status == 200 and bool(games_notice), f"HTTP {status}")
        # 反向对照：游戏房的公告里必须写着自己的房间名，否则说明「按房间」只是换了个文件名。
        check("★ 游戏房的公告里写着自己的房间名（不是大厅那一份）",
              "游戏讨论" in games_notice, repr(games_notice[:60]))
        status, body = request("GET", "/api/announcement?room_id=no_such_room")
        fallback_notice = body.get("announcement", "") if isinstance(body, dict) else ""
        check("★ 房间号不认识时回落大厅（不报错、也不留空白）",
              status == 200 and fallback_notice == notice, f"HTTP {status}")
        # ★ /api/online 与 /api/messages（公聊）现在都要求 token。以前这两条「谁都能读」，
        #   被请出聊天室的人（IP 已被 check_entry 拦下、再也换不到新 token）照样能把
        #   在线名单和整个房间的聊天记录拉走 —— 踢人等于只踢了「写」，没踢「读」。
        status, _ = request("GET", "/api/online")
        check("★ 不带 token 读在线列表被拒（401）", status == 401, f"HTTP {status}")
        status, _ = request("GET", "/api/messages?room_id=lobby")
        check("★ 不带 token 读公聊历史被拒（401）", status == 401, f"HTTP {status}")

        status, body = request("POST", "/api/register",
                               {"username": "stu_a", "password": "pw-a-111", "nickname": "甲同学"})
        token_a = body.get("token") if isinstance(body, dict) else None
        check("注册学生甲", status == 200 and bool(token_a), f"HTTP {status}")
        # 反向对照：带上 token 立刻能读。和上面两条 401 配对，才排除「接口整个坏了所以都 401」。
        status, _ = request("GET", "/api/online", token=token_a)
        check("反向对照：带 token 读在线列表 200", status == 200, f"HTTP {status}")
        status, _ = request("GET", "/api/messages?room_id=lobby", token=token_a)
        check("反向对照：带 token 读公聊历史 200", status == 200, f"HTTP {status}")

        # 此刻甲还只是个普通用户（后面才会被任命为房主）——
        # 正好拿来做「先被拒、后被允许」的配对，证明 403 是权限判的、不是接口坏的。
        status, _ = request("POST", "/api/announcement",
                            {"room_id": "lobby", "content": "普通用户想改公告"}, token=token_a)
        check("★ 普通用户改公告被拒（403，还没被任命为房主）", status == 403, f"HTTP {status}")
        status, _ = request("POST", "/api/announcement",
                            {"room_id": "lobby", "content": "游客也想改"}, token=None)
        check("★ 没带 token 改公告被拒（401）", status in (401, 403), f"HTTP {status}")

        status, body = request("POST", "/api/login", {"username": "stu_a", "password": "pw-a-111"})
        check("登录学生甲", status == 200 and bool(body.get("token")), f"HTTP {status}")

        status, body = request("GET", "/api/me", token=token_a)
        me_user = body.get("user", {}) if isinstance(body, dict) else {}
        # ★ 注意：public() 里没有 username 字段，这里只能断言 nickname。
        check("/api/me 带 token 可读", status == 200 and me_user.get("nickname") == "甲同学", f"HTTP {status}")
        # ★ 本项目踩过两次的坑：只下发给本人的数据必须走 self_public()，用 public() 会少 inventory。
        check("/api/me 走的是 self_public（带 inventory）", "inventory" in me_user,
              "本人下发必须用 self_public()")
        status, _ = request("GET", "/api/me")
        check("/api/me 不带 token 返回 401", status == 401, f"HTTP {status}")

        status, _ = request("GET", "/api/activities?room_id=lobby", token=token_a)
        check("房间活动 /api/activities", status == 200, f"HTTP {status}")

        status, _ = request("GET", "/api/rankings?room_id=lobby&kind=room", token=token_a)
        check("排行榜 /api/rankings", status == 200, f"HTTP {status}")

        status, body = request("POST", "/api/guest", {"nickname": "路人甲"})
        check("游客进入 /api/guest", status == 200 and bool(body.get("token")), f"HTTP {status}")

        status, body = request("POST", "/api/login", {"username": "admin", "password": "admin123"})
        admin_token = body.get("token") if isinstance(body, dict) else None
        check("管理员登录", status == 200 and bool(admin_token), f"HTTP {status}")

        # ---------- 个人资料：按处计费 ----------
        status, body = request("POST", "/api/register",
                               {"username": "stu_b", "password": "pw-b-222", "nickname": "乙同学"})
        token_b = body.get("token") if isinstance(body, dict) else None
        # ★ user_id 是数据库里的数字 id，不是账号名 —— 查看资料按 id 找会话，别写成 "stu_b"。
        b_id = (body.get("user") or {}).get("id") if isinstance(body, dict) else None
        check("注册学生乙（注册流程未改动）", status == 200 and bool(token_b), f"HTTP {status}")
        check("新账号仍是 100 初始金币", (body.get("user") or {}).get("coins") == 100 if isinstance(body, dict) else False,
              f"coins={(body.get('user') or {}).get('coins') if isinstance(body, dict) else None}")

        status, body = request("POST", "/api/profile",
                               {"real_name": "李小明", "class_name": "八年级3班"}, token=token_b)
        check("改 2 处资料扣 2×5＝10 金币",
              status == 200 and isinstance(body, dict) and body.get("charged") == 10,
              f"HTTP {status} charged={(body or {}).get('charged') if isinstance(body, dict) else None}")
        check("改完的资料立刻回给本人（self_public）",
              isinstance(body, dict) and (body.get("user") or {}).get("real_name") == "李小明")

        status, _ = request("POST", "/api/profile", {"real_name": "李小明"}, token=token_b)
        check("传了但和现在一样的字段不收钱（整单拒绝）", status == 400, f"HTTP {status}")

        status, body = request("POST", "/api/profile", {"bio": "好好学习，天天向上"}, token=token_b)
        check("单独改个性签名一处扣 5 金币",
              status == 200 and isinstance(body, dict) and body.get("charged") == 5,
              f"HTTP {status} charged={(body or {}).get('charged') if isinstance(body, dict) else None}")
        check("改完的签名立刻回给本人",
              isinstance(body, dict) and (body.get("user") or {}).get("bio") == "好好学习，天天向上")

        status, body = request("POST", "/api/profile",
                               {"old_password": "pw-b-222", "new_password": "pw-b-333"}, token=token_b)
        check("改密码成功（验了原密码）", status == 200, f"HTTP {status}")
        status, _ = request("POST", "/api/login", {"username": "stu_b", "password": "pw-b-333"})
        check("★ 改完密码用新密码能登录", status == 200, f"HTTP {status}")
        status, _ = request("POST", "/api/login", {"username": "stu_b", "password": "pw-b-222"})
        check("改完密码旧密码立即失效", status == 401, f"HTTP {status}")

        status, body = request("GET", "/api/me", token=token_b)
        me_b = body.get("user", {}) if isinstance(body, dict) else {}
        check("本人的 /api/me 里带真实姓名与班级（设置面板要回填）",
              me_b.get("real_name") == "李小明" and me_b.get("class_name") == "八年级3班",
              f"real_name={me_b.get('real_name')} class_name={me_b.get('class_name')}")

        status, body = request("POST", "/api/profile/view", {"user_id": b_id}, token=admin_token)
        check("管理员查看学生资料免费，且资料卡里有真实姓名",
              status == 200 and isinstance(body, dict) and body.get("charged") == 0
              and (body.get("card") or {}).get("real_name") == "李小明",
              f"HTTP {status}")
        check("管理员能看到对方账号名（便于查人）",
              isinstance(body, dict) and (body.get("card") or {}).get("username") == "stu_b")

        status, body = request("POST", "/api/profile/view", {"user_id": b_id}, token=token_a)
        check("★ 学生查看一个没连上 WebSocket 的人 → 拒绝且不扣金币",
              status == 404, f"HTTP {status}")
        status, body = request("GET", "/api/me", token=token_a)
        check("★ 被拒的那次确实没扣金币",
              (body.get("user") or {}).get("coins") == 100,
              f"coins={(body.get('user') or {}).get('coins')}")

        # ---------- WebSocket ----------
        import websockets  # noqa: PLC0415

        async def websocket_probe() -> None:
            uri_a = f"ws://127.0.0.1:{port}/ws?token={token_a}"
            uri_b = f"ws://127.0.0.1:{port}/ws?token={token_b}"
            uri_admin = f"ws://127.0.0.1:{port}/ws?token={admin_token}"
            async with websockets.connect(uri_a, open_timeout=10) as ws_a:
                ready_msg, seen = await recv_until(ws_a, "ready")
                check("WebSocket 握手并收到 ready", ready_msg is not None, f"途中：{seen}")
                check("ready 里带完整本人信息（含 inventory）",
                      isinstance(ready_msg, dict) and "inventory" in (ready_msg.get("user") or {}),
                      "self_public() 判据")
                check("ready 里带公告（连上就能看到最新内容，老师改完不用重启服务）",
                      bool((ready_msg or {}).get("announcement")),
                      repr(str((ready_msg or {}).get("announcement", ""))[:60]))

                users_msg, seen = await recv_until(ws_a, "users")
                check("收到在线列表 users", users_msg is not None, f"途中：{seen}")

                async with websockets.connect(uri_admin, open_timeout=10) as ws_admin:
                    admin_ready, _ = await recv_until(ws_admin, "ready")
                    check("第二个连接（管理员）也能进", admin_ready is not None)

                    plain = "今天的作业交了吗"
                    await ws_a.send(json.dumps({"type": "message", "content": plain}))
                    broadcast, _ = await recv_until(ws_a, "message")
                    got = (broadcast or {}).get("message", {}).get("content")
                    check("正常消息原样广播（零误伤）", got == plain, repr(got))

                    admin_msg, seen = await recv_until(ws_admin, "message")
                    check("同房间的另一个用户收到同一条广播",
                          (admin_msg or {}).get("message", {}).get("content") == plain, f"途中：{seen}")

                    if sample_word:
                        payload = f"你这个{sample_word}东西"
                        await ws_a.send(json.dumps({"type": "message", "content": payload}))
                        notice, _ = await recv_until(ws_a, "system")
                        check(f"违禁词「{sample_word}」被拦并提示", notice is not None,
                              (notice or {}).get("message", "")[:40])
                        masked, _ = await recv_until(ws_a, "message")
                        content = (masked or {}).get("message", {}).get("content", "")
                        check("违规内容在广播里已被打码", sample_word not in content and "*" in content, repr(content))
                        check("打码后长度与原文一致", len(content) == len(payload),
                              f"{len(content)} vs {len(payload)}")

                    # ---------- 活动消息分档：实时收到 与 刷新后读回 必须同档 ----------
                    # ★ 只验「广播里带了 tier」是不够的，那只证明发出去的一刻是对的。
                    #   真正的坑在历史接口没把字段带回来 —— 表现是玩的时候折叠得好好的，
                    #   学生按一下 F5 全散开：不报错、不崩，只静默失效。所以两边一起核。
                    await drain(ws_a)
                    await ws_a.send(json.dumps({"type": "interaction", "action": "lottery"}))
                    live_msg, seen = await recv_matching(
                        ws_a, "message", lambda item: item.get("message", {}).get("kind") == "activity")
                    live = (live_msg or {}).get("message", {})
                    check("★ 抽奖提示实时广播时就带着 activity_kind / activity_tier",
                          live.get("activity_kind") == "lottery" and live.get("activity_tier") == "stream",
                          f"activity_kind={live.get('activity_kind')!r} tier={live.get('activity_tier')!r}　途中：{seen}")

                    status, body = await asyncio.to_thread(
                        request, "GET", "/api/messages?room_id=lobby&limit=50", None, token_a)
                    saved = body.get("messages", []) if isinstance(body, dict) else []
                    history = [item for item in saved if item.get("kind") == "activity"]
                    last = history[-1] if history else {}
                    check("★★ 刷新页面读回的历史档位与实时那条完全一致",
                          last.get("activity_kind") == live.get("activity_kind") == "lottery"
                          and last.get("activity_tier") == live.get("activity_tier") == "stream",
                          f"实时 tier={live.get('activity_tier')!r} / 历史读回 tier={last.get('activity_tier')!r}"
                          f"（activity_kind={last.get('activity_kind')!r}，共 {len(history)} 条活动消息）")
                    check("★ 普通聊天不会被带上档位（否则前端会把真人的话也折叠掉）",
                          all(item.get("activity_tier") is None
                              for item in saved if item.get("kind") != "activity"),
                          f"聊天记录共 {len(saved)} 条，其中非活动 {len(saved) - len(history)} 条都不带档位")

                    # ---------- 个人资料：付费字段不许免费广播 + 在线查看收费 ----------
                    # ★ 必须等乙真的连上 WebSocket 才做这两条断言：
                    #   在线名单只装「websocket 非空」的人，不连就查不到任何人的资料字段，
                    #   那样「没有泄露」只是名单为空造成的假绿。
                    async with websockets.connect(uri_b, open_timeout=10) as ws_b:
                        b_ready, _ = await recv_until(ws_b, "ready")
                        check("第三个连接（学生乙）也能进", b_ready is not None)

                        _, online_body = await asyncio.to_thread(request, "GET", "/api/online", None, token_a)
                        online_users = online_body.get("users", []) if isinstance(online_body, dict) else []
                        names = [item.get("nickname") for item in online_users]
                        # 反向对照：先确认乙确实在这个名单里，泄露检查才不是空跑。
                        check("反向对照：刚连上的乙确实出现在在线名单里", "乙同学" in names, f"名单：{names}")
                        leaked = sorted({key for item in online_users
                                         for key in ("real_name", "class_name", "username")
                                         if item.get(key)})
                        check("★ 在线列表里没有真实姓名/班级/账号名（付费字段不许免费广播）",
                              not leaked, f"泄露字段：{leaked}")
                        # 反向对照：签名不是付费字段，它必须出现在名单里。
                        # 和上面那条配对，才排除「名单是空的所以什么都没露」这种假绿。
                        listed_bios = [item.get("bio") or "" for item in online_users]
                        check("★ 在线列表里确实带着别人的个性签名（签名是公开的）",
                              "好好学习，天天向上" in listed_bios, f"名单里的签名：{listed_bios}")

                        status, body = await asyncio.to_thread(
                            request, "POST", "/api/profile/view", {"user_id": b_id}, token_a)
                        check("学生甲查看在线的学生乙，扣 1 金币",
                              status == 200 and isinstance(body, dict) and body.get("charged") == 1,
                              f"HTTP {status} charged={(body or {}).get('charged') if isinstance(body, dict) else None}")
                        card = body.get("card") or {} if isinstance(body, dict) else {}
                        check("★ 资料卡里能看到真实姓名与班级（付费才拿得到）",
                              card.get("real_name") == "李小明" and card.get("class_name") == "八年级3班",
                              f"real_name={card.get('real_name')} class_name={card.get('class_name')}")
                        check("★ 学生之间看不到对方的账号名（只有管理员能看）", "username" not in card,
                              f"card 里的键：{sorted(card)}")

                        status, body = await asyncio.to_thread(
                            request, "POST", "/api/profile/view", {"user_id": b_id}, token_a)
                        check("同一次登录里再看同一个人不再扣金币",
                              status == 200 and isinstance(body, dict) and body.get("charged") == 0,
                              f"HTTP {status}")

                        # ============ 房主：授权 / 按房间公告 / 按房间禁言 ============
                        # 甲乙此刻都在大厅。管理员把甲任命成「大厅」的房主，然后一步步验证房主的
                        # 权限边界。这几条全是「越权了也不会报错」的类型 —— 只有端到端跑得出来。
                        _, me_body = await asyncio.to_thread(request, "GET", "/api/me", None, token_a)
                        a_id = ((me_body or {}).get("user") or {}).get("id")
                        check("取到甲的 user_id（房主授权靠它找人）", a_id is not None, f"id={a_id}")

                        await drain(ws_admin)
                        await ws_admin.send(json.dumps({"type": "admin_action", "action": "set_owner",
                                                        "target_id": a_id, "room_id": "lobby"}))
                        receipt, seen = await recv_matching(
                            ws_admin, "info", lambda item: "房主" in str(item.get("message", "")))
                        check("管理员任命甲为「大厅」房主（有回执）", receipt is not None, f"途中：{seen}")

                        _, online_body = await asyncio.to_thread(request, "GET", "/api/online", None, token_a)
                        online_users = online_body.get("users", []) if isinstance(online_body, dict) else []
                        a_entry = next((item for item in online_users if item.get("id") == a_id), None)
                        b_entry = next((item for item in online_users if item.get("id") == b_id), None)
                        # 两条配对，缺一条都不算数：只查「甲是房主」的话，
                        # 「所有人都是房主」这种写坏也会绿；只查「乙不是」同理。
                        check("★ 房主身份随在线名单广播（owner=true）",
                              bool(a_entry) and a_entry.get("owner") is True,
                              f"甲的条目：{a_entry}")
                        check("反向对照：普通同学乙不带房主标记",
                              bool(b_entry) and b_entry.get("owner") is False,
                              f"乙的条目：{b_entry}")

                        # ---- 房主改公告：本房间可以，别的房间不行 ----
                        status, body = await asyncio.to_thread(
                            request, "POST", "/api/announcement",
                            {"room_id": "lobby", "content": "大厅公告：房主甲改的"}, token_a)
                        check("★ 房主能改自己房间的公告",
                              status == 200 and isinstance(body, dict)
                              and body.get("announcement") == "大厅公告：房主甲改的",
                              f"HTTP {status} {body if not isinstance(body, dict) else body.get('detail', '')}")
                        status, body = await asyncio.to_thread(
                            request, "POST", "/api/announcement",
                            {"room_id": "games", "content": "手伸到游戏房来了"}, token_a)
                        check("★ 房主改不了别的房间的公告（403）", status == 403, f"HTTP {status}")
                        _, games_body = await asyncio.to_thread(
                            request, "GET", "/api/announcement?room_id=games")
                        check("★ 被拒的那次确实没写进游戏房",
                              isinstance(games_body, dict)
                              and games_body.get("announcement") != "手伸到游戏房来了",
                              repr(str((games_body or {}).get("announcement", ""))[:40]))
                        # 改完立刻重取：缓存没失效的话这里会读回旧内容（页面显示「存了但没变」）。
                        _, lobby_body = await asyncio.to_thread(
                            request, "GET", "/api/announcement?room_id=lobby")
                        check("★ 房主改完马上能读到新公告（缓存已失效）",
                              isinstance(lobby_body, dict)
                              and lobby_body.get("announcement") == "大厅公告：房主甲改的",
                              repr(str((lobby_body or {}).get("announcement", ""))[:40]))
                        # 同房间的其他人要能「不刷新页面就看到」—— 靠 announcement_updated 广播。
                        pushed, seen = await recv_matching(
                            ws_b, "announcement_updated",
                            lambda item: item.get("room_id") == "lobby"
                            and item.get("announcement") == "大厅公告：房主甲改的")
                        check("★ 同房间的人立刻收到 announcement_updated（不用刷新）",
                              pushed is not None, f"途中：{seen}")
                        # 光刷新左侧面板不够 —— 正在打字的学生不会发现公告变了，
                        # 所以后面还跟一条聊天区系统提示（同一个发送队列，顺序有保证）。
                        noticed, notice_seen = await recv_matching(
                            ws_b, "system",
                            lambda item: "更新了本房间公告" in str(item.get("message", "")))
                        check("★ 聊天区插了一条「房主甲 更新了本房间公告」的系统提示",
                              noticed is not None and "房主" in str(noticed.get("message", "")),
                              f"收到的：{(noticed or {}).get('message', '')!r}　途中：{notice_seen}")

                        # ---- 房主越权：不能自己任命房主 ----
                        await drain(ws_a)
                        await ws_a.send(json.dumps({"type": "admin_action", "action": "set_owner",
                                                    "target_id": b_id, "room_id": "lobby"}))
                        denied, seen = await recv_matching(
                            ws_a, "error", lambda item: "管理员" in str(item.get("message", "")))
                        check("★ 房主不能自己任命房主（只有管理员能）", denied is not None, f"途中：{seen}")

                        # ---- 房主按房间禁言：本房间生效 ----
                        await drain(ws_a)
                        await drain(ws_b)
                        await ws_a.send(json.dumps({"type": "admin_action", "action": "mute",
                                                    "target_id": b_id, "minutes": 2}))
                        muted, seen = await recv_matching(
                            ws_b, "profile_updated",
                            lambda item: (item.get("user") or {}).get("id") == b_id
                            and (item.get("user") or {}).get("muted_until", 0) > time.time())
                        check("★ 房主禁言本房间的人 → 对方立刻收到禁言状态", muted is not None, f"途中：{seen}")
                        await ws_b.send(json.dumps({"type": "message", "content": "我还能说话吗"}))
                        blocked, seen = await recv_matching(
                            ws_b, "error", lambda item: "禁言" in str(item.get("message", "")))
                        check("★ 被禁言的人在**本房间**发不出消息", blocked is not None, f"途中：{seen}")

                        # ---- 换个房间：按房间禁言不该跟着人走 ----
                        await drain(ws_b)
                        await ws_b.send(json.dumps({"type": "join_room", "room_id": "games"}))
                        joined, seen = await recv_matching(
                            ws_b, "room_joined", lambda item: item.get("room_id") == "games")
                        check("乙换到游戏房", joined is not None, f"途中：{seen}")
                        await ws_b.send(json.dumps({"type": "message", "content": "换个房间就能说话了"}))
                        spoke, seen = await recv_matching(
                            ws_b, "message",
                            lambda item: (item.get("message") or {}).get("content") == "换个房间就能说话了")
                        check("★ 在别的房间不受大厅那份禁言影响（按房间隔离）", spoke is not None, f"途中：{seen}")

                        # ---- 房主管不到别的房间的人 ----
                        await drain(ws_a)
                        await ws_a.send(json.dumps({"type": "admin_action", "action": "mute",
                                                    "target_id": b_id, "minutes": 2}))
                        refused, seen = await recv_matching(
                            ws_a, "error",
                            lambda item: "管不了" in str(item.get("message", ""))
                            or "不是这个房间的房主" in str(item.get("message", "")))
                        check("★ 房主管不了别的房间的人（乙现在在游戏房）", refused is not None, f"途中：{seen}")

                        # ---- 房主不能全站禁言 ----
                        await drain(ws_a)
                        await ws_a.send(json.dumps({"type": "admin_action", "action": "mute_all",
                                                    "target_id": b_id, "minutes": 30}))
                        refused_all, seen = await recv_matching(
                            ws_a, "error", lambda item: "管理员" in str(item.get("message", "")))
                        check("★ 房主不能全站禁言（那是管理员专属）", refused_all is not None, f"途中：{seen}")

            # 伪造 token：服务端的设计是先发一条 error 说明原因、再关闭连接，
            # 所以「收到 error」和「直接断开」都算拒绝通过，只有收到正常业务消息才算漏。
            rejected, detail = False, ""
            try:
                async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token=bogus-token", open_timeout=10) as ws_bad:
                    try:
                        first = json.loads(await asyncio.wait_for(ws_bad.recv(), 5))
                        detail = f"收到 type={first.get('type')}"
                        rejected = first.get("type") == "error"
                    except asyncio.TimeoutError:
                        detail = "5 秒内既无回应也没断开"
                    except Exception as error:  # noqa: BLE001
                        rejected, detail = True, f"连接被关闭（{type(error).__name__}）"
            except Exception as error:  # noqa: BLE001
                rejected, detail = True, f"握手就被拒（{type(error).__name__}）"
            check("伪造 token 被拒（发 error 或直接断开）", rejected, detail)

        asyncio.run(websocket_probe())

        # ---------- 历史消息也要过词库 ----------
        status, body = request("GET", "/api/messages?room_id=lobby", None, token_a)
        texts = [item.get("content") for item in body.get("messages", [])] if isinstance(body, dict) else []
        check("历史消息能查回刚才发的正常消息", "今天的作业交了吗" in texts, f"{len(texts)} 条")
        check("历史消息里的违规内容同样已打码",
              not any(sample_word in (text or "") for text in texts) if sample_word else True)

        # ---------- 重置密码：服务运行中改，学生立刻生效 ----------
        manage.DB = workspace / "chatroom.db"
        names = [item["username"] for item in manage.list_students()]
        check("名单只列学生（不含 admin）", sorted(names) == ["stu_a", "stu_b"], str(names))

        ok, message = manage.reset_student_password("stu_a")
        check("服务运行中重置学生密码", ok, message)
        status, _ = request("POST", "/api/login", {"username": "stu_a", "password": manage.RESET_PASSWORD})
        check("★ 重置后新密码立刻能登录", status == 200, f"HTTP {status}")
        status, _ = request("POST", "/api/login", {"username": "stu_a", "password": "pw-a-111"})
        check("重置后旧密码已失效", status == 401, f"HTTP {status}")
        status, _ = request("POST", "/api/login", {"username": "admin", "password": "admin123"})
        check("管理员密码未受牵连", status == 200, f"HTTP {status}")

        ok, _ = manage.reset_student_password("admin")
        check("拒绝重置管理员密码", not ok)
        ok, _ = manage.reset_student_password("查无此人")
        check("拒绝重置不存在的账号", not ok)
    finally:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
        drain_log()

    return log_path


# ================================================================ L4
def layer_gui(workspace: Path) -> None:
    head("L4 · 控制台界面")

    import sqlite3  # noqa: PLC0415
    from contextlib import closing  # noqa: PLC0415

    import tkinter as tk  # noqa: PLC0415  （只是为了拿到 TclError 类型）

    import launcher  # noqa: PLC0415

    manage.DB = workspace / "chatroom.db"
    # ★ 这个库刻意造成「users 列不全 + 附属表齐全」：
    #   · users 只有 10 列（真实库里是靠 _ensure_column() 一列列补上去的），
    #     所以「缺列只让那一栏显示 —、不让窗口读不出来」这条能被真测到；
    #   · 同时建出 rooms / room_owners / messages / shop_items，
    #     派生列（物品中文名、房间名、发言条数、最后发言）才有东西可验。
    with closing(sqlite3.connect(manage.DB)) as db:
        db.executescript(
            "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,"
            " password_hash TEXT NOT NULL, nickname TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',"
            " created_at TEXT NOT NULL, level INTEGER, coins INTEGER, bio TEXT, inventory TEXT);"
            "CREATE TABLE rooms (id TEXT PRIMARY KEY, name TEXT, icon TEXT, description TEXT, created_at TEXT);"
            "CREATE TABLE room_owners (room_id TEXT, user_id TEXT, granted_at REAL,"
            " PRIMARY KEY (room_id, user_id));"
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id TEXT, kind TEXT,"
            " created_at TEXT);"
            "CREATE TABLE shop_items (id TEXT PRIMARY KEY, name TEXT, icon TEXT, price INTEGER,"
            " item_type TEXT, description TEXT, min_level INTEGER);"
        )
        for account, nickname, role, password, level, coins, bio, inventory in (
            ("admin", "管理员", "admin", "admin123", 9, 999, "我是管理员", '["basic_flower"]'),
            ("lisi", "李四", "user", "old-li", 5, 60, "八年级3班", '{"gift_rose": 2, "basic_applause": 1}'),
            ("zhangsan", "张三", "user", "old-zhang", 3, 30, "", "[]"),
        ):
            db.execute(
                "INSERT INTO users (username, password_hash, nickname, role, created_at, level, coins, bio,"
                " inventory) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (account, manage.hash_password(password), nickname, role, "2026-10-01 20:00:00",
                 level, coins, bio, inventory))
        db.execute("INSERT INTO rooms (id, name) VALUES ('lobby', '综合大厅')")
        db.execute("INSERT INTO room_owners (room_id, user_id, granted_at) VALUES ('lobby', '2', 0)")
        db.execute("INSERT INTO shop_items (id, name, icon, price, item_type, description, min_level)"
                   " VALUES ('gift_rose', '玫瑰花束', '🌹', 10, 'gift', '', 1)")
        for day in range(1, 6):
            db.execute("INSERT INTO messages (sender_id, kind, created_at) VALUES ('2', 'text', ?)",
                       (f"2026-10-0{day} 10:00:00",))
        db.commit()

    try:
        root = launcher.Launcher()
    except tk.TclError as error:
        skip("控制台界面", f"打不开图形界面（{error}）")
        return

    root.withdraw()
    root.update_idletasks()

    deadline = time.time() + 20
    while time.time() < deadline and root.deps_state == "checking":
        root.update()
        time.sleep(0.05)
    check("依赖检查完成（不会重复装包）", root.deps_state == "ready", root.deps_state)

    # 按钮按「服务行 / 账号数据行」两行摆（见 launcher._build），这里的顺序要和界面一致。
    buttons = [root.btn_start, root.btn_stop, root.btn_restart, root.btn_open,
               root.btn_members, root.btn_clear, root.btn_backup, root.btn_reset]
    labels = [button["text"] for button in buttons]
    check("控制台 8 个按钮齐全",
          labels == ["启动服务", "停止服务", "重启服务", "打开聊天室",
                     "成员列表", "清空聊天记录", "备份与恢复", "重置数据"],
          str(labels))

    # 行首标签要跟所在行对得上：文字一致 + 落在同一 grid 的对应行（0 上排 / 1 下排）。
    label_columns = []
    for row_name, row_label, expected_row, first_button in (
        ("服务控制", root.row_label_service, 0, root.btn_start),
        ("账号数据", root.row_label_data, 1, root.btn_members),
    ):
        info = row_label.grid_info()
        label_columns.append(int(info["column"]))
        check(f"「{row_name}」标签在同一行行首",
              row_label["text"] == row_name
              and row_label.master is first_button.master
              and int(info["row"]) == expected_row,
              f"文字={row_label['text']}　grid 行={info['row']} 列={info['column']}")
    check("两个行首标签在同一列（左右也对齐）",
          len(set(label_columns)) == 1, str(label_columns))

    # 「上下对齐」的实际含义：4 列 × 2 行宽度完全一致。
    # 只比「同列上下等宽」是不够的 —— 两排同位置的按钮字数正好一样（4/5/4/5）时，
    # 那条断言即使把 uniform 去掉也恒真（验牙实测：去掉后是 86/110/86/98，上下照样相等），
    # 所以必须比「8 个宽度全等」，它才真咬住等宽这件事。
    for _ in range(10):
        root.update()
    widths = [button.winfo_width() for button in buttons]
    if min(widths) <= 1:
        check("拿到按钮实际宽度（窗口已渲染）", False, f"实测宽度 {widths}，窗口没真正画出来")
    else:
        check("4 列 × 2 行按钮宽度完全一致（上下逐列对齐）", len(set(widths)) == 1,
              f"上排 {widths[:4]} / 下排 {widths[4:]}")

    # 溢出按实际渲染位置算：谁的右边缘越过窗口右边界就点名。
    # 默认窗口和最小窗口各量一次 —— 窄窗口才是最容易把右边按钮挤出去的情况。
    def overflowed() -> list[str]:
        edge = root.winfo_rootx() + root.winfo_width() - 12
        return [button["text"] for button in buttons
                if button.winfo_rootx() + button.winfo_width() > edge]

    wide = overflowed()
    check("按钮都没越过窗口右边界（默认 780 窗口）", not wide,
          ("越界：" + "、".join(wide)) if wide else "")
    root.geometry("680x520")
    for _ in range(10):
        root.update()
    narrow = overflowed()
    check("窗口缩到最小 680x520 时按钮也不越界", not narrow,
          ("越界：" + "、".join(narrow)) if narrow else "")
    root.geometry("780x620")
    for _ in range(10):
        root.update()

    # ---- 备份与恢复窗口：备份和还原是两个独立入口，不能混成一个按钮 ----
    root.on_backup()
    for _ in range(16):
        root.update()
        time.sleep(0.05)
    backup_dialog = root.backup_window
    check("备份与恢复窗口能打开", backup_dialog is not None and backup_dialog.winfo_exists())
    if backup_dialog is not None and backup_dialog.winfo_exists():
        check("窗口里有「立即备份」按钮", backup_dialog.btn_new["text"] == "立即备份",
              backup_dialog.btn_new["text"])
        check("窗口里有「还原选中的备份」按钮",
              backup_dialog.btn_restore["text"] == "还原选中的备份",
              backup_dialog.btn_restore["text"])
        check("没选中备份时「还原」按钮是灰的",
              str(backup_dialog.btn_restore["state"]) == "disabled",
              str(backup_dialog.btn_restore["state"]))
        backup_dialog._close()  # noqa: SLF001 - 测试里直接收掉弹窗
        for _ in range(6):
            root.update()

    # ---- 成员列表：全部账号 + 全部信息 + 就地重置密码 ----
    root.on_members()
    dialog = None
    deadline = time.time() + 20
    while time.time() < deadline:
        root.update()
        dialog = root.member_window
        if dialog is not None and (dialog.members or not dialog.loading):
            break
        time.sleep(0.05)
    check("成员列表窗口能打开", dialog is not None and dialog.winfo_exists())
    if dialog is None or not dialog.winfo_exists():
        root.closing = True
        root.destroy()
        return

    # ★ 这里用的是最小 users 表（只有 6 列）——users 表在真实库里是靠 _ensure_column()
    #   一列列补上去的，老库缺列只该让那一栏显示「—」，不该整个窗口读不出来。
    check("★ 老库缺列也能读全名单（users 缺经验 / 姓名 / 班级等列）", len(dialog.members) == 3,
          str([m.get("username") for m in dialog.members]))
    check("列出的是全部账号（含管理员），不只是学生",
          {m.get("username") for m in dialog.members} == {"admin", "lisi", "zhangsan"},
          str(sorted(str(m.get("username")) for m in dialog.members)))

    member_keys = set().union(*(m.keys() for m in dialog.members))
    check("★ 成员数据里没有任何密码字段", not any("password" in key for key in member_keys),
          str(sorted(member_keys)))
    check("★ 列表里没有一列是密码",
          all("密码" not in dialog.tree.heading(col)["text"] for col in dialog.tree["columns"]),
          str([dialog.tree.heading(col)["text"] for col in dialog.tree["columns"]]))
    check("默认把管理员排在最前", str(dialog.shown[0].get("role")) == "admin",
          str([m.get("username") for m in dialog.shown]))

    by_name = {m.get("username"): m for m in dialog.members}
    # 物品栏：一条老式「id 数组」、一条新式「id → 数量」，两种形态都要认。
    check("★ 物品栏解析出中文名（不在 shop_items 里的基础礼物也认得）",
          "鲜花" in by_name["admin"]["items_text"] and "basic_flower" not in by_name["admin"]["items_text"],
          by_name["admin"]["items_text"])
    check("★ 新式物品栏（id → 数量）解析正确",
          by_name["lisi"]["items_text"] == "掌声 ×1、玫瑰花束 ×2", by_name["lisi"]["items_text"])
    check("空物品栏显示「—」而不是空白", by_name["zhangsan"]["items_text"] == "—",
          by_name["zhangsan"]["items_text"])
    check("★ 房主房间显示房间名而不是房间号", by_name["lisi"]["room_owner_text"] == "综合大厅",
          by_name["lisi"]["room_owner_text"])
    check("发言条数与最后发言都从 messages 表算出来",
          by_name["lisi"]["messages"] == 5 and by_name["lisi"]["last_spoke"] == "2026-10-05 10:00:00",
          f"{by_name['lisi']['messages']} 条 / {by_name['lisi']['last_spoke']}")
    check("没发过言的人发言数不是 0 而是「没有记录」",
          by_name["zhangsan"]["messages"] is None, str(by_name["zhangsan"]["messages"]))
    check("缺列在详情里显示为空，不报错（等级/金币有值、经验缺列）",
          by_name["lisi"]["level"] == 5 and "exp" not in by_name["lisi"],
          f"level={by_name['lisi'].get('level')} exp={by_name['lisi'].get('exp')}")

    def pick(index: int) -> None:
        """选中第 index 行。

        ★ 直接调处理函数：窗口处于 withdraw 状态时虚拟事件 <<TreeviewSelect>> 不一定派发，
          靠它触发会让下面的断言全部假红。selection_set 仍然照做，保持与真人操作一致。
        """
        dialog.tree.selection_set(dialog.tree.get_children()[index])
        dialog._on_select()  # noqa: SLF001 - 父子窗口之间的既有约定
        root.update()

    check("未选中时「重置密码」按钮是灰的", str(dialog.btn_reset["state"]) == "disabled")

    pick(0)
    detail_text = dialog.detail.get("1.0", "end")
    check("选中管理员时「重置密码」依然是灰的（后端也只重置学生）",
          str(dialog.btn_reset["state"]) == "disabled", str(dialog.btn_reset["state"]))
    check("详情区按字段清单逐条显示（缺列也不报错）",
          all(header in detail_text for header, _ in manage.MEMBER_FIELDS),
          detail_text[:60].replace("\n", "|"))
    check("详情区有内容（不是空面板）", len(detail_text.strip()) > 40, str(len(detail_text)))

    pick(1)
    check("选中学生后「重置密码」可点", str(dialog.btn_reset["state"]) == "normal")

    dialog.search_var.set("zhang")
    root.update()
    check("搜索能过滤（昵称 / 账号 / 姓名 / 班级 / 签名）",
          [m.get("username") for m in dialog.shown] == ["zhangsan"],
          str([m.get("username") for m in dialog.shown]))
    dialog.search_var.set("")
    root.update()
    check("清空搜索后恢复全部", len(dialog.shown) == 3, str(len(dialog.shown)))

    dialog._set_role_filter("admin")  # noqa: SLF001
    root.update()
    check("「只看管理员」只剩管理员", [m.get("username") for m in dialog.shown] == ["admin"],
          str([m.get("username") for m in dialog.shown]))
    dialog._set_role_filter("all")  # noqa: SLF001
    root.update()

    csv_text = manage.members_csv(dialog.members)
    head_line = csv_text.splitlines()[0]
    check("★ 导出的 CSV 表头里没有密码列", "密码" not in head_line, head_line)
    check("CSV 与详情用的是同一份字段清单（首列一致）",
          head_line.split(",")[0] == manage.MEMBER_FIELDS[0][0], head_line[:40])
    check("CSV 行数与成员数一致", len(csv_text.splitlines()) == len(dialog.members) + 1,
          f"{len(csv_text.splitlines())} 行 / {len(dialog.members)} 人")
    check("★ CSV 防公式注入：以 = 开头的内容加了 ' 前缀",
          manage.member_cell("=1+1") == "'=1+1", manage.member_cell("=1+1"))

    logged: list[tuple[bool, str]] = []
    dialog.on_done = lambda ok, message: logged.append((ok, message))
    target = dialog._selected() or {}
    check("筛选 / 排序之后仍然记着原来选中的是谁",
          bool(target.get("username")), str([m.get("username") for m in dialog.shown]))
    dialog.busy = True
    dialog._reset_worker(target)
    deadline = time.time() + 8
    while time.time() < deadline and not logged:
        root.update()
        time.sleep(0.05)
    check("重置走通并回调主窗口", bool(logged) and logged[0][0], str(logged))
    with closing(sqlite3.connect(manage.DB)) as db:
        stored_row = db.execute("SELECT password_hash FROM users WHERE username = ?",
                                (target.get("username"),)).fetchone()
    check("★ 库里写的是新密码",
          bool(stored_row) and manage.verify_password(manage.RESET_PASSWORD, stored_row[0]),
          str(target.get("username")))
    marks = " ".join(str(value) for iid in dialog.tree.get_children()
                     for value in dialog.tree.item(iid, "values"))
    check("重置过的账号在名单里打上勾", "✓" in marks, marks[:80])
    check("窗口提示变绿", str(dialog.hint["fg"]) == launcher.GREEN, str(dialog.hint["fg"]))

    dialog._close()
    root.update()
    check("成员列表能正常关闭", not dialog.winfo_exists())
    root.closing = True
    root.destroy()


# ================================================================ 入口
ALL_LAYERS = ("L1", "L2", "L3", "L4")
LAYER_TITLES = {
    "L1": "L1 · 静态检查",
    "L2": "L2 · 单元测试",
    "L3": "L3 · 端到端",
    "L4": "L4 · 控制台界面",
}


def parse_args(argv: list[str] | None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="full_check.py",
        description="局域网互动聊天室 · 全量检查（全程不碰真实数据库、不占 9000 端口）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="耗时参考：L1 一秒内 / L3 约 6 秒 / L4 约 4 秒 / L2 约 40 秒（要跑 PBKDF2，最慢的一层）\n"
               "退出码：0 全过 / 1 有失败 / 2 参数或环境不对",
    )
    parser.add_argument("--only", default="", metavar="L1,L2",
                        help="只跑这几层（L1 静态 / L2 单元 / L3 端到端 / L4 控制台，逗号分隔）")
    parser.add_argument("--no-gui", action="store_true", help="跳过 L4 控制台（没有桌面环境时用）")
    parser.add_argument("--port", type=int, default=0, help="端到端用哪个端口（默认自动挑一个空闲的）")
    parser.add_argument("--keep-temp", action="store_true", help="保留临时目录，失败后好翻现场")
    return parser.parse_args(argv)


def resolve_layers(args: argparse.Namespace) -> set[str] | None:
    """算出这次要跑哪几层。返回 None 表示参数写错了（调用方直接退出）。"""
    if not args.only:
        layers = set(ALL_LAYERS)
    else:
        wanted = {part.strip().upper() for part in args.only.split(",") if part.strip()}
        unknown = wanted - set(ALL_LAYERS)
        if unknown:
            print(f"未知的层：{'、'.join(sorted(unknown))}　只支持 L1 / L2 / L3 / L4")
            return None
        layers = wanted
    if args.no_gui:
        layers.discard("L4")
    return layers


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    layers = resolve_layers(args)
    if layers is None:
        return 2
    if not layers:
        print("这次一层都不跑，没有意义。去掉 --only / --no-gui 再试。")
        return 2

    # ---- 先体检再开跑：缺依赖时给一句人话，而不是让 traceback 糊满屏幕 ----
    missing: list[str] = []
    for name in ("fastapi", "uvicorn", "websockets"):
        if importlib.util.find_spec(name) is None:
            missing.append(name)
    print(f"{LINE}\n局域网互动聊天室 · 全量检查\n{LINE}")
    print(f"  项目目录：{ROOT}")
    print(f"  解释器　：{sys.executable}")
    print(f"  本次跑　：{'、'.join(sorted(layers))}")
    if missing:
        print(f"  依赖缺失：{'、'.join(missing)}")
        print("  请用项目的虚拟环境跑：.venv\\Scripts\\python.exe tests\\full_check.py"
              "（或先双击「聊天室控制台.bat」把依赖装上）")
        return 2
    if importlib.util.find_spec("tkinter") is None:
        print("  提示　　：这个解释器没有 tkinter，L4 会自动跳过")

    started = time.time()
    workspaces: list[Path] = []
    context: dict = {"wordlist_text": ""}
    log_path: Path | None = None
    port = args.port or free_port()
    # 开跑前给老师自己的文件拍个快照，跑完核对 —— 见 pollution_snapshot 的说明。
    baseline = pollution_snapshot()

    try:
        if "L1" in layers:
            context = layer_static()
        else:
            head(LAYER_TITLES["L1"])
            skip("静态检查", "本次没有选中 L1")

        if "L2" in layers:
            layer_unit()
        else:
            head(LAYER_TITLES["L2"])
            skip("单元测试", "本次没有选中 L2")

        if "L3" in layers:
            workspace = Path(tempfile.mkdtemp(prefix="lan-chat-check-"))
            workspaces.append(workspace)
            log_path = layer_e2e(port, workspace, context.get("wordlist_text", ""))
        else:
            head(LAYER_TITLES["L3"])
            skip("端到端检查", "本次没有选中 L3")

        if "L4" in layers:
            if importlib.util.find_spec("tkinter") is None:
                head(LAYER_TITLES["L4"])
                skip("控制台界面", "这个解释器没有 tkinter")
            else:
                gui_workspace = Path(tempfile.mkdtemp(prefix="lan-chat-check-gui-"))
                workspaces.append(gui_workspace)
                try:
                    layer_gui(gui_workspace)
                except Exception as error:  # noqa: BLE001 - 界面层崩了也不能把整份报告吞掉
                    check("控制台界面层跑完", False, f"{type(error).__name__}: {error}")
        else:
            head(LAYER_TITLES["L4"])
            skip("控制台界面", "本次没有选中 L4")
    finally:
        if not args.keep_temp:
            for path in workspaces:
                shutil.rmtree(path, ignore_errors=True)
        else:
            for path in workspaces:
                print(f"\n  临时目录已保留：{path}", flush=True)
    stop_section()

    # ---- 收尾：核对项目目录有没有被本次检查写脏 ----
    head("收尾 · 项目目录污染核对")
    pollution = pollution_diff(baseline)
    check("跑完之后项目里的公告 / 词库文件一处都没被动过", not pollution,
          "；".join(pollution) + "　—— 说明有测试没隔离好路径"
          if pollution else "公告\\、公告.txt、违禁词库.txt 均未改动")

    # ---- 汇总 ----
    print(f"\n{'=' * 64}\n汇 总\n{'=' * 64}")
    section = None
    failed: list[str] = []
    passed = skipped = 0
    for group, name, status, _detail in RESULTS:
        if group != section:
            section = group
            print(f"\n{group}　（{SECTION_SECONDS.get(group, 0.0):.1f} 秒）", flush=True)
        print(f"  {status:<4} {name}", flush=True)
        if status == "PASS":
            passed += 1
        elif status == "FAIL":
            failed.append(f"{group} / {name}")
        else:
            skipped += 1

    elapsed = time.time() - started
    print(f"\n共 {len(RESULTS)} 项：通过 {passed}，失败 {len(failed)}，跳过 {skipped}"
          f"　（总耗时 {elapsed:.1f} 秒）", flush=True)
    if failed:
        print("\n失败清单：", flush=True)
        for item in failed:
            print(f"  - {item}", flush=True)
        if log_path is not None and log_path.exists():
            print("\n--- 服务日志尾部 ---", flush=True)
            print(log_path.read_text(encoding="utf-8", errors="replace")[-1500:], flush=True)
        return 1
    print("全部检查通过。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())