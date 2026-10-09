"""「管理员还在用出厂默认密码」启动告警的测试。

要守住的四条契约：
  ① 默认密码时：返回 True 且**真的打印**了告警 —— 没打印等于老师看不到，这个功能就是白做的；
  ② 密码改过之后：一声不吭。每次都喊「狼来了」，喊几回就没人看了；
  ③ 库里没有管理员账号时：既不崩、也不告警（比如账号被改名/删掉的情况）；
  ④ init_db() **真的接上了**这道体检。只测函数不测接线的话，
     「函数写得对、但忘了调用」会静默漏过去 —— 这正是这类功能最常见的坏法。

安全：全程指向临时数据目录 + 临时公告目录。init_db() 结尾有「搬老公告」的迁移
（把根目录 公告.txt 复制成 公告/lobby.txt），不隔离就会写进真实项目目录——踩过的坑。
"""
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 这三个环境变量必须都在 import app 之前设好：app 在导入期就会创建目录、读词库，
# 而 CHAT_DATA_DIR / CHAT_ANNOUNCE_DIR 决定它们落在哪里。
_import_data = tempfile.TemporaryDirectory(prefix="lan-chat-default-pw-import-")
os.environ["CHAT_DATA_DIR"] = _import_data.name
os.environ["CHAT_ANNOUNCE_DIR"] = os.path.join(_import_data.name, "公告")
import app as chat  # noqa: E402

WARNING_PREFIX = "[安全提醒]"

SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    nickname TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL
);
"""


class DefaultPasswordWarningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lan-chat-default-pw-")
        self.root = Path(self.temp.name)
        self.db_path = self.root / "chatroom.db"

        self.original_db_path = chat.DB_PATH
        self.original_announce_dir = chat.ANNOUNCE_DIR
        self.original_legacy = chat.LEGACY_ANNOUNCE_FILE
        chat.DB_PATH = self.db_path
        chat.ANNOUNCE_DIR = self.root / "公告"
        # 指向一个不存在的旧公告，免得上游迁移从真实项目目录里搬东西过来。
        chat.LEGACY_ANNOUNCE_FILE = self.root / "没有这个旧公告.txt"

        with closing(sqlite3.connect(self.db_path)) as db:
            db.executescript(SCHEMA)
            db.commit()

    def tearDown(self):
        # get_db() 按线程缓存连接，不先关掉，Windows 上临时目录会因「文件被占用」删不掉。
        chat.close_db()
        chat.DB_PATH = self.original_db_path
        chat.ANNOUNCE_DIR = self.original_announce_dir
        chat.LEGACY_ANNOUNCE_FILE = self.original_legacy
        self.temp.cleanup()

    # ------------------------------------------------------------ 小工具
    def add_admin(self, password: str) -> None:
        """按 app.py 自己的算法造密码哈希，别手写 —— 算法改了这里要跟着认出来。"""
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "INSERT INTO users (username, password_hash, nickname, role, created_at)"
                " VALUES (?, ?, ?, 'admin', ?)",
                (chat.ADMIN_USERNAME, chat.hash_password(password), "管理员", "2026-10-01 20:00:00"),
            )
            db.commit()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def run_warning(self) -> tuple[bool, str]:
        with closing(self.connect()) as db:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                warned = chat.warn_default_admin_password(db)
        return warned, buffer.getvalue()

    # ------------------------------------------------------------ ①②③
    def test_01_warns_when_password_is_still_the_default(self):
        self.add_admin(chat.DEFAULT_ADMIN_PASSWORD)
        warned, output = self.run_warning()
        self.assertTrue(warned, "仍在用默认密码却没有告警")
        self.assertIn(WARNING_PREFIX, output)
        self.assertIn(chat.ADMIN_USERNAME, output)

    def test_02_silent_after_password_changed(self):
        """改过密码就闭嘴 —— 否则每次启动都报，很快就会变成没人看的背景噪音。"""
        self.add_admin("定了一个新密码-9f3k")
        warned, output = self.run_warning()
        self.assertFalse(warned, "密码已经改过了，不该再告警")
        self.assertEqual(output.strip(), "")

    def test_03_silent_when_no_admin_account(self):
        warned, output = self.run_warning()
        self.assertFalse(warned)
        self.assertEqual(output.strip(), "")

    def test_04_silent_when_there_is_a_different_admin_username(self):
        """账号被改名的情况：查不到 ADMIN_USERNAME 就该安静，别误报。"""
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "INSERT INTO users (username, password_hash, nickname, role, created_at)"
                " VALUES (?, ?, ?, 'admin', ?)",
                ("teacher", chat.hash_password(chat.DEFAULT_ADMIN_PASSWORD), "管理员", "2026-10-01 20:00:00"),
            )
            db.commit()
        warned, _ = self.run_warning()
        self.assertFalse(warned)

    # ------------------------------------------------------------ ④ 接线
    def test_05_init_db_actually_calls_the_check(self):
        """光有函数不算数：init_db() 必须真的调它。把库删掉让它全新建库。"""
        self.db_path.unlink()
        original_password = chat.ADMIN_PASSWORD
        chat.ADMIN_PASSWORD = chat.DEFAULT_ADMIN_PASSWORD  # 与部署环境的变量无关，让断言确定
        try:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                chat.init_db()
        finally:
            chat.ADMIN_PASSWORD = original_password

        output = buffer.getvalue()
        self.assertIn(WARNING_PREFIX, output, f"init_db() 没有调用体检。实际输出：{output!r}")

        # 新库真的建出了管理员，且密码确实是默认那个（否则上面的告警是碰巧）。
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT password_hash FROM users WHERE username = ?", (chat.ADMIN_USERNAME,)
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(chat.verify_password(chat.DEFAULT_ADMIN_PASSWORD, row["password_hash"]))

    def test_06_init_db_is_silent_once_password_changed(self):
        """接线对了，还得确认「不该响的时候真不响」——只有该响才响才叫告警。"""
        self.db_path.unlink()
        with redirect_stdout(io.StringIO()):  # 这一次的告警是预期的，别让它漏到测试输出里
            chat.init_db()  # 全新建库：此刻管理员用的就是默认密码
        with closing(self.connect()) as db:
            db.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (chat.hash_password("换成新密码-7q"), chat.ADMIN_USERNAME),
            )
            db.commit()

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            chat.init_db()  # 再启动一次
        self.assertNotIn(WARNING_PREFIX, buffer.getvalue())

    # ------------------------------------------------------------ 跨文件握手
    def test_07_warning_prefix_matches_the_console_highlighter(self):
        """app 负责打印、launcher 负责标橙色，两边靠这个前缀握手。
        改了 app 的措辞就会让控制台的橙色规则悄悄失效（退回灰色、淹没在输出里），
        这里把它焊住。判据前先剥掉 `#` 注释，免得被注释里的字样骗过。"""
        self.add_admin(chat.DEFAULT_ADMIN_PASSWORD)
        _, output = self.run_warning()
        first_line = output.strip().splitlines()[0]
        self.assertTrue(first_line.startswith(WARNING_PREFIX), first_line)

        source = (ROOT / "launcher.py").read_text(encoding="utf-8")
        code_only = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
        self.assertIn(f'"{WARNING_PREFIX}"', code_only, "launcher.py 里找不到这个前缀，控制台不会标橙色")


if __name__ == "__main__":
    unittest.main(verbosity=2)
