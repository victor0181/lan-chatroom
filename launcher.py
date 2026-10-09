"""局域网互动聊天室 · 控制台。

双击根目录的「聊天室控制台.bat」打开这个窗口，启动 / 停止 / 重启 / 备份 / 还原 / 重置
全部用鼠标点按钮完成，不需要记任何命令。

「成员列表」是一个只读的成员台账：一眼看全部注册账号，选中一个看他的全部信息（不含密码），
需要时就在同一个窗口里把他的密码重置掉 —— 重置密码原来是独立弹窗，现在并进这里了。

注意：所有中文提示都写在这里 —— .bat 里必须一个非 ASCII 字符都没有（cmd 按字节
边读边执行，混进中文会整行错位报「'xx' 不是内部或外部命令」）。
"""
from __future__ import annotations

import ctypes
import hashlib
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import manage  # noqa: E402  只依赖标准库，第三方依赖还没装好时也能先把窗口开出来

TOOL_BUILD = "2026-10-05"
PORT = manage.PORT
LOG_FILE = manage.LOGS / "server.log"
REQUIREMENTS = ROOT / "requirements.txt"
DEPS_MARKER = ROOT / ".venv" / ".deps_ok"

FONT = ("Microsoft YaHei UI", 9)
FONT_BOLD = ("Microsoft YaHei UI", 9, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 14, "bold")
FONT_MONO = ("Consolas", 9)

BG = "#eef4f9"
PANEL = "#ffffff"
BORDER = "#9db5c8"
TEXT = "#1f4460"
MUTED = "#7d8b96"
GREEN = "#2e9e57"
BLUE = "#2b6cb0"
RED = "#c0392b"
PURPLE = "#8e44ad"
TEAL = "#0f8b8d"
ORANGE = "#c97b1d"
DISABLED_BG = "#ccd6de"


def console_python() -> str:
    """优先用带控制台的解释器；pythonw 下 pip 和子进程的行为都更别扭。"""
    interpreter = Path(sys.executable)
    console = interpreter.with_name("python.exe")
    return str(console if console.exists() else interpreter)


