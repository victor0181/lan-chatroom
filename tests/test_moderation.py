"""聊天内容过滤与违规扣分测试。

词库是老师自己在项目根目录维护的 txt，测试必须用**自己的临时词库**，
否则老师改一个词就能让测试变红。这里所有用例都指向临时文件。
"""
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as chat

BOM = b"\xef\xbb\xbf"
# 固定测试词库：只放测试断言用得到的那几个词。
TEST_WORDS = "傻逼\r\n傻b\r\nfuck\r\n他妈\r\n妈的\r\n日你\r\nsb\r\n我操\r\n"


class ModerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lan-chat-moderation-")
        self.root = Path(self.temp.name)
        chat.DB_PATH = self.root / "chatroom.db"
        self.original_announce_dir = chat.ANNOUNCE_DIR
        chat.ANNOUNCE_DIR = self.root / "公告"
        self.original_legacy = chat.LEGACY_ANNOUNCE_FILE
        chat.LEGACY_ANNOUNCE_FILE = self.root / "没有这个旧公告.txt"

        self.words = self.root / "违禁词库.txt"
        self.words.write_bytes(BOM + TEST_WORDS.encode("utf-8"))
        self.original_words_file = chat.WORDS_FILE
        self.original_interval = chat.WORDS_CHECK_INTERVAL
        chat.WORDS_FILE = self.words
        # 生产环境每秒才查一次改动，测试里改成每次调用都查，省掉 sleep。
        chat.WORDS_CHECK_INTERVAL = 0.0
        chat.reset_wordlist_cache()

        chat.sessions.clear()
        chat.manager.sessions.clear()
        chat.init_db()
        self.user = chat.Session("token", "1", "测试用户", "user", "tester")

    def tearDown(self):
        chat.WORDS_FILE = self.original_words_file
        chat.WORDS_CHECK_INTERVAL = self.original_interval
        chat.reset_wordlist_cache()
        chat.sessions.clear()
        chat.manager.sessions.clear()
        # 与 test_interactions 同理：复用连接必须先真关掉，临时目录才删得掉。
        chat.close_db()
        chat.ANNOUNCE_DIR = self.original_announce_dir
        chat.LEGACY_ANNOUNCE_FILE = self.original_legacy
        self.temp.cleanup()

    def save_wordlist(self, content: str, encoding: str = "utf-8", bom: bool = True) -> None:
        """改写词库文件后**不**手动清缓存，走真实的“发现文件变了就重载”路径。"""
        self.words.write_bytes((BOM if bom else b"") + content.encode(encoding))

    def test_profanity_is_masked(self):
        masked, violated = chat.moderate_content("大家好，傻逼和fuck都不能说")
        self.assertTrue(violated)
        self.assertEqual(masked, "大家好，**和****都不能说")
        clean, clean_violation = chat.moderate_content("大家好，今天一起聊天")
        self.assertFalse(clean_violation)
        self.assertEqual(clean, "大家好，今天一起聊天")

    def test_violation_deducts_exactly_five_without_chat_income(self):
        masked, violated = chat.moderate_content("傻逼")
        self.assertTrue(violated)
        message = chat.save_message(self.user, masked, award_income=not violated)
        deducted = chat.apply_violation_penalty(self.user)
        self.assertEqual(deducted, 5)
        self.assertEqual(self.user.coins, 95)
        self.assertEqual(self.user.exp, 0)
        self.assertEqual(message["content"], "**")
        with closing(chat.get_db()) as db:
            row = db.execute("SELECT coins, exp, content FROM users JOIN messages ON messages.sender_id = users.id WHERE users.id = 1").fetchone()
        self.assertEqual((row[0], row[1], row[2]), (95, 0, "**"))

    def make_user(self, user_id: str, username: str) -> "chat.Session":
        """建一行真实用户 + 一个绑定它的会话（违规计数要落库，必须先有这一行）。"""
        with closing(chat.get_db()) as db:
            db.execute(
                "INSERT INTO users (id, username, password_hash, nickname, created_at) VALUES (?, ?, 'x', ?, ?)",
                (user_id, username, username, chat.now_text()),
            )
            db.commit()
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        user = chat.Session(f"token-{user_id}", user_id, username, "user", username, dict(row))
        chat.sessions[user.token] = user
        return user

    def test_violation_counter_is_persisted_so_relogin_cannot_erase_it(self):
        """违规计数必须落库：只放内存的话，登出重登就能无限规避禁言。"""
        user = self.make_user("91", "违规甲")
        for _ in range(2):
            chat.apply_violation_penalty(user)
        self.assertEqual(user.violations, 2)

        with closing(chat.get_db()) as db:
            stored = db.execute("SELECT violations FROM users WHERE id = 91").fetchone()[0]
        self.assertEqual(stored, 2, "违规计数没有写进 users 表")

        # 「登出重登」的真实路径：重新从数据库那一行构造会话
        with closing(chat.get_db()) as db:
            row = db.execute("SELECT * FROM users WHERE id = 91").fetchone()
        reborn = chat.Session("token-91-again", "91", "违规甲", "user", "违规甲", dict(row))
        self.assertEqual(reborn.violations, 2, "登出重登把违规计数清零了")

        chat.apply_violation_penalty(reborn)
        self.assertGreater(reborn.muted_until, time.time(), "第 3 次违规没有触发禁言")
        self.assertEqual(reborn.violations, 0, "触发禁言后应重新计数")

        # ★ 禁言截止时刻本身也必须落库：只写进内存的话，退出重登就把这次禁言绕过去了
        #   （原来的 bug 就是这个 —— 计数落库了，真正起作用的 muted_until 却没落库）。
        with closing(chat.get_db()) as db:
            row = db.execute("SELECT * FROM users WHERE id = 91").fetchone()
        after_relogin = chat.Session("token-91-third", "91", "违规甲", "user", "违规甲", dict(row))
        self.assertGreater(after_relogin.muted_until, time.time(),
                           "禁言没有落库，退出重登即可绕过自动禁言")

    def test_violation_window_restarts_the_count(self):
        """时间窗之外的违规不该累计 —— 否则隔几小时说一句脏话也会被禁言。"""
        user = self.make_user("92", "违规乙")
        chat.apply_violation_penalty(user)
        self.assertEqual(user.violations, 1)

        # 把「上次违规时刻」推到时间窗之外，模拟隔了很久才又违规一次
        user.violations_at -= chat.VIOLATION_WINDOW_SECONDS + 1
        chat.apply_violation_penalty(user)
        self.assertEqual(user.violations, 1, "时间窗外的违规被错误地累计了")
        self.assertEqual(user.muted_until, 0, "时间窗外不该禁言")

        # 对照组：同一个窗口内再连犯两次，累计到 3 → 必须禁言
        chat.apply_violation_penalty(user)
        chat.apply_violation_penalty(user)
        self.assertGreater(user.muted_until, time.time(), "窗口内连犯三次没有禁言")

    def test_separators_and_fullwidth_cannot_bypass_wordlist(self):
        for text in ("傻 逼", "傻。逼", "傻-逼", "傻*逼", "傻Ｂ", "f u c k"):
            with self.subTest(text=text):
                self.assertTrue(chat.moderate_content(text)[1], f"{text} 应该被拦住")

    def test_normal_sentences_are_not_touched(self):
        for text in ("他妈妈是老师", "我妈的病好多了", "今天我生日你没来", "我去操场打球",
                     "我操作一下这个软件", "USB 接口坏了", "class 演示", "Shift 键在哪"):
            with self.subTest(text=text):
                masked, violated = chat.moderate_content(text)
                self.assertFalse(violated, f"{text} 被误伤了：{masked}")
                self.assertEqual(masked, text)

    def test_new_word_takes_effect_without_restart(self):
        self.assertFalse(chat.moderate_content("你这个测试脏话")[1])
        self.save_wordlist(TEST_WORDS + "测试脏话\r\n")   # 进程没有任何重启动作
        masked, violated = chat.moderate_content("你这个测试脏话")
        self.assertTrue(violated, "词库改了却要重启才生效")
        self.assertEqual(masked, "你这个****")

    def test_removed_word_stops_matching_without_restart(self):
        self.save_wordlist(TEST_WORDS + "测试脏话\r\n")
        self.assertTrue(chat.moderate_content("测试脏话")[1])
        self.save_wordlist(TEST_WORDS)
        self.assertFalse(chat.moderate_content("测试脏话")[1])

    def test_wordlist_accepts_gbk_and_bom_less_utf8(self):
        # 记事本「另存为 ANSI」后文件是 GBK，不回退等于整个词库失效。
        self.save_wordlist("污言\r\n", encoding="gbk", bom=False)
        self.assertTrue(chat.moderate_content("污言")[1])
        self.save_wordlist("污言\r\n", encoding="utf-8", bom=False)
        self.assertTrue(chat.moderate_content("污言")[1])

    def test_missing_wordlist_is_created_with_defaults(self):
        missing = self.root / "还没有的词库.txt"
        chat.WORDS_FILE = missing
        chat.reset_wordlist_cache()
        self.assertFalse(missing.exists())
        self.assertTrue(chat.moderate_content("傻逼")[1], "词库文件缺失时应退回内置默认词表")
        self.assertTrue(missing.exists(), "词库文件缺失时应自动生成一份")
        self.assertTrue(missing.read_bytes().startswith(BOM), "生成的词库应带 BOM，记事本才不会乱码")

    def test_empty_wordlist_disables_filtering_without_crashing(self):
        self.save_wordlist("# 全是注释，没有词条\r\n\r\n")
        masked, violated = chat.moderate_content("傻逼")
        self.assertFalse(violated)
        self.assertEqual(masked, "傻逼")

    def test_inline_markers_control_matching(self):
        self.save_wordlist("污言 <-我\r\n图腾 ->腾\r\nfk =\r\n")
        self.assertFalse(chat.moderate_content("我污言")[1])   # 前置阻断
        self.assertTrue(chat.moderate_content("他污言")[1])
        self.assertFalse(chat.moderate_content("图腾腾")[1])   # 后置阻断
        self.assertTrue(chat.moderate_content("图腾好")[1])
        self.assertTrue(chat.moderate_content("fk")[1])        # 英文整词
        self.assertFalse(chat.moderate_content("fkzz")[1])

    def test_builtin_guards_survive_a_full_rewrite(self):
        """老师整份重写词库后，「零误伤」保护不能丢。"""
        self.save_wordlist("他妈\r\n妈的\r\n日你\r\nsb\r\n我操\r\n")
        for text in ("他妈妈是老师", "我妈的病好多了", "今天我生日你没来", "USB 接口坏了", "我操作一下这个软件"):
            with self.subTest(text=text):
                self.assertFalse(chat.moderate_content(text)[1], f"内置保护失效：{text}")
        self.assertTrue(chat.moderate_content("他妈的")[1], "该拦的不能跟着一起放行")

    def test_history_message_is_filtered_on_the_way_out(self):
        """后加的词要能过滤历史消息，且不能改动内存里的原消息。"""
        self.save_wordlist(TEST_WORDS + "测试脏话\r\n")
        with closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            db.execute("CREATE TABLE messages (content TEXT, sender_name TEXT, attachment_json TEXT)")
            db.execute("INSERT INTO messages VALUES (?, ?, ?)", ("这是个测试脏话句子", "测试脏话用户", None))
            row = db.execute("SELECT * FROM messages").fetchone()
        original = dict(row)
        message = chat.row_to_message(row)
        self.assertEqual(message["content"], "这是个****句子")
        self.assertEqual(message["sender_name"], "****用户")
        self.assertEqual(dict(row), original, "不能就地改动数据库取出来的原始行")


if __name__ == "__main__":
    unittest.main()
