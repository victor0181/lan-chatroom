"""控制台「重置密码」的测试。

要证明的三件事：
1. 名单里只有学生，管理员不出现在可重置的列表里；
2. 重置只动选中的那一行，别的账号一个字节都不变；
3. 重置出来的哈希，用 app.py 的同一套算法认得出新密码、认不出旧密码。

所有用例都指向临时库，绝不碰 data/chatroom.db。
"""
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manage  # noqa: E402

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


class PasswordResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lan-chat-passwd-")
        self.root = Path(self.temp.name)
        self.db_path = self.root / "chatroom.db"
        self.original_db = manage.DB
        manage.DB = self.db_path

        with closing(sqlite3.connect(self.db_path)) as db:
            db.executescript(SCHEMA)
            for username, nickname, role, password in (
                ("admin", "管理员", "admin", "admin123"),
                ("zhangsan", "张三", "user", "zs-old-pass"),
                ("lisi", "李四", "user", "ls-old-pass"),
                ("wangwu", "王五", "user", "ww-old-pass"),
            ):
                db.execute(
                    "INSERT INTO users (username, password_hash, nickname, role, created_at) VALUES (?, ?, ?, ?, ?)",
                    (username, manage.hash_password(password), nickname, role, "2026-10-01 20:00:00"),
                )
            db.commit()

    def tearDown(self):
        manage.DB = self.original_db
        self.temp.cleanup()

    def hashes(self) -> dict:
        with closing(sqlite3.connect(self.db_path)) as db:
            return {row[0]: row[1] for row in db.execute("SELECT username, password_hash FROM users")}

    # ------------------------------------------------------------------ 名单
    def test_lists_only_students(self):
        students = manage.list_students()
        names = sorted(item["username"] for item in students)
        self.assertEqual(names, ["lisi", "wangwu", "zhangsan"])
        self.assertNotIn("admin", names)

    # ------------------------------------------------------------------ 重置
    def test_reset_touches_only_the_selected_student(self):
        before = self.hashes()
        ok, message = manage.reset_student_password("lisi")
        self.assertTrue(ok, message)
        after = self.hashes()

        # ★ 本次改动的关键断言：新密码能通过校验，旧密码作废。
        self.assertTrue(manage.verify_password(manage.RESET_PASSWORD, after["lisi"]))
        self.assertFalse(manage.verify_password("ls-old-pass", after["lisi"]))
        # 其他账号（含管理员）的哈希一个字节都没变。
        self.assertEqual(before["zhangsan"], after["zhangsan"])
        self.assertEqual(before["wangwu"], after["wangwu"])
        self.assertEqual(before["admin"], after["admin"])

    def test_reset_twice_keeps_working(self):
        self.assertTrue(manage.reset_student_password("lisi")[0])
        ok, message = manage.reset_student_password("lisi")
        self.assertTrue(ok, message)
        self.assertTrue(manage.verify_password(manage.RESET_PASSWORD, self.hashes()["lisi"]))

    def test_custom_password_is_honoured(self):
        ok, message = manage.reset_student_password("wangwu", "my-new-pass")
        self.assertTrue(ok, message)
        self.assertTrue(manage.verify_password("my-new-pass", self.hashes()["wangwu"]))
        self.assertFalse(manage.verify_password(manage.RESET_PASSWORD, self.hashes()["wangwu"]))

    # ------------------------------------------------------------------ 拦阻
    def test_rejects_admin_account(self):
        before = self.hashes()
        ok, message = manage.reset_student_password("admin")
        self.assertFalse(ok)
        self.assertIn("管理员", message)
        self.assertEqual(before["admin"], self.hashes()["admin"])

    def test_rejects_unknown_username(self):
        ok, message = manage.reset_student_password("nobody")
        self.assertFalse(ok)
        self.assertIn("没有找到", message)

    def test_rejects_empty_selection(self):
        ok, _ = manage.reset_student_password("")
        self.assertFalse(ok)

    def test_missing_database_is_reported(self):
        manage.DB = self.root / "not-here.db"
        self.assertEqual(manage.list_students(), [])
        ok, message = manage.reset_student_password("lisi")
        self.assertFalse(ok)
        self.assertIn("数据库", message)


if __name__ == "__main__":
    unittest.main()