class Launcher(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"局域网互动聊天室 · 控制台    （版本 {TOOL_BUILD}）")
        self.configure(bg=BG)
        self.geometry("780x620")
        self.minsize(680, 520)
        self.events: queue.Queue = queue.Queue()
        self.status = {"running": False, "pids": [], "others": [], "responds": False}
        self.busy = False
        self.member_window: MemberDialog | None = None
        self.backup_window: BackupDialog | None = None
        self.deps_state = "checking"  # checking / installing / ready / failed
        # 只看"打开窗口之后"产生的新输出：把历史日志整段倒出来，用户会以为刚出了错。
        # 想看以前的内容，点「打开日志文件」。
        self.log_offset = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
        self.closing = False
        self._build()
        self._center()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        threading.Thread(target=self._status_worker, daemon=True).start()
        threading.Thread(target=self._log_worker, daemon=True).start()
        self.after(120, self._drain_events)
        self.after(300, self._bootstrap)
        self.log(f"控制台已打开，版本 {TOOL_BUILD}。")
        self.log(f"教师机本机地址 http://127.0.0.1:{PORT}　学生机地址 http://{manage.lan_ip()}:{PORT}")

    # ---------------------------------------------------------------- 界面
    def _build(self) -> None:
        outer = tk.Frame(self, bg=BG, padx=12, pady=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        card = tk.Frame(outer, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        card.grid(row=0, column=0, sticky="ew")
        card.columnconfigure(1, weight=1)

        self.canvas = tk.Canvas(card, width=18, height=18, bg=PANEL, highlightthickness=0)
        self.canvas.grid(row=0, column=0, rowspan=2, padx=(12, 8), pady=10)
        self.dot = self.canvas.create_oval(3, 3, 15, 15, fill=BG, outline="")

        self.status_label = tk.Label(card, text="正在检查服务状态…", bg=PANEL, fg=TEXT, font=FONT_TITLE)
        self.status_label.grid(row=0, column=1, sticky="sw", pady=(10, 0))
        self.status_hint = tk.Label(card, text="", bg=PANEL, fg=MUTED, font=FONT)
        self.status_hint.grid(row=1, column=1, sticky="nw", pady=(0, 10))

        self.addr_label = tk.Label(card, text="", bg=PANEL, fg=TEXT, font=FONT_MONO, justify="left", anchor="e")
        self.addr_label.grid(row=0, column=2, rowspan=2, sticky="e", padx=12)
        self._update_addresses()

        # 按钮分两行：8 个按钮挤一行放不下（实测约 788px > 窗口可用的 756px），
        # 而且按「服务控制」「账号数据」分开，破坏性按钮离日常按钮也远一点。
        #
        # 两行放进同一个 grid，4 个按钮列挂同一个 uniform 组 → 4 列宽度自动对齐到
        # 「最宽的那个按钮」（清空聊天记录），上下两排因此逐列等宽、左右都齐。
        # 不设 weight，按钮就保持紧凑的固定宽度，不会随窗口变胖。
        # 标签占第 0 列（也是 grid 列，两行标签因此天然对齐）。
        # ★ 行首标签一律写成 4 个字：「服务控制」「账号数据」一样宽，列 0 的宽度就不会变。
        #   多写一个字（比如「账号与数据」）会把整排按钮往右推十几像素，
        #   680x520 这个最小窗口下右边那颗就可能越界（L4 有实测断言）。
        bar = tk.Frame(outer, bg=BG)
        bar.grid(row=1, column=0, sticky="ew", pady=(10, 8))
        for column in range(1, 5):
            bar.columnconfigure(column, uniform="button")
        self.row_label_service = self._row_label(bar, "服务控制", row=0)
        self.row_label_data = self._row_label(bar, "账号数据", row=1)
        self.btn_start = self._button(bar, "启动服务", self.on_start, GREEN, row=0, column=1)
        self.btn_stop = self._button(bar, "停止服务", self.on_stop, RED, row=0, column=2)
        self.btn_restart = self._button(bar, "重启服务", self.on_restart, BLUE, row=0, column=3)
        self.btn_open = self._button(bar, "打开聊天室", self.on_open, BLUE, row=0, column=4)
        self.btn_members = self._button(bar, "成员列表", self.on_members, TEAL, row=1, column=1)
        self.btn_clear = self._button(bar, "清空聊天记录", self.on_clear, ORANGE, row=1, column=2)
        self.btn_backup = self._button(bar, "备份与恢复", self.on_backup, BLUE, row=1, column=3)
        self.btn_reset = self._button(bar, "重置数据", self.on_reset, PURPLE, row=1, column=4)

        log_frame = tk.Frame(outer, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        head = tk.Frame(log_frame, bg=PANEL)
        head.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 2))
        tk.Label(head, text="运行记录", bg=PANEL, fg=TEXT, font=FONT_BOLD).pack(side="left")
        tk.Button(head, text="清空", command=self.clear_log, font=FONT, relief="flat", bg="#e3ebf2",
                  activebackground="#d2dee8", fg=TEXT, cursor="hand2", padx=8).pack(side="right")
        tk.Button(head, text="打开日志文件", command=self.open_log_file, font=FONT, relief="flat", bg="#e3ebf2",
                  activebackground="#d2dee8", fg=TEXT, cursor="hand2", padx=8).pack(side="right", padx=6)

        self.log_box = tk.Text(log_frame, height=14, font=FONT_MONO, bg="#fbfdff", fg=TEXT,
                               relief="flat", wrap="word", state="disabled", bd=0)
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=(8, 0), pady=(0, 8))
        scroll = tk.Scrollbar(log_frame, orient="vertical", command=self.log_box.yview)
        scroll.grid(row=1, column=1, sticky="ns", padx=(0, 6), pady=(0, 8))
        self.log_box.configure(yscrollcommand=scroll.set)
        self.log_box.tag_configure("warn", foreground="#b9770e")
        self.log_box.tag_configure("error", foreground=RED)
        self.log_box.tag_configure("good", foreground=GREEN)
        self.log_box.tag_configure("server", foreground=MUTED)

        footer = tk.Label(outer, bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 8), anchor="w", justify="left",
                          text=f"数据文件：{manage.DB}　|　改了代码或换了新版，要把这个窗口关掉再重新双击 bat，"
                               f"否则窗口里跑的仍是旧代码。")
        footer.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self._refresh_buttons()

    def _row_label(self, parent: tk.Frame, text: str, row: int) -> tk.Label:
        """行首小标题：说明这一排按钮管什么。

        放在统一的 grid 第 0 列，两行标签天然左右对齐。
        别设 width —— 中文按整字裁切，设小了文字会被截掉。
        """
        label = tk.Label(parent, text=text, bg=BG, fg=MUTED, font=FONT_BOLD)
        label.grid(row=row, column=0, sticky="w", padx=(2, 8), pady=3)
        return label

    def _button(self, parent: tk.Frame, text: str, command, color: str, row: int, column: int) -> tk.Button:
        button = tk.Button(parent, text=text, command=command, font=FONT_BOLD, fg="#ffffff", bg=color,
                           activeforeground="#ffffff", activebackground=color, relief="flat", bd=0,
                           padx=16, pady=8, cursor="hand2")
        button._base_color = color  # noqa: SLF001 - 仅供本类刷新用
        # sticky="ew"：列被 uniform 拉宽时按钮跟着撑满，这样上下两排才一样宽。
        button.grid(row=row, column=column, sticky="ew", padx=(0, 8), pady=3)
        return button

    def _center(self) -> None:
        self.update_idletasks()
        x = max(0, (self.winfo_screenwidth() - self.winfo_width()) // 2)
        y = max(0, (self.winfo_screenheight() - self.winfo_height()) // 3)
        self.geometry(f"+{x}+{y}")

    def _update_addresses(self) -> None:
        self.addr_label.configure(text=f"教师机：http://127.0.0.1:{PORT}\n学生机：http://{manage.lan_ip()}:{PORT}")

    def _refresh_buttons(self) -> None:
        running = bool(self.status.get("running"))
        ready = self.deps_state == "ready"
        busy = self.busy
        for button, enabled in (
            (self.btn_start, ready and not running),
            (self.btn_stop, running),
            (self.btn_restart, ready and running),
            (self.btn_members, not busy),
            (self.btn_clear, not busy),
            (self.btn_backup, ready),
            (self.btn_reset, not busy),
            (self.btn_open, True),
        ):
            usable = enabled and not busy
            button.configure(state="normal" if usable else "disabled",
                             bg=button._base_color if usable else DISABLED_BG,  # noqa: SLF001
                             fg="#ffffff" if usable else "#7f8d99")

    def _apply_status(self, payload: dict) -> None:
        self.status = payload
        running = bool(payload.get("running"))
        if running:
            self.canvas.itemconfigure(self.dot, fill=GREEN)
            self.status_label.configure(text="● 服务运行中", fg=GREEN)
            pids = "、".join(str(pid) for pid in payload.get("pids", []))
            hint = f"进程号 {pids}　端口 {PORT}"
            if not payload.get("responds"):
                hint += "　（端口在监听但连不上，可能还在启动中）"
            self.status_hint.configure(text=hint)
        else:
            self.canvas.itemconfigure(self.dot, fill="#b0bcc7")
            self.status_label.configure(text="○ 服务已停止", fg=MUTED)
            if payload.get("others"):
                self.status_hint.configure(
                    text=f"注意：端口 {PORT} 被别的程序占用（进程号 {'、'.join(str(p) for p in payload['others'])}），"
                         f"控制台不会去关它。")
            else:
                self.status_hint.configure(text=f"端口 {PORT} 空闲，点「启动服务」就能开始上课。")
        self._refresh_buttons()

    # ---------------------------------------------------------------- 日志
    def log(self, text: str, tag: str = "info") -> None:
        self.events.put(("log", (text, tag)))

    def _append(self, text: str, tag: str = "info") -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text.rstrip() + "\n", tag)
        if int(self.log_box.index("end-1c").split(".")[0]) > 2500:
            self.log_box.delete("1.0", "500.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def open_log_file(self) -> None:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOG_FILE.touch(exist_ok=True)
        if sys.platform == "win32":
            import os  # noqa: PLC0415
            os.startfile(LOG_FILE)  # noqa: S606
        else:
            webbrowser.open(LOG_FILE.as_uri())

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    text, tag = payload
                    self._append(text, tag)
                elif kind == "status":
                    self._apply_status(payload)
                elif kind == "deps_ready":
                    self.deps_state = "ready"
                    if payload:
                        self._append(payload, "server")
                    self.log("运行环境已就绪，「启动服务」可以点了。", "good")
                    self.busy = False
                    self._refresh_buttons()
                elif kind == "deps_failed":
                    self.deps_state = "failed"
                    self.busy = False
                    if payload:
                        self._append(payload, "error")
                    self.log("运行环境安装失败，请连上网络后关掉窗口重新双击 bat。", "error")
                    self._refresh_buttons()
                elif kind == "done":
                    ok, message = payload
                    self.busy = False
                    self.log(message, "good" if ok else "error")
                    self._refresh_buttons()
        except queue.Empty:
            pass
        if not self.closing:
            self.after(120, self._drain_events)

    # ------------------------------------------------------------ 后台线程
    def _status_worker(self) -> None:
        while not self.closing:
            try:
                ours, others = manage.service_pids()
                payload = {
                    "running": bool(ours),
                    "pids": sorted(ours),
                    "others": sorted(others),
                    "responds": manage.port_responds() if ours else False,
                }
            except Exception as error:  # noqa: BLE001 - 状态探测失败不该让窗口崩掉
                payload = {"running": False, "pids": [], "others": [], "responds": False, "error": str(error)}
            self.events.put(("status", payload))
            time.sleep(1.5)

    def _log_worker(self) -> None:
        """把服务自己写进日志文件的输出搬进窗口（用文件而不是管道：没人读的管道会把服务勒死）。"""
        while not self.closing:
            try:
                if LOG_FILE.exists():
                    size = LOG_FILE.stat().st_size
                    if size < self.log_offset:
                        self.log_offset = 0
                    if size > self.log_offset:
                        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as handle:
                            handle.seek(self.log_offset)
                            chunk = handle.read()
                            self.log_offset = handle.tell()
                        for line in chunk.splitlines():
                            if line.strip():
                                self.events.put(("log", (line, "server")))
            except OSError:
                pass
            time.sleep(0.8)

    def _bootstrap(self) -> None:
        if not REQUIREMENTS.exists():
            self.deps_state = "ready"
            self._refresh_buttons()
            return
        wanted = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
        if DEPS_MARKER.exists() and DEPS_MARKER.read_text(encoding="utf-8").strip() == wanted:
            self.deps_state = "ready"
            self.log("运行环境已就绪。")
            self._refresh_buttons()
            return
        self.deps_state = "installing"
        self.busy = True
        self._refresh_buttons()
        self.log("第一次运行，正在安装运行环境（需要联网，大约一两分钟）…")
        threading.Thread(target=self._install_deps, args=(wanted,), daemon=True).start()

    def _install_deps(self, wanted: str) -> None:
        try:
            result = subprocess.run(
                [console_python(), "-m", "pip", "install", "-r", str(REQUIREMENTS), "--disable-pip-version-check"],
                cwd=str(ROOT), stdin=subprocess.DEVNULL, capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output = ((result.stdout or b"") + (result.stderr or b"")).decode("utf-8", errors="replace").strip()
            if result.returncode != 0:
                self.events.put(("deps_failed", output[-1200:] or "安装程序没有输出任何信息"))
                return
            DEPS_MARKER.parent.mkdir(parents=True, exist_ok=True)
            DEPS_MARKER.write_text(wanted, encoding="utf-8")
            self.events.put(("deps_ready", ""))
        except Exception as error:  # noqa: BLE001
            self.events.put(("deps_failed", str(error)))

    # ------------------------------------------------------------ 按钮动作
    def _run(self, intro: str, worker) -> None:
        if self.busy:
            return
        self.busy = True
        self._refresh_buttons()
        self.log(intro)
        threading.Thread(target=self._run_worker, args=(worker,), daemon=True).start()

    def _run_worker(self, worker) -> None:
        try:
            result = worker()
            ok, message = result if isinstance(result, tuple) else (True, str(result))
        except Exception as error:  # noqa: BLE001 - 按钮动作出错要落在窗口里，不能只在后台炸掉
            ok, message = False, f"操作失败：{error}"
        self.events.put(("done", (ok, message)))

    def on_start(self) -> None:
        if manage.service_running():
            self.log("服务已经在运行了，不用重复启动。", "warn")
            return
        self._run(f"正在启动服务（端口 {PORT}）…", self._start_worker)

    def on_stop(self) -> None:
        self._run("正在停止服务…", self._stop_worker)

    def on_restart(self) -> None:
        self._run("正在重启服务…", self._restart_worker)

    def on_backup(self) -> None:
        """备份与恢复：一个窗口里两件事都能做。

        备份和还原各自的破坏性完全不同（一个只读、一个会覆盖数据），所以
        「立即备份」和「还原选中的备份」在窗口里是两个独立按钮，各自有确认框
        —— 不会出现「想备份却点成还原」。
        """
        if self.backup_window is not None and self.backup_window.winfo_exists():
            self.backup_window.lift()
            self.backup_window.focus_force()
            return
        self.backup_window = BackupDialog(self, self._on_backup_done)

    def _on_backup_done(self, ok: bool, message: str) -> None:
        self.log(message, "good" if ok else "error")

    def on_open(self) -> None:
        if not self.status.get("running"):
            self.log("服务还没启动，先点「启动服务」。", "warn")
            return
        webbrowser.open(f"http://127.0.0.1:{PORT}")

    def on_members(self) -> None:
        """成员列表：看全部注册账号 + 选中一个重置他的密码。只动那一行，不用停服务。"""
        if self.member_window is not None and self.member_window.winfo_exists():
            self.member_window.lift()
            self.member_window.focus_force()
            return
        self.member_window = MemberDialog(self, self._on_members_done)

    def _on_members_done(self, ok: bool, message: str) -> None:
        self.log(message, "good" if ok else "error")

    def on_reset(self) -> None:
        detail = (
            "确定要重置吗？\n\n"
            "· 会删除全部注册账号和聊天记录，学生需要重新注册。\n"
            "· 操作前会自动备份到项目外面的文件夹；备份失败就不删任何东西。\n"
            "· 重置后重新创建默认管理员账号：admin / admin123。\n"
            "· 房间和商城物品是固定的，不会消失。\n"
        )
        if manage.service_running():
            detail += "\n注意：服务正在运行，会先自动把它停掉。\n"
        detail += "\n确定要继续吗？"
        if not messagebox.askyesno("重置数据（不可恢复）", detail, icon="warning", default="no", parent=self):
            self.log("已取消重置，数据没有变化。")
            return
        self._run("正在重置数据：先备份，再删除…", self._reset_worker)

    def on_clear(self) -> None:
        detail = (
            "确定要清空聊天记录吗？\n\n"
            "· 只清聊天记录（含私聊、礼品和活动播报），注册账号、金币、等级、道具都保留，学生不用重新注册。\n"
            "· 房间、商城物品、进行中的红包/竞猜、房间活跃榜都不受影响。\n"
            "· 操作前会自动备份到项目外面的文件夹；备份失败就不删任何东西。\n"
        )
        if manage.service_running():
            detail += "\n注意：服务正在运行，清空后学生要按一次 F5 刷新页面才会看不到旧记录。\n"
        detail += "\n确定要继续吗？"
        if not messagebox.askyesno("清空聊天记录（不可恢复）", detail, icon="warning", default="no", parent=self):
            self.log("已取消清空，聊天记录没有变化。")
            return
        self._run("正在清空聊天记录：先备份，再删除…", self._clear_worker)

    def _wait_online(self, process) -> tuple[bool, str]:
        """等服务真的能连上（进程活着不等于服务就绪）。

        进程号按"谁在监听端口"来报：venv 里的 python.exe 是个转发器，
        Popen 拿到的是转发器 PID，跟任务管理器里看到的那个不是同一个。
        """
        deadline = time.time() + 12
        while time.time() < deadline:
            time.sleep(0.4)
            if manage.port_responds():
                ours, _ = manage.service_pids()
                return True, f"（进程号 {'、'.join(str(pid) for pid in sorted(ours))}）" if ours else ""
            if process.poll() is not None:
                return False, f"服务进程已退出（退出码 {process.returncode}），启动没有成功。请看下面的运行记录。"
        return False, "等了 12 秒还是连不上服务，请看下面的运行记录排查。"

    def _start_worker(self) -> tuple[bool, str]:
        self.log_offset = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
        process = manage.start_server(LOG_FILE)
        ok, detail = self._wait_online(process)
        return (ok, f"服务已启动{detail}，学生机现在可以访问了。" if ok else detail)

    def _stop_worker(self) -> tuple[bool, str]:
        stopped = manage.stop_server(quiet=True)
        left, _ = manage.service_pids()
        if left:
            return False, f"没能停干净：还有 {len(left)} 个进程占着 {PORT} 端口，请稍后再点一次。"
        return True, f"服务已停止（关闭了 {stopped} 个进程）。" if stopped else "服务本来就没有在运行。"

    def _restart_worker(self) -> tuple[bool, str]:
        manage.stop_server(quiet=True)
        time.sleep(1.2)
        left, _ = manage.service_pids()
        if left:
            return False, f"重启中止：旧进程没停干净，还有 {len(left)} 个占着 {PORT} 端口。"
        self.log_offset = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
        process = manage.start_server(LOG_FILE)
        ok, detail = self._wait_online(process)
        return (ok, f"服务已重启{detail}。" if ok else detail.replace("启动", "重启"))

    def _reset_worker(self) -> tuple[bool, str]:
        ok, message = manage.perform_reset()
        return ok, ("重置完成。\n" + message) if ok else ("重置没有完成。\n" + message)

    def _clear_worker(self) -> tuple[bool, str]:
        # perform_clear_messages() 返回的文案本身已自包含（含删除条数与备份路径）。
        return manage.perform_clear_messages()

    # ---------------------------------------------------------------- 关闭
    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno(
            "还有操作在进行", "有一个操作还没做完，现在关掉窗口它可能只做了一半。\n\n确定要关闭吗？", parent=self
        ):
            return
        if manage.service_running():
            answer = messagebox.askyesnocancel(
                "关闭控制台",
                "聊天室服务还在运行。\n\n"
                "· 点「是」：连服务一起关掉（学生马上就连不上了）。\n"
                "· 点「否」：只关这个窗口，服务继续跑着，学生照常使用。\n"
                "· 点「取消」：什么都不做。",
                parent=self, default="no",
            )
            if answer is None:
                return
            if answer:
                manage.stop_server(quiet=True)
            else:
                self.log("服务继续在后台运行；下次双击 bat 还能看到它。")
        self.closing = True
        if self.member_window is not None and self.member_window.winfo_exists():
            self.member_window.alive = False
            self.member_window.destroy()
        if self.backup_window is not None and self.backup_window.winfo_exists():
            self.backup_window.destroy()
        self.destroy()


class MemberDialog(tk.Toplevel):
    """成员列表：一眼看全部注册账号 → 选中一个看他的全部信息 → 就地重置他的密码。

    几个刻意的选择：

    · **不 import app.py**（控制台要在第三方依赖装好之前也能开出来），信息全部来自
      manage.list_members() 直读数据库。代价是这边的物品中文名不能复用 app.py，
      只能靠 manage 里的 KNOWN_ITEM_NAMES —— L1 会锁住那份与 app.py STARTER_ITEMS 一致。

    · **「重置密码」不再另开一个窗口**，并进这里。原来它是个独立弹窗，只有
      「昵称（账号）注册于 …」三样东西；现在选中的人、看到的信息、要重置的账号是同一个，
      不会出现「在这个窗口看、回那个窗口点错人」。

    · 读名单、改密码、导出 CSV 全在后台线程里做，结果一律经 queue 回主线程 ——
      tkinter 控件只能在主线程碰。服务正在写库时读库走 WAL，但等锁也不该把窗口冻住。

    · **身份只显示、不在这里改**。role 是全站唯一的字段，改它等于换一个人；
      那不该是「成员列表」顺手能做的事（要改就走重置数据后的默认账号流程）。
    """

    # 列表显示哪些列 / 列宽 / 对齐。key 必须是 list_members() 真的会放进去的键。
    COLUMNS = (
        ("nickname", "昵称", 150, "w"),
        ("username", "账号", 110, "w"),
        ("role_text", "角色", 78, "center"),
        ("level", "等级", 58, "center"),
        ("coins", "金币", 66, "center"),
        ("messages", "发言", 58, "center"),
        ("created_at", "注册时间", 138, "center"),
    )
    ROLE_RANK = {"admin": 0, "user": 1, "guest": 2}
    NUMERIC_KEYS = {"level", "exp", "coins", "charm", "messages", "violations"}

    def __init__(self, master: "Launcher", on_done) -> None:
        super().__init__(master)
        self.on_done = on_done
        self.queue: queue.Queue = queue.Queue()
        self.members: list[dict] = []      # 库里读出来的全部成员
        self.shown: list[dict] = []        # 当前筛选 + 排序后真正显示出来的那些行
        self.replaced: set[str] = set()    # 本次打开期间重置过密码的账号
        self.selected_username = ""        # 跨「刷新 / 筛选」记住选中的是谁
        self.role_filter = "all"
        self.sort_key = "role"
        self.sort_desc = False
        self.busy = False
        self.loading = False
        self.alive = True

        self.title("成员列表")
        self.configure(bg=BG)
        self.geometry("1020x640")
        self.minsize(900, 540)
        self.transient(master)
        self._build()
        self._center_on(master)
        self.protocol("WM_DELETE_WINDOW", self._close)
        # 窗口刚建出来时还没真正显示，这时候 grab_set() 会抛 "window not viewable"。
        # 挪到事件循环里、并且容错 —— 抢不到焦点也不该让弹窗开不出来。
        self.after(50, self._grab)
        self.reload()
        self.after(120, self._poll)

    # ---------------------------------------------------------------- 界面
    def _build(self) -> None:
        outer = tk.Frame(self, bg=BG, padx=14, pady=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        top = tk.Frame(outer, bg=BG)
        top.grid(row=0, column=0, sticky="ew")
        tk.Label(top, text="全部注册账号", bg=BG, fg=TEXT, font=FONT_BOLD).pack(side="left")
        self.stat = tk.Label(top, text="正在读取…", bg=BG, fg=MUTED, font=FONT)
        self.stat.pack(side="left", padx=(10, 0))
        self.btn_export = self._flat_button(top, "导出 CSV", self._export, BLUE)
        self.btn_export.pack(side="right")
        self.btn_refresh = self._flat_button(top, "刷新", self.reload, BLUE)
        self.btn_refresh.pack(side="right", padx=(0, 8))

        filters = tk.Frame(outer, bg=BG)
        filters.grid(row=1, column=0, sticky="ew", pady=(10, 6))
        tk.Label(filters, text="搜索", bg=BG, fg=MUTED, font=FONT).pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_args: self._apply_filter())
        tk.Entry(filters, textvariable=self.search_var, font=FONT, bg=PANEL, fg=TEXT, width=20,
                 relief="flat", highlightbackground=BORDER, highlightthickness=1).pack(
            side="left", padx=(6, 4), ipady=4)
        tk.Button(filters, text="清空", command=lambda: self.search_var.set(""), font=FONT, relief="flat",
                  bg="#e3ebf2", activebackground="#d2dee8", fg=TEXT, cursor="hand2", padx=10,
                  pady=3).pack(side="left")
        tk.Label(filters, text="昵称 / 账号 / 真实姓名 / 班级 / 签名都能搜", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(8, 0))
        # 角色筛选按钮从右往左摆（pack(side="right")），所以这里的顺序要反过来写。
        self.role_buttons: dict[str, tk.Button] = {}
        for key, label in (("all", "全部"), ("admin", "只看管理员"), ("user", "只看普通用户")):
            button = tk.Button(filters, text=label, font=FONT, relief="flat", bd=0, padx=12, pady=3,
                               cursor="hand2", command=lambda k=key: self._set_role_filter(k))
            button.pack(side="right", padx=(6, 0))
            self.role_buttons[key] = button

        body = tk.Frame(outer, bg=BG)
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        table_box = tk.Frame(body, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        table_box.grid(row=0, column=0, sticky="nsew")
        table_box.columnconfigure(0, weight=1)
        table_box.rowconfigure(0, weight=1)

        style = ttk.Style(self)
        try:
            # ★ Windows 默认的 vista 主题会**忽略** Treeview 的背景色设置（隔行底色、选中色
            #   全被系统顶掉），换成 clam 才由我们说了算，才能和其余面板的配色一致。
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Members.Treeview", background="#fbfdff", fieldbackground="#fbfdff",
                        foreground=TEXT, font=FONT, rowheight=24)
        style.configure("Members.Treeview.Heading", font=FONT_BOLD, background="#e3ebf2", foreground=TEXT)
        style.map("Members.Treeview", background=[("selected", "#cfe3f6")],
                  foreground=[("selected", TEXT)])

        self.tree = ttk.Treeview(table_box, columns=[key for key, _header, _width, _anchor in self.COLUMNS],
                                 style="Members.Treeview", show="headings", selectmode="browse")
        for key, header, width, anchor in self.COLUMNS:
            self.tree.heading(key, text=header, command=lambda k=key: self._sort_by(k))
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "nickname"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = tk.Scrollbar(table_box, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._on_select())
        self.tree.bind("<Double-Button-1>", lambda _event: self._confirm_reset())

        detail_box = tk.Frame(body, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        detail_box.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        detail_box.columnconfigure(0, weight=1)
        detail_box.rowconfigure(1, weight=1)
        tk.Label(detail_box, text="成员详情", bg=PANEL, fg=TEXT, font=FONT_BOLD).grid(
            row=0, column=0, sticky="w", padx=10, pady=(8, 2))
        self.detail = tk.Text(detail_box, font=FONT, bg="#fbfdff", fg=TEXT, relief="flat", wrap="word",
                              state="disabled", bd=0, padx=8, pady=4)
        self.detail.grid(row=1, column=0, sticky="nsew", padx=(10, 0), pady=(0, 8))
        detail_scroll = tk.Scrollbar(detail_box, orient="vertical", command=self.detail.yview)
        detail_scroll.grid(row=1, column=1, sticky="ns", padx=(0, 6), pady=(0, 8))
        self.detail.configure(yscrollcommand=detail_scroll.set)
        self.detail.tag_configure("field", foreground="#4a6b85", font=FONT_BOLD)

        self.hint = tk.Label(outer, text="正在读取成员名单…", bg=BG, fg=MUTED, font=FONT, anchor="w",
                             justify="left", wraplength=960)
        self.hint.grid(row=3, column=0, sticky="ew", pady=(10, 0))

        actions = tk.Frame(outer, bg=BG)
        actions.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.btn_reset = tk.Button(actions, text=f"重置选中成员的密码（{manage.RESET_PASSWORD}）",
                                   command=self._confirm_reset, font=FONT_BOLD, fg="#ffffff", bg=TEAL,
                                   activeforeground="#ffffff", activebackground=TEAL, relief="flat", bd=0,
                                   padx=16, pady=8, cursor="hand2")
        self.btn_reset.pack(side="left")
        tk.Button(actions, text="关闭", command=self._close, font=FONT, relief="flat", bg="#e3ebf2",
                  activebackground="#d2dee8", fg=TEXT, cursor="hand2", padx=16, pady=8).pack(side="right")

        self._refresh_role_buttons()
        self._refresh_action()
        self.detail.configure(state="normal")
        self.detail.insert("end", "左边点一个人，这里显示他的全部信息。\n\n"
                                  "· 双击一行 = 直接重置他的密码\n"
                                  "· 「导出 CSV」导出的是当前筛选之后看到的结果\n"
                                  "· 游客没有账号记录，不会出现在这里\n")
        self.detail.configure(state="disabled")

    def _flat_button(self, parent: tk.Frame, text: str, command, color: str) -> tk.Button:
        return tk.Button(parent, text=text, command=command, font=FONT_BOLD, fg="#ffffff", bg=color,
                         activeforeground="#ffffff", activebackground=color, relief="flat", bd=0,
                         padx=14, pady=6, cursor="hand2")

    def _grab(self) -> None:
        if not self.alive:
            return
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _center_on(self, master: tk.Misc) -> None:
        self.update_idletasks()
        x = master.winfo_rootx() + max(0, (master.winfo_width() - self.winfo_width()) // 2)
        y = master.winfo_rooty() + max(0, (master.winfo_height() - self.winfo_height()) // 3)
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _close(self) -> None:
        if self.busy and not messagebox.askyesno(
            "还有操作在进行",
            "正在重置密码或导出，现在关掉窗口它可能只做了一半。\n\n确定要关闭吗？",
            parent=self,
        ):
            return
        self.alive = False
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    # ---------------------------------------------------------------- 读名单
    def reload(self) -> None:
        """重新读一次数据库。学生新注册之后点这个。"""
        if self.busy or self.loading:
            return
        self.loading = True
        self.hint.configure(text="正在读取成员名单…", fg=MUTED)
        self._refresh_action()
        threading.Thread(target=self._load_worker, daemon=True).start()

    def _load_worker(self) -> None:
        try:
            self.queue.put(("list", (True, manage.list_members())))
        except Exception as error:  # noqa: BLE001 - 读库失败要把原因显示在窗口里，不能只在后台炸掉
            self.queue.put(("list", (False, str(error))))

    # ---------------------------------------------------------------- 筛选与排序
    def _set_role_filter(self, key: str) -> None:
        self.role_filter = key
        self._refresh_role_buttons()
        self._apply_filter()

    def _refresh_role_buttons(self) -> None:
        for key, button in self.role_buttons.items():
            active = key == self.role_filter
            button.configure(bg=TEAL if active else "#e3ebf2", fg="#ffffff" if active else TEXT,
                             activebackground=TEAL if active else "#d2dee8")

    def _sort_by(self, key: str) -> None:
        """点表头排序。同一个表头再点一次就反过来，换一个表头一律从升序开始。"""
        self.sort_desc = (not self.sort_desc) if key == self.sort_key else False
        self.sort_key = key
        self._apply_filter()

    def _sort_key(self, member: dict):
        """排序键。

        ★ 元组三个位置的类型必须固定（int / float / str）：金币、等级这些是数值，
          按字符串排会出现「100 排在 99 前面」「等级 10 排在 9 前面」。混着 None 也不行。
        """
        key = self.sort_key
        if key == "role":
            # 默认视图：管理员在最前，其余按注册时间从早到晚 —— 老师一眼先看到自己。
            return (self.ROLE_RANK.get(str(member.get("role") or "user"), 3), 0.0,
                    str(member.get("created_at") or ""))
        if key in self.NUMERIC_KEYS:
            try:
                return (0, float(member.get(key) or 0), "")
            except (TypeError, ValueError):
                return (0, -1.0, "")
        return (0, 0.0, str(member.get(key) or ""))

    def _apply_filter(self) -> None:
        keyword = self.search_var.get().strip().lower()
        rows = []
        for member in self.members:
            if self.role_filter != "all" and str(member.get("role") or "user") != self.role_filter:
                continue
            if keyword:
                haystack = " ".join(
                    str(member.get(field) or "")
                    for field in ("nickname", "username", "real_name", "class_name", "bio")
                ).lower()
                if keyword not in haystack:
                    continue
            rows.append(member)
        self.shown = sorted(rows, key=self._sort_key, reverse=self.sort_desc)
        self._render()

    def _render(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for index, member in enumerate(self.shown):
            mark = "✓ " if str(member.get("username") or "") in self.replaced else ""
            self.tree.insert("", "end", iid=str(index), values=(
                mark + str(member.get("nickname") or "—"),
                str(member.get("username") or "—"),
                str(member.get("role_text") or "—"),
                member.get("level") if member.get("level") is not None else "—",
                member.get("coins") if member.get("coins") is not None else "—",
                member.get("messages") if member.get("messages") is not None else "—",
                str(member.get("created_at") or "—"),
            ))
        # 刷新 / 筛选 / 排序之后，尽量把原来选中的那个人重新选上 ——
        # 否则「刚看完一个人，点一下刷新就跳回没人选中」，还得再找一遍。
        if self.selected_username:
            for index, member in enumerate(self.shown):
                if str(member.get("username") or "") == self.selected_username:
                    self.tree.selection_set(str(index))
                    self.tree.see(str(index))
                    break
        self._show_detail()
        self._update_stat()

    def _update_stat(self) -> None:
        total = len(self.members)
        if not total:
            self.stat.configure(text="还没有注册过账号")
            return
        admins = sum(1 for member in self.members if str(member.get("role")) == "admin")
        text = f"共 {total} 个注册账号（管理员 {admins} 人 · 普通用户 {total - admins} 人）"
        if len(self.shown) != total:
            text += f"　当前显示 {len(self.shown)} 人"
        self.stat.configure(text=text)

    # ---------------------------------------------------------------- 详情
    def _selected(self) -> dict | None:
        selection = self.tree.selection()
        if not selection:
            return None
        try:
            index = int(selection[0])
        except ValueError:
            return None
        return self.shown[index] if 0 <= index < len(self.shown) else None

    def _on_select(self) -> None:
        member = self._selected()
        # ★ 只在「真的选中了一个人」时才更新记忆：_render() 是「先全删、再重插、最后重选」，
        #   删掉旧行的瞬间 Tk 会派发一次空选中的 <<TreeviewSelect>>。要是空选中也照记不误，
        #   记忆就被抹成 ""，紧接着的重选逻辑失效 —— 表现是「点一下刷新 / 搜一下，选中的人就没了」。
        if member is not None:
            self.selected_username = str(member.get("username") or "")
        self._show_detail()
        self._refresh_action()

    def _show_detail(self) -> None:
        member = self._selected()
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if member is None:
            self.detail.insert("end", "左边点一个人，这里显示他的全部信息。\n\n"
                                      "· 双击一行 = 直接重置他的密码\n"
                                      "· 「导出 CSV」导出的是当前筛选之后看到的结果\n"
                                      "· 游客没有账号记录，不会出现在这里\n")
        else:
            for header, key in manage.MEMBER_FIELDS:
                # 全角空格补位，字段名一列对齐（中文按整字算宽，不能用 ljust）。
                pad = "　" * max(0, 7 - len(header))
                self.detail.insert("end", f"{header}{pad}", "field")
                self.detail.insert("end", f"{manage.member_cell(member.get(key))}\n")
        self.detail.configure(state="disabled")

    def _refresh_action(self) -> None:
        member = self._selected()
        busy = self.busy or self.loading
        # 管理员不给重置：后端 manage.reset_student_password() 也会拒，这里先灰掉，
        # 免得点下去才弹一句「这里只重置学生密码」。
        usable = member is not None and str(member.get("role")) != "admin" and not busy
        self.btn_reset.configure(state="normal" if usable else "disabled",
                                 bg=TEAL if usable else DISABLED_BG,
                                 fg="#ffffff" if usable else "#7f8d99")
        self.btn_refresh.configure(state="disabled" if busy else "normal",
                                   bg=BLUE if not busy else DISABLED_BG,
                                   fg="#ffffff" if not busy else "#7f8d99")
        exportable = bool(self.shown) and not busy
        self.btn_export.configure(state="normal" if exportable else "disabled",
                                  bg=BLUE if exportable else DISABLED_BG,
                                  fg="#ffffff" if exportable else "#7f8d99")

    # ---------------------------------------------------------------- 重置密码
    def _confirm_reset(self) -> None:
        if self.busy:
            return
        member = self._selected()
        if member is None:
            return
        if str(member.get("role")) == "admin":
            self.hint.configure(text="管理员账号不能在这里重置密码（这里只重置学生）。", fg=RED)
            return
        username = str(member.get("username") or "")
        nickname = str(member.get("nickname") or username)
        if not messagebox.askyesno(
            "重置密码",
            f"确定把「{nickname}」（{username}）的密码重置为 {manage.RESET_PASSWORD} 吗？\n\n"
            "· 只改这一个账号的登录密码。\n"
            "· 他的等级、经验、金币、道具、聊天记录全部保留。\n"
            "· 其他学生完全不受影响；服务不用重启，他立刻就能用新密码登录。\n",
            icon="warning", default="no", parent=self,
        ):
            self.hint.configure(text=f"已取消，「{nickname}」的密码没有变化。", fg=MUTED)
            return
        self.busy = True
        self._refresh_action()
        self.hint.configure(text=f"正在重置「{nickname}」的密码…", fg=MUTED)
        threading.Thread(target=self._reset_worker, args=(member,), daemon=True).start()

    def _reset_worker(self, member: dict) -> None:
        try:
            ok, message = manage.reset_student_password(str(member.get("username") or ""))
        except Exception as error:  # noqa: BLE001
            ok, message = False, f"重置失败：{error}"
        self.queue.put(("reset", (member, ok, message)))

    # ---------------------------------------------------------------- 导出
    def _export(self) -> None:
        if self.busy or self.loading:
            return
        if not self.shown:
            self.hint.configure(text="当前没有可导出的成员：先把搜索清空，或换一个角色筛选。", fg=MUTED)
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="导出成员列表", defaultextension=".csv",
            initialfile=f"成员列表_{time.strftime('%Y-%m-%d_%H%M')}.csv",
            filetypes=[("CSV 表格", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            self.hint.configure(text="已取消导出，没有写出任何文件。", fg=MUTED)
            return
        self.busy = True
        self._refresh_action()
        self.hint.configure(text=f"正在导出 {len(self.shown)} 人的信息…", fg=MUTED)
        threading.Thread(target=self._export_worker, args=(path, list(self.shown)), daemon=True).start()

    def _export_worker(self, path: str, rows: list[dict]) -> None:
        try:
            # ★ 用 write_bytes + utf-8-sig（带 BOM），Excel 双击打开才是中文而不是乱码。
            #   不能用 write_text：文本模式会把 csv 写出的 \r\n 再翻译一遍（Windows 上是
            #   \r\r\n），Excel 看到的就是一片空行。
            Path(path).write_bytes(manage.members_csv(rows).encode("utf-8-sig"))
        except OSError as error:
            self.queue.put(("export", (0, f"导出失败：{error}")))
            return
        self.queue.put(("export", (len(rows), path)))

    # ---------------------------------------------------------------- 收结果
    def _poll(self) -> None:
        """所有跨线程的结果都从这个队列里取，tkinter 只在主线程碰控件。"""
        if not self.alive:
            return
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "list":
                    ok, data = payload
                    self.loading = False
                    if ok:
                        self.members = data
                        self._apply_filter()
                        if data:
                            self.hint.configure(
                                text=f"读取到 {len(data)} 个账号。名单只在打开 / 刷新时读一次；"
                                     f"选中一个人看他的全部信息，需要时直接在这里重置他的密码。", fg=MUTED)
                        else:
                            self.hint.configure(
                                text="还没有注册过账号。学生在网页上注册之后，点「刷新」就能看到。", fg=MUTED)
                    else:
                        self.hint.configure(text=f"读取成员名单失败：{data}", fg=RED)
                    self._refresh_action()
                elif kind == "reset":
                    member, ok, message = payload
                    self.busy = False
                    if ok:
                        username = str(member.get("username") or "")
                        self.replaced.add(username)
                        self.selected_username = username
                        self._render()
                    self.hint.configure(text=message, fg=GREEN if ok else RED)
                    self.on_done(ok, message)
                    self._refresh_action()
                elif kind == "export":
                    count, detail = payload
                    self.busy = False
                    if count:
                        self.hint.configure(
                            text=f"已导出 {count} 人的信息到 {detail}"
                                 f"　（文件里有真实姓名和班级，注意存放位置）", fg=GREEN)
                    else:
                        self.hint.configure(text=str(detail), fg=RED)
                    self._refresh_action()
        except queue.Empty:
            pass
        self.after(120, self._poll)


class BackupDialog(tk.Toplevel):
    """备份与恢复：上半「立即备份」，下半列出所有能还原的备份。

    几个刻意的选择：
    · 不 import app.py（控制台要在第三方依赖装好之前也能开出来）；
    · 扫列表、备份、还原全在后台线程里做 —— 项目外面可能堆着几十份备份，
      逐份 stat 会把窗口卡住；
    · 「立即备份」和「还原」做成两个独立按钮：前者只读，后者会覆盖数据。
      混成一个按钮，用户点「备份」时就会担心误触还原。
    """

    def __init__(self, master: "Launcher", on_done) -> None:
        super().__init__(master)
        self.on_done = on_done
        self.queue: queue.Queue = queue.Queue()
        self.items: list[dict] = []
        self.busy = False
        self.alive = True
        self.master_busy_set = False

        self.title("备份与恢复")
        self.configure(bg=BG)
        self.geometry("660x500")
        self.minsize(560, 420)
        self.transient(master)
        self._build()
        self._center_on(master)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(50, self._grab)
        threading.Thread(target=self._load_worker, daemon=True).start()
        self.after(120, self._poll)

    def _grab(self) -> None:
        if not self.alive:
            return
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _center_on(self, master: tk.Misc) -> None:
        self.update_idletasks()
        x = master.winfo_rootx() + max(0, (master.winfo_width() - self.winfo_width()) // 2)
        y = master.winfo_rooty() + max(0, (master.winfo_height() - self.winfo_height()) // 3)
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _close(self) -> None:
        if self.busy and not messagebox.askyesno(
            "还有操作在进行",
            "备份 / 还原还没做完，现在关掉窗口它可能只做了一半。\n\n确定要关闭吗？",
            parent=self,
        ):
            return
        self.alive = False
        self._unlock_master()
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    def _lock_master(self) -> None:
        """还原会停服务、覆盖文件 —— 期间把主窗口的按钮一起锁上。

        否则边上有人点「启动服务」，服务把 data\\chatroom.db 打开着，
        紧接着的覆盖就会因文件被占用失败，用户看到的是「请先停止服务」的怪提示
        （明明刚停过）。用一个标记记住「锁是我加的」，解锁时不会误清别人的忙状态。
        """
        if self.master_busy_set:
            return
        self.master.busy = True
        self.master_busy_set = True
        self.master._refresh_buttons()  # noqa: SLF001 - 父子窗口之间的约定

    def _unlock_master(self) -> None:
        if not self.master_busy_set:
            return
        self.master.busy = False
        self.master_busy_set = False
        self.master._refresh_buttons()  # noqa: SLF001

    # ---------------------------------------------------------------- 界面
    def _build(self) -> None:
        outer = tk.Frame(self, bg=BG, padx=14, pady=12)
        outer.pack(fill="both", expand=True)

        top = tk.Frame(outer, bg=BG)
        top.pack(fill="x")
        tk.Label(top, text=f"备份存放在：{manage.BACKUPS}", bg=BG, fg=TEXT, font=FONT_BOLD,
                 anchor="w").pack(side="left")
        self.btn_new = tk.Button(top, text="立即备份", command=self._do_backup, font=FONT_BOLD,
                                 fg="#ffffff", bg=BLUE, activeforeground="#ffffff", activebackground=BLUE,
                                 relief="flat", bd=0, padx=16, pady=6, cursor="hand2")
        self.btn_new.pack(side="right")
        tk.Label(outer,
                 text="备份内容：聊天数据库（账号、金币、等级、道具、聊天记录）+ 头像图片。"
                      "服务运行中也能备，不用先停服务。",
                 bg=BG, fg=MUTED, font=FONT, anchor="w", justify="left",
                 wraplength=610).pack(fill="x", pady=(4, 12))

        tk.Label(outer, text="可以还原的备份（最新的排最上面）", bg=BG, fg=TEXT,
                 font=FONT_BOLD, anchor="w").pack(fill="x")

        box = tk.Frame(outer, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        box.pack(fill="both", expand=True, pady=(4, 0))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(box, font=FONT_MONO, bg="#fbfdff", fg=TEXT, relief="flat", bd=0,
                                  height=9, selectmode="browse", activestyle="none", highlightthickness=0,
                                  selectbackground="#cfe3f6", selectforeground=TEXT)
        self.listbox.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        scroll = tk.Scrollbar(box, orient="vertical", command=self.listbox.yview)
        scroll.grid(row=0, column=1, sticky="ns", padx=(0, 6), pady=8)
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.bind("<<ListboxSelect>>", lambda _event: self._refresh_actions())
        self.listbox.bind("<Double-Button-1>", lambda _event: self._confirm_restore())

        self.hint = tk.Label(outer, text="正在读取备份列表…", bg=BG, fg=MUTED, font=FONT,
                             anchor="w", justify="left", wraplength=610)
        self.hint.pack(fill="x", pady=(10, 0))

        actions = tk.Frame(outer, bg=BG)
        actions.pack(fill="x", pady=(8, 0))
        self.btn_restore = tk.Button(actions, text="还原选中的备份", command=self._confirm_restore,
                                     font=FONT_BOLD, fg="#ffffff", bg=ORANGE, activeforeground="#ffffff",
                                     activebackground=ORANGE, relief="flat", bd=0, padx=16, pady=8,
                                     cursor="hand2")
        self.btn_restore.pack(side="left")
        tk.Button(actions, text="关闭", command=self._close, font=FONT, relief="flat", bg="#e3ebf2",
                  activebackground="#d2dee8", fg=TEXT, cursor="hand2", padx=16, pady=8).pack(side="right")
        self._refresh_actions()

    # ---------------------------------------------------------------- 列表
    def _row_text(self, item: dict) -> str:
        tail = "含头像" if item["avatars"] else "无头像"
        return f"{item['when']}　{item['label']}　{max(1, item['size'] // 1024)} KB　[{tail}]"

    def _fill(self) -> None:
        self.listbox.delete(0, "end")
        for item in self.items:
            self.listbox.insert("end", self._row_text(item))
        if not self.items:
            self.hint.configure(text="还没有任何备份。点右上角的「立即备份」存一份。", fg=MUTED)
        else:
            self.hint.configure(text=f"共 {len(self.items)} 份，点一下选中；双击可以直接还原。", fg=MUTED)
        self._refresh_actions()

    def _selected(self) -> dict | None:
        if self.busy:
            return None
        selection = self.listbox.curselection()
        if not selection:
            return None
        index = selection[0]
        return self.items[index] if 0 <= index < len(self.items) else None

    def _refresh_actions(self) -> None:
        fresh = not self.busy
        self.btn_new.configure(state="normal" if fresh else "disabled",
                               bg=BLUE if fresh else DISABLED_BG,
                               fg="#ffffff" if fresh else "#7f8d99")
        usable = fresh and self._selected() is not None
        self.btn_restore.configure(state="normal" if usable else "disabled",
                                   bg=ORANGE if usable else DISABLED_BG,
                                   fg="#ffffff" if usable else "#7f8d99")

    # ---------------------------------------------------------------- 动作
    def _do_backup(self) -> None:
        if self.busy:
            return
        self.busy = True
        self._refresh_actions()
        self.hint.configure(text="正在备份…", fg=MUTED)
        threading.Thread(target=self._backup_worker, daemon=True).start()

    def _confirm_restore(self) -> None:
        item = self._selected()
        if item is None:
            return
        avatar_line = ("· 这份备份里有头像图片，会一起还原。\n" if item["avatars"]
                       else "· 这份备份里没有头像图片，项目里的头像保持原样。\n")
        detail = (
            "确定要还原这一份备份吗？\n\n"
            f"· 备份时间：{item['when']}\n"
            f"· 来源：{item['label']}\n"
            f"· 位置：{item['group']}\n\n"
            "还原会依次做这些事：\n"
            "· 先自动停掉聊天室服务。\n"
            "· 把你现在的数据另存一份到项目外面（万一选错了还能退回来）。\n"
            "· 用这份备份覆盖现在的数据 —— 备份之后新产生的聊天记录和账号都会消失。\n"
            + avatar_line
            + "\n还原完成后，回到主窗口点「启动服务」就能用。\n\n确定要继续吗？"
        )
        if not messagebox.askyesno("还原备份（不可恢复）", detail, icon="warning", default="no", parent=self):
            self.hint.configure(text="已取消还原，数据没有变化。", fg=MUTED)
            return
        self.busy = True
        self._lock_master()
        self._refresh_actions()
        self.hint.configure(text="正在还原：先把当前数据另存一份，再覆盖…", fg=MUTED)
        threading.Thread(target=self._restore_worker, args=(item["group"],), daemon=True).start()

    def _load_worker(self) -> None:
        try:
            self.queue.put(("list", (True, manage.list_backups())))
        except Exception as error:  # noqa: BLE001 - 读不出来也要显示原因，不能静默
            self.queue.put(("list", (False, str(error))))

    def _backup_worker(self) -> None:
        try:
            ok, message = manage.create_backup(manage.BACKUPS)
        except Exception as error:  # noqa: BLE001
            ok, message = False, f"备份失败：{error}"
        self.queue.put(("done", (ok, message)))

    def _restore_worker(self, group: Path) -> None:
        try:
            ok, message = manage.restore_backup(group)
        except Exception as error:  # noqa: BLE001
            ok, message = False, f"还原失败：{error}"
        self.queue.put(("done", (ok, message)))

    def _reload(self) -> None:
        self.hint.configure(text="正在刷新备份列表…", fg=MUTED)
        threading.Thread(target=self._load_worker, daemon=True).start()

    def _poll(self) -> None:
        """跨线程的结果统一从这里取，tkinter 只在主线程碰控件。"""
        if not self.alive:
            return
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "list":
                    ok, data = payload
                    if ok:
                        self.items = data
                        self._fill()
                    else:
                        self.hint.configure(text=f"读取备份列表失败：{data}", fg=RED)
                elif kind == "done":
                    ok, message = payload
                    self.busy = False
                    self._unlock_master()
                    first = message.splitlines()[0] if message else ""
                    self.hint.configure(text=first, fg=GREEN if ok else RED)
                    self.on_done(ok, message)
                    self._reload()
        except queue.Empty:
            pass
        self.after(120, self._poll)


def _fatal(message: str) -> None:
    """窗口都开不出来时也要让用户看到原因（pythonw 下没有控制台可看）。"""
    try:
        ctypes.windll.user32.MessageBoxW(None, message, "局域网互动聊天室 · 控制台", 0x10)
    except Exception:  # noqa: BLE001
        print(message, file=sys.stderr)


def main() -> int:
    try:
        app = Launcher()
    except Exception as error:  # noqa: BLE001 - 启动失败必须看得见
        _fatal(f"控制台窗口打开失败：\n{error}\n\n请确认 Python 安装时勾选了 tcl/tk 组件。")
        return 1
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
