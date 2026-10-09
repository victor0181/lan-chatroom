"""活动消息分档（聊天区折叠带）的测试。

要守住的三条契约：
  ① 分档判据只有一处（app.activity_tier）。「没分类的玩法」兜底成 notice ——
     出错的方向必须是「没折叠、多占几行」，绝不可以是「消息被藏起来、谁都没看见」；
  ② 活动消息带着 activity_kind 落库，**实时广播**与**刷新后读历史**拿到的档位必须一致。
     这条最容易坏：只在 save_message 里补字段、忘了 row_to_message 时，
     表现是「刚玩时折叠得好好的，按一下 F5 就全散开了」——不报错、只静默失效；
  ③ 每个互动动作产出的提示类型要对得上（抢红包=redpack_claim、投票=prediction_vote…），
     将来加了新玩法忘了分类，这里会红。
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_import_data = tempfile.TemporaryDirectory(prefix="lan-chat-feed-import-")
os.environ["CHAT_DATA_DIR"] = _import_data.name
# 公告目录同样要在 import 之前隔离：init_db() 结尾有「搬老公告」的迁移，不隔离会写进项目目录。
os.environ["CHAT_ANNOUNCE_DIR"] = os.path.join(_import_data.name, "公告")
import app as chat
import interactions as games

STREAM_KINDS = ("lottery", "wheel", "redpack_claim", "prediction_vote", "idiom")
# truth / dare 从流水档挪到通知档（2026-10-03）：它们是**等人回应**的事件，
# 折进「活动消息 N 条」就没人知道该接话，题直接废了。
NOTICE_KINDS = ("redpack_create", "prediction_start", "game_start", "game_win", "horn", "truth", "dare")


class Socket:
    def __init__(self):
        self.events: list[dict] = []

    async def send_json(self, data):
        self.events.append(data)


class TierRuleTests(unittest.TestCase):
    """分档判据本身：纯函数，不开数据库。"""

    def test_stream_kinds_are_stream(self):
        for kind in STREAM_KINDS:
            self.assertEqual(chat.activity_tier(kind), "stream", kind)

    def test_notice_kinds_are_notice(self):
        for kind in NOTICE_KINDS:
            self.assertEqual(chat.activity_tier(kind), "notice", kind)

    def test_truth_and_dare_stay_in_notice_tier(self):
        """★★ 真心话 / 大冒险必须留在通知档。

        这两类是**等人回应**的事件（抽到一个题，等别人接话）。
        折进「活动消息 N 条」折叠带 = 别人根本不知道有题在等，题就作废了 ——
        折叠带省下的那点版面，换不来「有人回应」这个前提。
        将来若有人为了「清爽」把它们塞回流水档，这条用例会当场变红。
        """
        for kind in ("truth", "dare"):
            self.assertEqual(chat.activity_tier(kind), "notice", kind)
            self.assertNotIn(kind, chat.ACTIVITY_STREAM_KINDS, kind)

    def test_unknown_kind_falls_back_to_notice(self):
        """★ 兜底方向必须是「不折叠」。

        新玩法忘了分类时，最坏结果是聊天区多占几行；
        如果兜底成 stream，那才是真出事 —— 消息会被收进折叠带，学生不点开就等于没看见。
        """
        self.assertEqual(chat.activity_tier("brand_new_game"), "notice")
        self.assertEqual(chat.activity_tier(""), "notice")
        self.assertEqual(chat.activity_tier("Lottery"), "notice", "大小写不同应视为没分类，而不是猜成 lottery")

    def test_only_activity_messages_carry_a_tier(self):
        """普通聊天、礼物、用道具都不该带档位 —— 前端凭这个字段分组，带上就误折叠。"""
        for kind in ("text", "gift", "use_item", "private", "file"):
            self.assertNotIn("activity_tier", chat.with_activity_tier({"kind": kind}), kind)

    def test_tier_is_derived_not_copied(self):
        self.assertEqual(chat.with_activity_tier({"kind": "activity", "activity_kind": "lottery"})["activity_tier"], "stream")
        self.assertEqual(chat.with_activity_tier({"kind": "activity", "activity_kind": "horn"})["activity_tier"], "notice")
        # 档位清单只有一份，且就是被 activity_tier() 使用的那一份。
        self.assertEqual(set(chat.ACTIVITY_STREAM_KINDS), set(STREAM_KINDS))


class PersistenceTests(unittest.TestCase):
    """落库与读回：实时看到的档位，刷新页面之后必须还是同一个。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lan-chat-feed-")
        self.root = Path(self.temp.name)
        chat.DB_PATH = self.root / "chatroom.db"
        self.original_announce_dir = chat.ANNOUNCE_DIR
        chat.ANNOUNCE_DIR = self.root / "公告"
        self.original_legacy = chat.LEGACY_ANNOUNCE_FILE
        chat.LEGACY_ANNOUNCE_FILE = self.root / "没有这个旧公告.txt"
        chat.sessions.clear()
        chat.manager.sessions.clear()
        chat.init_db()

    def tearDown(self):
        chat.ANNOUNCE_DIR = self.original_announce_dir
        chat.LEGACY_ANNOUNCE_FILE = self.original_legacy
        chat.sessions.clear()
        chat.manager.sessions.clear()
        chat.close_db()
        self.temp.cleanup()

    def make_session(self, user_id="1", nickname="测试用户", role="user", room="lobby"):
        with closing(chat.get_db()) as db:
            db.execute(
                "INSERT OR REPLACE INTO users (id, username, password_hash, nickname, role, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, f"user{user_id}", chat.hash_password("pw-123456"), nickname, role, chat.now_text()),
            )
            db.commit()
            row = dict(db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
        session = chat.Session(f"token-{user_id}", user_id, nickname, role, f"user{user_id}", row)
        session.websocket = Socket()
        session.room_id = room
        session.coins = 500
        chat.sessions[session.token] = session
        chat.manager.sessions[session.user_id] = session
        return session

    def history(self, room_id="lobby"):
        """走真实的历史接口，拿前端刷新页面时会看到的那一份。

        直接调用端点函数时必须把每个参数都显式传满：FastAPI 的 Query(default=...) 只有在
        经过框架解析时才取默认值，手工调用时拿到的是 Query 对象本身 ——
        private_with 漏传就是「真值」，整个请求会被误判成私聊历史，然后去碰 request.headers 崩掉。

        ★ 2026-10-03 起公聊历史**也要 token** 了（以前谁都能读，被请出聊天室的人照样能把
        整个房间的聊天记录拉走）。所以这里还得再补一个「假的 Request」：端点只用到
        request.headers.get("X-Session-Token")，一个带 headers 的鸭子对象就够，
        不必真造 Starlette 的 Request（那要凑 scope / receive，成本高得多）。
        token 从 chat.sessions 现取、不写死 —— 写死的话换个 user_id 就会静默变成 401。
        """
        token = next(iter(chat.sessions), "")
        request = SimpleNamespace(headers={"X-Session-Token": token})
        return asyncio.run(chat.messages(request, room_id=room_id, private_with=None, limit=200))["messages"]

    def test_messages_table_has_activity_kind_column(self):
        with closing(chat.get_db()) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(messages)").fetchall()}
        self.assertIn("activity_kind", columns, "少了这一列，活动类型就没法落库，刷新后档位会丢")

    def test_save_message_carries_kind_and_tier(self):
        session = self.make_session()
        stream = chat.save_message(session, "🎰 抽奖：抽中 5 金币", "activity", activity_kind="lottery")
        notice = chat.save_message(session, "🎮 发起小游戏：猜数字", "activity", activity_kind="game_start")
        self.assertEqual(stream["activity_kind"], "lottery")
        self.assertEqual(stream["activity_tier"], "stream")
        self.assertEqual(notice["activity_kind"], "game_start")
        self.assertEqual(notice["activity_tier"], "notice")

    def test_plain_chat_never_gets_a_tier(self):
        session = self.make_session()
        plain = chat.save_message(session, "老师好")
        self.assertNotIn("activity_tier", plain)
        self.assertIsNone(self.history()[-1].get("activity_tier"))

    def test_history_round_trip_keeps_the_tier(self):
        """★★ 本文件最重要的一条：实时与历史的口径必须一致。

        只在 save_message() 里补 activity_tier、忘了 row_to_message() 时，
        表现是「刚玩时折叠得好好的，按一下 F5 全散开」——不报错、不崩，只静默失效。
        """
        session = self.make_session()
        chat.save_message(session, "🎰 抽奖：抽中 5 金币", "activity", activity_kind="lottery")
        chat.save_message(session, "🎮 发起小游戏：猜数字", "activity", activity_kind="game_start")
        chat.save_message(session, "📜 接上「手到擒来」", "activity", activity_kind="idiom")
        chat.save_message(session, "老师好", "text")

        tiers = [(item["kind"], item.get("activity_kind"), item.get("activity_tier")) for item in self.history()]
        self.assertEqual(tiers, [
            ("activity", "lottery", "stream"),
            ("activity", "game_start", "notice"),
            ("activity", "idiom", "stream"),
            ("text", "", None),
        ])

    def test_old_rows_without_activity_kind_read_back_as_notice(self):
        """迁移之前的老活动行（activity_kind 为空）读回来按 notice 处理。

        按 notice 而不是丢掉：老数据照常显示在聊天区。若哪天有人把兜底改成 stream，
        历史上所有老活动消息会一夜之间被折叠进带子里。
        """
        session = self.make_session()
        with closing(chat.get_db()) as db:
            db.execute(
                "INSERT INTO messages (sender_id, sender_name, sender_role, room, room_id, sender_level,"
                " content, kind, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'activity', ?)",
                (session.user_id, session.nickname, session.role, "综合大厅", "lobby", 1,
                 "🎰 老版本留下的活动消息", chat.now_text()),
            )
            db.commit()
        row = [item for item in self.history() if item["kind"] == "activity"][-1]
        self.assertEqual(row.get("activity_tier"), "notice")

    def test_legacy_database_gets_the_column_added(self):
        """老库没有这一列 —— init_db() 要能自动补上，而不是让活动消息一落库就整条失败。"""
        chat.close_db()
        (self.root / "chatroom.db").unlink(missing_ok=True)
        con = sqlite3.connect(self.root / "chatroom.db")
        # 老表 = 新表去掉 activity_kind，其余列一个不少：
        # init_db() 里还有一句按 kind 过滤的修数据语句，表建得太省会在那儿先炸掉，
        # 测出来的就不是「能不能补列」，而是「测试自己造了个不合格的老库」。
        con.execute(
            "CREATE TABLE messages ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " sender_id TEXT NOT NULL, sender_name TEXT NOT NULL, sender_role TEXT NOT NULL,"
            " room TEXT NOT NULL DEFAULT '大厅', room_id TEXT NOT NULL DEFAULT 'lobby',"
            " recipient_id TEXT, sender_avatar TEXT NOT NULL DEFAULT '🙂',"
            " sender_badge TEXT NOT NULL DEFAULT '', sender_level INTEGER NOT NULL DEFAULT 1,"
            " text_style TEXT NOT NULL DEFAULT '', text_effect TEXT NOT NULL DEFAULT '',"
            " content TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'text',"
            " attachment_json TEXT, created_at TEXT NOT NULL)"
        )
        con.commit()
        con.close()

        chat.init_db()

        with closing(chat.get_db()) as db:
            after = {row[1] for row in db.execute("PRAGMA table_info(messages)").fetchall()}
        self.assertIn("activity_kind", after)


class LiveBroadcastTests(unittest.IsolatedAsyncioTestCase):
    """走真实的 handle_action：每个动作产出的提示，类型要对得上。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lan-chat-feed-live-")
        self.root = Path(self.temp.name)
        chat.DB_PATH = self.root / "chatroom.db"
        self.original_announce_dir = chat.ANNOUNCE_DIR
        chat.ANNOUNCE_DIR = self.root / "公告"
        self.original_legacy = chat.LEGACY_ANNOUNCE_FILE
        chat.LEGACY_ANNOUNCE_FILE = self.root / "没有这个旧公告.txt"
        chat.sessions.clear()
        chat.manager.sessions.clear()
        chat.init_db()
        games.init_interactions()
        self.user = self.make_user("小甲")

    def tearDown(self):
        chat.ANNOUNCE_DIR = self.original_announce_dir
        chat.LEGACY_ANNOUNCE_FILE = self.original_legacy
        chat.sessions.clear()
        chat.manager.sessions.clear()
        chat.close_db()
        self.temp.cleanup()

    def make_user(self, nickname, coins=500, room="lobby"):
        with closing(chat.get_db()) as db:
            cursor = db.execute(
                "INSERT INTO users(username,password_hash,nickname,created_at) VALUES(?,?,?,?)",
                (nickname, "unused", nickname, chat.now_text()),
            )
            db.commit()
        user = chat.Session(nickname, str(cursor.lastrowid), nickname, "user", nickname)
        user.websocket = Socket()
        user.room_id = room
        user.coins = coins
        chat.sessions[user.token] = user
        chat.manager.sessions[user.user_id] = user
        return user

    async def act(self, user, action, **fields):
        user.last_activity = 0
        user.last_horn = 0
        user.websocket.events.clear()
        await games.handle_action(user, {"action": action, **fields})
        results = [event for event in user.websocket.events
                   if event["type"] in ("interaction_result", "interaction_error")]
        return results[-1] if results else {}

    def announced(self, user):
        """取这次动作广播出去的活动消息（kind == activity 的 message）。"""
        return [event["message"] for event in user.websocket.events
                if event.get("type") == "message" and event.get("message", {}).get("kind") == "activity"]

    def one_announcement(self, user):
        messages = self.announced(user)
        self.assertEqual(len(messages), 1, f"本该正好广播一条活动提示，实际 {len(messages)} 条")
        return messages[0]

    def open_activity(self, user, kind):
        return next(item for item in games.activities(user.room_id)
                    if item["kind"] == kind and item["status"] == "open")

    async def test_lottery_and_wheel_are_stream(self):
        """抽奖/转盘一次一条，反复玩就一直在刷 —— 收进折叠带。"""
        for action in ("lottery", "wheel"):
            self.user.coins = 500
            await self.act(self.user, action)
            message = self.one_announcement(self.user)
            self.assertEqual(message["activity_kind"], action)
            self.assertEqual(message["activity_tier"], "stream")

    async def test_horn_is_notice(self):
        """大喇叭是花钱买曝光，砍进折叠带就等于把商品做废了。"""
        result = await self.act(self.user, "horn", content="大家好")
        self.assertEqual(result.get("type"), "interaction_result", result)
        message = self.one_announcement(self.user)
        self.assertEqual(message["activity_kind"], "horn")
        self.assertEqual(message["activity_tier"], "notice")

    async def test_truth_and_dare_are_notice(self):
        """★ 抽到题目的人要看得见，也要让别人看得见 —— 所以是通知档，不折叠。

        走的是真实 handle_action：确认广播出去的那条活动消息确实带 notice 档，
        不只是纯函数那一层自说自话。

        ⚠️ 两种玩法必须**换房间跑**—— 同一个房间同时只能有一道真心话/大冒险
        （防狂抽题狂自答刷金币的闸门），连着抽第二个必然被拒。这是产品行为，不是缺陷。
        """
        rooms = {"truth": "lobby", "dare": "games"}
        for action in ("truth", "dare"):
            with self.subTest(action=action):
                user = self.make_user(f"抽{action}", room=rooms[action])
                await self.act(user, action)
                message = self.one_announcement(user)
                self.assertEqual(message["activity_kind"], action)
                self.assertEqual(message["activity_tier"], "notice")

    async def test_redpack_create_is_notice_but_claiming_is_stream(self):
        """★ 同一个功能、两档待遇：发红包要召集（一条，留着），抢红包每人一条（折叠）。"""
        created = await self.act(self.user, "redpack_create", total=20, count=4)
        self.assertEqual(created.get("type"), "interaction_result", created)
        self.assertEqual(self.one_announcement(self.user)["activity_tier"], "notice")

        packet = self.open_activity(self.user, "redpack")
        second = self.make_user("小乙")
        await self.act(second, "redpack_claim", id=packet["id"])
        claim = self.one_announcement(second)
        self.assertEqual(claim["activity_kind"], "redpack_claim")
        self.assertEqual(claim["activity_tier"], "stream")

    async def test_prediction_start_is_notice_but_voting_is_stream(self):
        started = await self.act(self.user, "prediction_start")
        self.assertEqual(started.get("type"), "interaction_result", started)
        self.assertEqual(self.one_announcement(self.user)["activity_tier"], "notice")

        round_ = self.open_activity(self.user, "prediction")
        voter = self.make_user("小丙")
        await self.act(voter, "prediction_vote", id=round_["id"], choice="单数")
        vote = self.one_announcement(voter)
        self.assertEqual(vote["activity_kind"], "prediction_vote")
        self.assertEqual(vote["activity_tier"], "stream")

    async def test_idiom_chain_is_stream_but_winning_is_notice(self):
        """★ 接龙每接一句都刷一条 → 折叠；答对那一下有悬念 → 留着。"""
        await self.act(self.user, "game_start", game="idiom")
        self.assertEqual(self.one_announcement(self.user)["activity_tier"], "notice", "开局要召集，不该折叠")

        game = self.open_activity(self.user, "idiom")
        self.assertEqual(game["word"], "一心一意")
        await self.act(self.user, "game_answer", id=game["id"], answer="意气风发")
        chain = self.one_announcement(self.user)
        self.assertEqual(chain["activity_kind"], "idiom")
        self.assertEqual(chain["activity_tier"], "stream")

        # 换个房间：一个房间同时只能开一个小游戏，和小甲挤在一起会直接被拒。
        other = self.make_user("小丁", room="games")
        await self.act(other, "game_start", game="number")
        number = self.open_activity(other, "number")
        await self.act(other, "game_answer", id=number["id"], answer=str(number["answer"]))
        win = self.one_announcement(other)
        self.assertEqual(win["activity_kind"], "game_win")
        self.assertEqual(win["activity_tier"], "notice", "答对是喜报，要单独显示")

    async def test_wrong_guess_is_not_announced_at_all(self):
        """猜错只回本人（已有行为）：聊天区不该出现一行「猜错了」。"""
        await self.act(self.user, "game_start", game="number")
        game = self.open_activity(self.user, "number")
        wrong = 1 if game["answer"] != 1 else 2
        result = await self.act(self.user, "game_answer", id=game["id"], answer=str(wrong))
        self.assertEqual(result.get("type"), "interaction_result", result)
        self.assertIn("hint", result.get("result", {}))
        self.assertEqual(self.announced(self.user), [], "猜错不该广播到聊天区")

    async def test_every_announced_kind_is_classified(self):
        """所有广播出去的活动类型都必须有明确归属，不能靠兜底活着。

        兜底是给「将来新玩法还没上线」准备的保险，不是给现有玩法偷懒用的 ——
        真出现没分类的类型，这里报出来，好顺手补进 ACTIVITY_STREAM_KINDS。
        """
        seen: set[str] = set()
        plan = [
            ("lottery", {}),
            ("wheel", {}),
            ("horn", {"content": "喊一句"}),
            ("truth", {}),
            ("dare", {}),
            ("redpack_create", {"total": 10, "count": 2}),
            ("prediction_start", {}),
            ("game_start", {"game": "number"}),
        ]
        for action, fields in plan:
            self.user.coins = 500
            await self.act(self.user, action, **fields)
            seen.update(message["activity_kind"] for message in self.announced(self.user))
        self.assertTrue(seen, "一个活动提示都没收到，测试本身有问题")
        unclassified = sorted(kind for kind in seen
                              if kind not in chat.ACTIVITY_STREAM_KINDS and kind not in NOTICE_KINDS)
        self.assertEqual(unclassified, [], "这些活动类型没有归类，界面只能靠兜底处理：" + "、".join(unclassified))
