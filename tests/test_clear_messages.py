"""清空聊天记录（控制台「清空聊天记录」按钮 / manage.py clear-messages）的回归测试。

它和「重置数据」最容易搞混，所以这里最要紧的断言是：
**只动 messages 表，注册账号与金币必须逐字原样保留。**

所有用例都在临时假项目里跑，绝不碰项目里真实的 data/chatroom.db。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import manage  # noqa: E402

PROJECT_NAME = "局域网聊天室"
CLEAR_PREFIX = f"{PROJECT_NAME}_清空记录备份_"


def make_db(path: Path, messages: int = 7) -> None:
    """造一个和真库同构的最小库：该清的表 + 不该动的表都摆上。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    # ★ 必须和真库一样是 WAL：journal 模式下 BEGIN EXCLUSIVE 连读都会锁住，
    #   失败会提前发生在"读条数"那一步，测不到"删除失败"这条路径。
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(
        """
        CREATE TABLE users (id TEXT PRIMARY KEY, nickname TEXT, coins INTEGER, level INTEGER, inventory TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id TEXT, content TEXT, kind TEXT, room_id TEXT);
        CREATE TABLE rooms (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE room_activities (id TEXT PRIMARY KEY, room_id TEXT, kind TEXT, status TEXT);
        CREATE TABLE room_activity_scores (room_id TEXT, user_id TEXT, score INTEGER);
        """
    )
    db.execute("INSERT INTO users VALUES ('u1','小明',120,4,'{}')")
    db.execute("INSERT INTO users VALUES ('u2','小红',30,2,'{}')")
    db.execute("INSERT INTO rooms VALUES ('lobby','大厅')")
    db.execute("INSERT INTO room_activities VALUES ('a1','lobby','redpack','open')")
    db.execute("INSERT INTO room_activity_scores VALUES ('lobby','u1',50)")
    for index in range(messages):
        db.execute(
            "INSERT INTO messages (sender_id, content, kind, room_id) VALUES (?,?,?,?)",
            ("u1", f"第 {index + 1} 条", "text", "lobby"),
        )
    db.commit()
    db.close()


class ClearMessagesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="clear-msgs-"))
        self.project = self.workspace / PROJECT_NAME
        self.db = self.project / "data" / "chatroom.db"
        make_db(self.db)
        self._patches = [
            mock.patch.object(manage, "ROOT", self.project),
            mock.patch.object(manage, "DB", self.db),
            mock.patch.object(manage, "BACKUPS", self.project / "backups"),
            mock.patch.object(manage, "LOGS", self.project / "logs"),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self._patches):
            patch.stop()
        shutil.rmtree(self.workspace, ignore_errors=True)

    # ------------------------------------------------------------------ 辅助
    def count(self, table: str) -> int:
        db = sqlite3.connect(self.db)
        try:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            db.close()

    def rows(self, table: str) -> list[tuple]:
        db = sqlite3.connect(self.db)
        try:
            return db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        finally:
            db.close()

    def autosaves(self, prefix: str = CLEAR_PREFIX) -> list[Path]:
        """项目外的自动备份目录，按名字排序（= 时间顺序）。"""
        return sorted(path for path in self.workspace.iterdir() if path.is_dir() and path.name.startswith(prefix))

    def messages_in(self, backup: Path) -> int:
        db = sqlite3.connect(backup / "data" / "chatroom.db")
        try:
            return db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            db.close()

    # ------------------------------------------------------------ 核心语义
    def test_clears_messages_and_keeps_everything_else(self) -> None:
        users_before = self.rows("users")
        self.assertEqual(self.count("messages"), 7)
        ok, message = manage.perform_clear_messages()
        self.assertTrue(ok, message)
        self.assertIn("删除 7 条", message)
        self.assertEqual(self.count("messages"), 0)
        # ★ 账号与金币一格没动 —— 这是它跟「重置数据」的根本区别
        self.assertEqual(self.rows("users"), users_before)
        self.assertEqual(self.count("rooms"), 1)
        self.assertEqual(self.count("room_activities"), 1)  # 进行中的红包不动
        self.assertEqual(self.count("room_activity_scores"), 1)

    def test_backup_lands_outside_project_and_is_readable(self) -> None:
        ok, message = manage.perform_clear_messages()
        self.assertTrue(ok, message)
        backups = self.autosaves()
        self.assertEqual(len(backups), 1)
        backup = backups[0]
        suffix = backup.name[len(CLEAR_PREFIX):]
        self.assertTrue(suffix[:8].isdigit(), f"目录名缺 8 位日期戳：{backup.name}")
        # ★ 必须在项目【外面】—— 放项目里，整目录覆盖升级时会连备份一起丢
        self.assertEqual(backup.parent, self.workspace)
        self.assertNotIn(self.project, backup.parents)
        # 备份要真的能回退：里面还查得到清空前的 7 条（只拷主库会拷到空库）
        self.assertEqual(self.messages_in(backup), 7)
        self.assertTrue((backup / "还原说明.txt").is_file())
        self.assertIn(str(backup), message)  # 提示里报了备份路径

    def test_backup_carries_accounts_too(self) -> None:
        manage.perform_clear_messages()
        backup = self.autosaves()[0]
        db = sqlite3.connect(backup / "data" / "chatroom.db")
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT coins FROM users WHERE id='u1'").fetchone()[0], 120)
        finally:
            db.close()

    # ------------------------------------------------------------ 边界与幂等
    def test_empty_table_is_a_noop_without_extra_backup(self) -> None:
        manage.perform_clear_messages()
        first = self.autosaves()
        self.assertEqual(len(first), 1)
        ok, message = manage.perform_clear_messages()
        self.assertTrue(ok, message)
        self.assertIn("本来就是空的", message)
        self.assertEqual(self.autosaves(), first, "第二轮不该再多出一份备份")

    def test_two_rounds_are_idempotent(self) -> None:
        results = [manage.perform_clear_messages() for _ in range(2)]
        self.assertTrue(all(ok for ok, _ in results), results)
        self.assertEqual(self.count("messages"), 0)
        self.assertEqual(len(self.autosaves()), 1)
        self.assertEqual(len(self.rows("users")), 2)  # 账号两轮都在，没有被顺手清掉

    def test_missing_database_fails_without_backup(self) -> None:
        self.db.unlink()
        ok, message = manage.perform_clear_messages()
        self.assertFalse(ok)
        self.assertIn("没有找到聊天数据库", message)
        self.assertEqual(self.autosaves(), [])

    def test_missing_table_reports_nothing_to_clear(self) -> None:
        db = sqlite3.connect(self.db)
        db.execute("DROP TABLE messages")
        db.commit()
        db.close()
        ok, message = manage.perform_clear_messages()
        self.assertTrue(ok, message)
        self.assertIn("还没有聊天记录表", message)
        self.assertEqual(self.autosaves(), [])

    def test_backup_failure_aborts_before_deleting_anything(self) -> None:
        """备份失败必须中止 —— 否则会出现「没有备份 + 数据已删」的最坏结局。"""
        with mock.patch.object(manage.shutil, "copytree", side_effect=OSError("模拟磁盘写满")):
            ok, message = manage.perform_clear_messages()
        self.assertFalse(ok)
        self.assertIn("自动备份失败，已中止", message)
        self.assertEqual(self.count("messages"), 7)  # ★ 一条都没删
        self.assertEqual(len(self.rows("users")), 2)

    def test_locked_database_reports_failure_and_loses_nothing(self) -> None:
        """服务正在写、库一时被锁住 —— 必须如实报错，而不是假装清空了。"""
        locker = sqlite3.connect(self.db, timeout=0)
        locker.execute("BEGIN EXCLUSIVE")
        try:
            ok, message = manage.perform_clear_messages()
        finally:
            locker.rollback()
            locker.close()
        self.assertFalse(ok)
        self.assertIn("清空没能完成", message)
        self.assertIn("一条都没少", message)
        self.assertEqual(self.count("messages"), 7)  # ★ 关键：一条都没少
        backups = self.autosaves()
        self.assertEqual(len(backups), 1)  # 备份已生成，且如实出现在提示里
        self.assertIn(str(backups[0]), message)

    # -------------------------------------------------------------- 备份保留
    def test_purge_keeps_only_recent_backups(self) -> None:
        names = [f"{CLEAR_PREFIX}2026100{i}-120000" for i in range(1, 5)]
        for name in names:
            (self.workspace / name).mkdir()
        note = manage.purge_old_clear_backups(keep=2, protect=names[-1])
        # ★ 显式写出期望的目录清单，不用 startswith 反推
        self.assertEqual(sorted(path.name for path in self.autosaves()), [names[2], names[3]])
        self.assertIn(names[0], note)
        self.assertIn(names[1], note)

    def test_purge_protect_beats_recency(self) -> None:
        """protect 指向【最旧】那份时它也得活下来 —— 否则 protect 只是靠排序侥幸生效。"""
        names = [f"{CLEAR_PREFIX}2026100{i}-120000" for i in range(1, 5)]
        for name in names:
            (self.workspace / name).mkdir()
        manage.purge_old_clear_backups(keep=2, protect=names[0])
        survivors = sorted(path.name for path in self.autosaves())
        self.assertEqual(survivors, [names[0], names[3]])  # keep=2 含被 protect 的那一份

    def test_purge_never_touches_neighbours(self) -> None:
        others = [
            f"{PROJECT_NAME}_重置备份_20261001-120000",  # 同前缀、异用途
            f"{CLEAR_PREFIX}old",  # 非 8 位日期戳
            f"别的项目_清空记录备份_20261001-120000",  # 异项目
        ]
        for name in others:
            (self.workspace / name).mkdir()
        for index in range(1, 4):
            (self.workspace / f"{CLEAR_PREFIX}2026100{index}-120000").mkdir()
        manage.purge_old_clear_backups(keep=1, protect=f"{CLEAR_PREFIX}20261003-120000")
        self.assertTrue(self.project.is_dir(), "项目本体不该被删")
        for name in others:
            self.assertTrue((self.workspace / name).is_dir(), f"{name} 不该被删")

    # ------------------------------------------- 这次的重构没改坏「重置备份」链路
    def test_reset_backup_still_uses_its_own_prefix(self) -> None:
        """两个备份函数抽成了公共实现 —— 重置那边的行为必须一字不变。"""
        directory, error, _ = manage.backup_before_reset()
        self.assertEqual(error, "")
        backup = Path(directory)
        self.assertTrue(backup.is_dir())
        self.assertEqual(backup.parent, self.workspace)
        self.assertTrue(backup.name.startswith(f"{PROJECT_NAME}_重置备份_"), backup.name)
        self.assertTrue((backup / "还原说明.txt").is_file())
        self.assertEqual(self.messages_in(backup), 7)

    def test_two_backup_families_do_not_crowd_each_other(self) -> None:
        for index in range(1, 4):
            (self.workspace / f"{PROJECT_NAME}_重置备份_2026100{index}-120000").mkdir()
            (self.workspace / f"{CLEAR_PREFIX}2026100{index}-120000").mkdir()
        manage.purge_old_clear_backups(keep=1, protect=f"{CLEAR_PREFIX}20261003-120000")
        self.assertEqual(sorted(path.name for path in self.autosaves()), [f"{CLEAR_PREFIX}20261003-120000"])
        resets = sorted(path.name for path in self.autosaves(f"{PROJECT_NAME}_重置备份_"))
        self.assertEqual(resets, [f"{PROJECT_NAME}_重置备份_2026100{i}-120000" for i in (1, 2, 3)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
