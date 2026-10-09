"""防刷屏闸门测试：空白 / 零宽 / 纯符号 / 重复内容 / 发言频率 / 发言收益冷却。

这些判据都在「内容 + 会话状态」这一层，所以直接调 screen_outgoing / save_message，
不必起服务、不必连 WebSocket。词库用临时文件，理由同 test_moderation：
老师改一个词不该让测试变红。
"""
import asyncio
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as chat

BOM = b"\xef\xbb\xbf"


class FakeSession:
    """只用来验「拒收提示」的节流：记录被发出去的提示，不碰网络。"""

    def __init__(self) -> None:
        self.speak_notices: dict[str, float] = {}
        self.sent: list[dict] = []

    async def send(self, payload: dict) -> None:
        self.sent.append(payload)


class SpamFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lan-chat-spam-")
        self.root = Path(self.temp.name)
        chat.DB_PATH = self.root / "chatroom.db"
        self.original_announce_dir = chat.ANNOUNCE_DIR
        chat.ANNOUNCE_DIR = self.root / "公告"
        self.original_legacy = chat.LEGACY_ANNOUNCE_FILE
        chat.LEGACY_ANNOUNCE_FILE = self.root / "没有这个旧公告.txt"

        self.words = self.root / "违禁词库.txt"
        self.words.write_bytes(BOM + "傻逼\r\n".encode("utf-8"))
        self.original_words_file = chat.WORDS_FILE
        self.original_interval = chat.WORDS_CHECK_INTERVAL
        chat.WORDS_FILE = self.words
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
        # 复用连接必须先真关掉，否则临时目录里的 chatroom.db 删不掉（同 test_moderation）。
        chat.close_db()
        chat.ANNOUNCE_DIR = self.original_announce_dir
        chat.LEGACY_ANNOUNCE_FILE = self.original_legacy
        self.temp.cleanup()

    # ------------------------------------------------------------------ 工具
    def screen(self, text):
        """只验「内容判据」：清空两个滑动窗口，别让频率 / 重复闸门掺进来。"""
        self.user.speak_times.clear()
        self.user.recent_speaks.clear()
        return chat.screen_outgoing(self.user, text)

    def make_user(self, user_id: str, username: str) -> "chat.Session":
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

    # ------------------------------------------------------ 该拦的：空内容 / 零宽
    def test_zero_width_only_cannot_become_an_empty_message(self):
        """零宽字符 strip() 去不掉（它的 isspace() 是假），要等 normalize 之后才现形。

        改动前这里会放行一条 content="" 的消息：屏幕上多一个空气泡，而且照样加金币。
        """
        for text in ("\u200b\u200b\u200b", "  \u200b\u200b  ", "\u200b\r\n\u200b", "\u200b" * 40):
            with self.subTest(text=repr(text)):
                content, violated, reason = self.screen(text)
                self.assertIsNone(content, "零宽内容被当成正常消息放出去了")
                self.assertTrue(reason, "拦下了却没告诉用户为什么")
                self.assertFalse(violated, "空白内容不该按「说脏话」处理")

    def test_plain_blank_is_still_dropped_silently(self):
        """纯空白（半角/全角空格、换行）保持改动前的行为：静默丢弃，不弹提示。"""
        for text in ("", "   ", "\u3000\u3000", "\n\n", " \t "):
            with self.subTest(text=repr(text)):
                content, violated, reason = chat.screen_outgoing(self.user, text)
                self.assertIsNone(content)
                self.assertEqual(reason, "", "纯空白不该给用户弹提示")

    # ------------------------------------------------------ 该拦的：无意义内容
    def test_only_symbol_lines_are_rejected(self):
        for text in ("!" * 16, "？" * 10, "~!@#$%^&*()_+", "。。。。。。", "！！！？？？"):
            with self.subTest(text=text):
                self.assertIsNone(self.screen(text)[0], f"{text} 应该被拦下")

    def test_single_character_repeat_is_rejected(self):
        for text in ("啊" * 500, "a" * 30, "。" * 20):
            with self.subTest(text=text[:8] + "…"):
                self.assertIsNone(self.screen(text)[0], f"{text[:8]}… 应该被拦下")

    def test_normal_expression_is_not_touched(self):
        """加严判据最容易误伤的就是这些正常表达，逐条确认照常发出去。"""
        for text in ("哈哈哈哈", "!!!!", "😀" * 8, "👍👍👍", "太好了！！！", "+1",
                     "今天天气不错我们一起去操场打球吧", "\u200b我\u200b", "??", "……"):
            with self.subTest(text=text):
                content, violated, _ = self.screen(text)
                self.assertIsNotNone(content, f"{text} 被误伤了")
                self.assertFalse(violated)

    # ------------------------------------------------------ 重复内容
    def test_repeated_lines_are_blocked(self):
        self.user.recent_speaks.clear()
        passed = 0
        for _ in range(6):
            self.user.speak_times.clear()   # 挪开频率闸门，单独验重复闸门
            if chat.screen_outgoing(self.user, "同样的一句话")[0] is not None:
                passed += 1
        self.assertEqual(passed, chat.SPEAK_REPEAT_LIMIT,
                         f"连发同一句话放行了 {passed} 条（预期 {chat.SPEAK_REPEAT_LIMIT} 条）")

    def test_alternating_repeat_lines_are_blocked(self):
        """「甲 乙 甲 乙 甲 乙」这种交替刷屏也要拦住。

        窗口取 5 时交替出现的计数永远只到 2、够不到阈值 3 —— 这条用例就是为此立的。
        """
        self.user.recent_speaks.clear()
        passed = 0
        for index in range(12):
            self.user.speak_times.clear()
            if chat.screen_outgoing(self.user, "甲" if index % 2 == 0 else "乙")[0] is not None:
                passed += 1
        self.assertLess(passed, 12, "交替刷屏一条都没拦住")

    def test_different_lines_are_not_treated_as_repeat(self):
        self.user.speak_times.clear()
        self.user.recent_speaks.clear()
        for index in range(4):
            self.user.speak_times.clear()
            with self.subTest(index=index):
                self.assertIsNotNone(
                    chat.screen_outgoing(self.user, f"这是第{index}句完全不同的话")[0],
                    "内容不一样却被当成重复刷屏")

    # ------------------------------------------------------ 发言频率
    def test_speaking_rate_is_limited(self):
        self.user.speak_times.clear()
        self.user.recent_speaks.clear()
        passed = 0
        for index in range(12):
            if chat.screen_outgoing(self.user, f"不重复的第{index}句话")[0] is not None:
                passed += 1
        self.assertEqual(passed, chat.SPEAK_MAX_IN_WINDOW,
                         f"12 条瞬间连发放行了 {passed} 条（预期 {chat.SPEAK_MAX_IN_WINDOW} 条）")

        # 窗口滑过去之后必须恢复，否则学生被永久禁言
        self.user.speak_times.clear()
        self.assertIsNotNone(chat.screen_outgoing(self.user, "安静一下再说")[0],
                             "窗口过后仍然发不出去")

    # ------------------------------------------------------ 发言收益冷却
    def test_talk_income_is_cooled_down(self):
        """连发不该持续换金币（改动前实测 170 条能刷出 240 金币）。"""
        before = self.user.coins
        for index in range(10):
            chat.save_message(self.user, f"第{index}句", "text")
        gained = self.user.coins - before
        self.assertLessEqual(gained, 2, f"连发 10 条净赚 {gained} 金币，收益没有冷却")

        # 冷却是按时间算的，不是按条数：过了间隔就该恢复
        self.user.last_income_at -= chat.SPEAK_INCOME_INTERVAL + 1
        chat.save_message(self.user, "隔了一会儿再说一句", "text")
        self.assertEqual(self.user.coins - before, gained + 1, "冷却过后收益没有恢复")

    def test_talk_income_cool_down_is_persisted(self):
        """收益冷却时刻要落库：只放内存的话，重连一次就重置了，等于给刷币留门。"""
        user = self.make_user("95", "刷币的")
        chat.save_message(user, "第一句", "text")
        first = user.last_income_at
        self.assertGreater(first, 0, "发言后没有记下收益时刻")
        with closing(chat.get_db()) as db:
            stored = db.execute("SELECT last_income_at FROM users WHERE id = 95").fetchone()[0]
        self.assertAlmostEqual(stored, first, places=3, msg="last_income_at 没有落库")

        with closing(chat.get_db()) as db:
            row = db.execute("SELECT * FROM users WHERE id = 95").fetchone()
        reborn = chat.Session("token-95-again", "95", "刷币的", "user", "刷币的", dict(row))
        self.assertAlmostEqual(reborn.last_income_at, first, places=3,
                               msg="登出重登把收益冷却重置了")

    def test_gift_income_is_cooled_down_like_talking(self):
        """送礼也走发言冷却。

        旧口径是「送礼本身在消耗道具，所以不设闸门」—— 但道具有**收礼方**：
        两个人来回送同一件礼物时道具根本没消失，每送一次双方各 +1 金币 / +2 经验，就是一台印钞机。
        """
        before = self.user.exp
        for _ in range(3):
            chat.save_message(self.user, "送出礼物", "gift")
        self.assertLessEqual(self.user.exp - before, 2, "连送 3 次礼物没被冷却拦住")

        # 冷却是按时间算的：过了间隔照样给收益
        self.user.last_income_at -= chat.SPEAK_INCOME_INTERVAL + 1
        chat.save_message(self.user, "过了一会儿又送一次", "gift")
        self.assertEqual(self.user.exp - before, 4, "冷却过后礼物收益没有恢复")

    def test_gift_charm_shares_the_same_income_gate(self):
        """收礼方的**魅力**必须和送出方的金币/经验共用同一道闸门。

        缺口原样复现：两个人对送同一件礼物，道具绕一圈回到手里，魅力却每次都在涨 ——
        玫瑰 price=10 → 每次 +2，对送 10 个来回 = 双方各 +20（本轮修的正是这个）。
        修法是把判据收成 income_gate_open() 一个函数，送礼那边调 award_gift_charm()。
        """
        sender = chat.Session("token-s", "9001", "送礼的", "user", "送礼的")
        target = chat.Session("token-t", "9002", "收礼的", "user", "收礼的")
        item = {"price": 10}

        sender.last_income_at = 0                      # 闸门开着 → 给
        self.assertEqual(chat.award_gift_charm(sender, target, item), 2)
        sender.last_income_at = time.time()            # 刚记过账 → 关着 → 不给
        for _ in range(9):
            self.assertEqual(chat.award_gift_charm(sender, target, item), 0)
        self.assertEqual(target.charm, 2, f"对送 10 次刷走了 {target.charm} 点魅力")

        # 反向对照：过了一个间隔照样给（别把玩法一起关掉）
        sender.last_income_at -= chat.SPEAK_INCOME_INTERVAL + 1
        self.assertEqual(chat.award_gift_charm(sender, target, item), 2)
        self.assertEqual(target.charm, 4)

    def test_use_item_gives_no_income(self):
        """佩戴头像 / 徽章**不消耗任何道具**（只改一个展示字段），给它收益就是白拿。

        改动前实测：连点 10 次「使用头像」净赚 10 金币 + 20 经验。
        """
        coins, exp = self.user.coins, self.user.exp
        for _ in range(10):
            chat.save_message(self.user, "使用了头像", "use_item")
        self.assertEqual(self.user.coins - coins, 0, f"使用道具白拿了 {self.user.coins - coins} 金币")
        self.assertEqual(self.user.exp - exp, 0, f"使用道具白拿了 {self.user.exp - exp} 经验")

    # ------------------------------------------------------ 拒收提示的节流
    def test_rejection_notice_is_throttled(self):
        """提示本身也要节流，否则「发言太快了」会变成新的刷屏源（前端会把它落进消息流）。"""
        session = FakeSession()

        async def hammer():
            for _ in range(10):
                await chat.reject_speak(session, "发言太快了")

        asyncio.run(hammer())
        self.assertEqual(len(session.sent), 1, f"同一条提示重复发了 {len(session.sent)} 次")

        # 节流窗口过后应当能再提醒一次
        session.speak_notices["发言太快了"] = time.time() - chat.SPEAK_NOTICE_INTERVAL - 1
        asyncio.run(chat.reject_speak(session, "发言太快了"))
        self.assertEqual(len(session.sent), 2, "节流窗口过后不再提醒了")

    def test_blank_drop_never_notices(self):
        """reason 为空表示静默丢弃，不该被 reject_speak 发出任何东西。"""
        session = FakeSession()
        asyncio.run(chat.reject_speak(session, ""))
        self.assertEqual(session.sent, [], "静默丢弃却发了提示")

    # ------------------------------------------------------ 脏话与刷屏互不干扰
    def test_profanity_still_masked_and_penalised(self):
        """防刷屏不能让原有的违禁词处理失效。"""
        content, violated, reason = self.screen("你这个傻逼")
        self.assertIsNotNone(content, "正常长度的脏话消息不该被防刷屏拦掉")
        self.assertTrue(violated, "违禁词没有被识别")
        self.assertEqual(content, "你这个**")
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
