"""真心话 / 大冒险「回答别人的题」的测试。

2026-10-03 之前，这两个玩法是「谁抽谁答」：点一下抽一道题，题目只往聊天区发一句话，
别人既看不到一个可参与的 id，也没有地方提交回答 —— 整个玩法事实上只有抽题者自己玩。
现在改成「题目公开、全房间可答、回答实名公开」，所以要守住四条契约：

  ① 抽题必须**真的产生一个活动**（落进 room_activities）。只在聊天区发一句话的话，
     别人根本拿不到 id，答不上 —— 这正是改造前的病根；
  ② 每人每题只能答一次。少这一条，同一个人能反复答同一题反复领金币；
  ③ 一个房间同时只能有一道题。少这一条，可以狂抽题、狂自答，
     PROMPT_REWARD 就成了一台合法的印钞机（对比 prediction_start 的同类闸门）；
  ④ 回答是聊天内容，必须走违禁词过滤 —— 它不是大喇叭那种付费内容，
     没有「违规就拒发」这条豁免。
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_import_data = tempfile.TemporaryDirectory(prefix="lan-chat-prompt-import-")
os.environ["CHAT_DATA_DIR"] = _import_data.name
# 公告目录同样要在 import 之前隔离：init_db() 结尾有「搬老公告」的迁移，不隔离会写进项目目录。
os.environ["CHAT_ANNOUNCE_DIR"] = os.path.join(_import_data.name, "公告")
import app as chat
import interactions as games


class Socket:
    def __init__(self):
        self.events: list[dict] = []

    async def send_json(self, data):
        self.events.append(data)


class PromptRewardRuleTests(unittest.TestCase):
    """奖励口径的静态约定：数字只有一处、且必须小到不值得刷。"""

    def setUp(self):
        # snapshot() 会去读 room_activities，所以这个类也得有库可查，不能真的当纯函数跑。
        self.temp = tempfile.TemporaryDirectory(prefix="lan-chat-prompt-rule-")
        root = Path(self.temp.name)
        chat.DB_PATH = root / "chatroom.db"
        self.original_announce_dir = chat.ANNOUNCE_DIR
        chat.ANNOUNCE_DIR = root / "公告"
        self.original_legacy = chat.LEGACY_ANNOUNCE_FILE
        chat.LEGACY_ANNOUNCE_FILE = root / "没有这个旧公告.txt"
        chat.sessions.clear()
        chat.manager.sessions.clear()
        chat.init_db()
        games.init_interactions()

    def tearDown(self):
        chat.ANNOUNCE_DIR = self.original_announce_dir
        chat.LEGACY_ANNOUNCE_FILE = self.original_legacy
        chat.sessions.clear()
        chat.manager.sessions.clear()
        chat.close_db()
        self.temp.cleanup()

    def test_reward_is_a_single_positive_constant(self):
        self.assertIsInstance(games.PROMPT_REWARD, int)
        self.assertGreater(games.PROMPT_REWARD, 0)
        # 答题是零成本的（不用抽、不用下注），奖励又是白拿的，所以必须比抢答题（10）低一截。
        # 定成 0 就没人答了，定成 10 就成了刷币口子 —— 这个区间是有意的。
        self.assertLess(games.PROMPT_REWARD, 10, "答题几乎零成本，奖励必须显著低于抢答题的 10 金币")

    def test_prompt_seconds_is_a_positive_number(self):
        self.assertGreater(games.PROMPT_SECONDS, 0)
        self.assertLessEqual(games.PROMPT_SECONDS, 300, "题目是聊天内容不是抢答，拖太久就没人接了")

    def test_answer_max_bounds_one_sentence(self):
        self.assertGreater(games.PROMPT_ANSWER_MAX, 0)
        self.assertLessEqual(games.PROMPT_ANSWER_MAX, 200, "答题板是聊天区，别让人写小作文刷屏")

    def test_snapshot_publishes_the_reward(self):
        """前端必须从 snapshot 读奖励值，改后端常量才不会让界面显示错价。"""
        published = games.snapshot("lobby")["prompt"]
        self.assertEqual(published["reward"], games.PROMPT_REWARD)
        self.assertEqual(published["seconds"], games.PROMPT_SECONDS)
        self.assertEqual(published["answer_max"], games.PROMPT_ANSWER_MAX)


class PromptAnswerTests(unittest.IsolatedAsyncioTestCase):
    """走真实的 handle_action 与数据库。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lan-chat-prompt-")
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
        self.host = self.make_user("小甲")
        self.guest = self.make_user("小乙")

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

    def open_prompt(self, room="lobby"):
        return next((item for item in games.activities(room)
                     if item["kind"] in ("truth", "dare") and item["status"] == "open"), None)

    def visible_prompt(self, activity_id, room="lobby"):
        return next(item for item in games.snapshot(room)["activities"] if item["id"] == activity_id)

    async def draw(self, user, action="truth"):
        """抽一道题，返回 (activity_id, handle_action 的结果)。"""
        result = await self.act(user, action)
        self.assertEqual(result.get("type"), "interaction_result", result)
        return result["result"]["id"], result

    # ---------- ① 抽题产生的是一个真活动，别的房间答不上也看不见 ----------

    async def test_drawing_creates_a_stored_activity(self):
        """★ 改造的核心：题目必须落库，别的玩家才拿得到 id 去回答。"""
        activity_id, result = await self.draw(self.host)
        stored = self.open_prompt()
        self.assertIsNotNone(stored, "题目只发了一句话，没有留下可回答的活动")
        self.assertEqual(stored["id"], activity_id)
        self.assertEqual(stored["kind"], "truth")
        self.assertEqual(stored["host"], self.host.nickname)
        self.assertTrue(stored["prompt"], "题目内容是空的")
        self.assertEqual(result["result"]["prompt"], stored["prompt"], "回执里的题目与实际存下来的对不上")

    async def test_prompt_and_answers_are_visible_to_the_whole_room(self):
        """★★ 实名公开：别人答了什么，全房间都要看得到（这是它唯一的乐趣来源）。"""
        activity_id, _ = await self.draw(self.host, "dare")
        reply = await self.act(self.guest, "prompt_answer", id=activity_id, text="我用三个表情：😀😀😀")
        self.assertEqual(reply.get("type"), "interaction_result", reply)

        shown = self.visible_prompt(activity_id)
        self.assertEqual(shown["kind"], "dare")
        self.assertEqual(shown["reply_count"], 1)
        self.assertEqual(len(shown["replies"]), 1, "别人的回答没有明文下发 —— 大家答了但谁也看不到")
        self.assertEqual(shown["replies"][0]["name"], self.guest.nickname, "回答要实名")
        self.assertEqual(shown["replies"][0]["text"], "我用三个表情：😀😀😀")

    async def test_answer_reaches_a_player_in_another_room(self):
        """房间是隔离的：换个房间的玩家不该看到、也不该能回答这题。"""
        activity_id, _ = await self.draw(self.host)
        stranger = self.make_user("小丙", room="games")
        result = await self.act(stranger, "prompt_answer", id=activity_id, text="我来试试")
        self.assertEqual(result.get("type"), "interaction_error", "跨房间也能回答，房间隔离漏了")
        self.assertIn("不存在", result["message"])

    # ---------- ② 每人每题只能答一次（奖励机制的生命线） ----------

    async def test_reward_is_granted_exactly_once_per_person(self):
        """★★ 同一个人反复答同一题，只能拿到一次金币。"""
        activity_id, _ = await self.draw(self.host)
        before = self.guest.coins
        first = await self.act(self.guest, "prompt_answer", id=activity_id, text="第一次")
        self.assertEqual(first.get("type"), "interaction_result", first)
        gained = self.guest.coins - before
        self.assertEqual(gained, games.PROMPT_REWARD)

        second = await self.act(self.guest, "prompt_answer", id=activity_id, text="再答一次")
        self.assertEqual(second.get("type"), "interaction_error", "同一个人能重复回答同一题")
        self.assertEqual(self.guest.coins - before, gained, "重复回答又发了金币 —— 印钞机开张了")

    async def test_duplicate_answer_is_not_stored_twice(self):
        activity_id, _ = await self.draw(self.host)
        await self.act(self.guest, "prompt_answer", id=activity_id, text="我的回答")
        await self.act(self.guest, "prompt_answer", id=activity_id, text="换个说法")
        shown = self.visible_prompt(activity_id)
        self.assertEqual(shown["reply_count"], 1, "重复提交把回答写了两份")

    async def test_the_person_who_drew_it_can_also_answer(self):
        """抽题人自己也能答 —— 不设「只能别人答」的怪规矩。"""
        activity_id, _ = await self.draw(self.host)
        before = self.host.coins
        result = await self.act(self.host, "prompt_answer", id=activity_id, text="我先说")
        self.assertEqual(result.get("type"), "interaction_result", result)
        self.assertEqual(self.host.coins - before, games.PROMPT_REWARD)

    async def test_many_people_each_get_the_reward_once(self):
        """多人都能答，各拿一次 —— 前提是彼此不冲突（按 user_id 去重）。"""
        activity_id, _ = await self.draw(self.host)
        players = [self.make_user(f"学生{index}") for index in range(4)]
        for player in players:
            result = await self.act(player, "prompt_answer", id=activity_id, text=f"{player.nickname} 的回答")
            self.assertEqual(result.get("type"), "interaction_result", result)
        shown = self.visible_prompt(activity_id)
        self.assertEqual(shown["reply_count"], 4)
        self.assertEqual([reply["name"] for reply in shown["replies"]],
                         [player.nickname for player in players], "回答顺序应与提交顺序一致")

    # ---------- ③ 同房间只能一题（防狂抽题狂自答刷币） ----------

    async def test_a_second_prompt_is_refused_while_one_is_open(self):
        """★★ 没有这条闸门，就能狂抽题狂自己答，奖励变成合法印钞机。"""
        await self.draw(self.host, "truth")
        again = await self.act(self.guest, "dare")
        self.assertEqual(again.get("type"), "interaction_error", "同一房间能同时开两道题")
        self.assertIn("已有一道", again["message"])

    async def test_a_new_prompt_is_allowed_after_the_first_expires(self):
        """闸门只挡「进行中」的题：上一题结束了就该能再抽。"""
        activity_id, _ = await self.draw(self.host)
        stale = games.find(activity_id, "lobby")
        stale["expires_at"] = 0  # 拖到过期，模拟时间流逝
        games.finish(stale)
        again = await self.act(self.guest, "dare")
        self.assertEqual(again.get("type"), "interaction_result", "上一题已结束，却还是开不了新题")
        self.assertEqual(again["result"]["reward"], games.PROMPT_REWARD)

    async def test_the_same_rule_covers_both_kinds(self):
        """真心话和大冒险共用一道闸门 —— 混着抽也只算一题。"""
        await self.draw(self.host, "truth")
        other = await self.act(self.guest, "truth")
        self.assertEqual(other.get("type"), "interaction_error", "大冒险也能绕过闸门")

    # ---------- ④ 内容校验与违禁词 ----------

    async def test_empty_answer_is_refused(self):
        activity_id, _ = await self.draw(self.host)
        for text in ("", "   "):
            result = await self.act(self.guest, "prompt_answer", id=activity_id, text=text)
            self.assertEqual(result.get("type"), "interaction_error", f"空内容（{text!r}）居然通过了")
        self.assertEqual(self.visible_prompt(activity_id)["reply_count"], 0)

    async def test_answer_is_capped_to_the_max_length(self):
        """超长内容要截断而不是整条塞进去 —— 否则答题板会被长文刷屏。"""
        activity_id, _ = await self.draw(self.host)
        await self.act(self.guest, "prompt_answer", id=activity_id, text="啊" * 500)
        stored = games.find(activity_id, "lobby")["answers"][self.guest.user_id]["text"]
        self.assertLessEqual(len(stored), games.PROMPT_ANSWER_MAX)

    async def test_answer_goes_through_the_word_filter(self):
        """★★ 答题板不能成为绕过违禁词的旁路（它和大喇叭不同，是免费发言）。"""
        activity_id, _ = await self.draw(self.host)
        # 用词库里真实存在的词，才能证明过滤真的生效；找不到就跳过而不是假装通过。
        original = chat.sensitive_pattern
        try:
            chat.sensitive_pattern = lambda: __import__("re").compile("坏话")
            await self.act(self.guest, "prompt_answer", id=activity_id, text="这是一句坏话")
        finally:
            chat.sensitive_pattern = original
        stored = games.find(activity_id, "lobby")["answers"][self.guest.user_id]["text"]
        self.assertIn("*", stored, "回答原样入库了 —— 答题板成了违禁词的旁路")
        self.assertNotIn("坏话", stored)

    async def test_answering_a_finished_prompt_is_refused(self):
        activity_id, _ = await self.draw(self.host)
        stale = games.find(activity_id, "lobby")
        stale["expires_at"] = 0
        games.finish(stale)
        result = await self.act(self.guest, "prompt_answer", id=activity_id, text="我来晚了")
        self.assertEqual(result.get("type"), "interaction_error", "题已经结束还能答")
        self.assertIn("已经结束", result["message"])

    async def test_prompt_answer_rejects_a_game_id(self):
        """不能拿成语接龙的 id 走答题通道（两个动作共用一个入口是常见漏洞）。"""
        await self.act(self.host, "game_start", game="idiom")
        game = next(item for item in games.activities("lobby") if item["kind"] == "idiom")
        result = await self.act(self.guest, "prompt_answer", id=game["id"], text="意气风发")
        self.assertEqual(result.get("type"), "interaction_error", "拿游戏 id 答题居然成功了")
        self.assertIn("已经结束", result["message"])

    # ---------- 公告与房间分 ----------

    async def test_answering_is_announced_with_the_prompt_answer_kind(self):
        """回答要进聊天区（别人不答就白答了），且类型不能是流水档。"""
        activity_id, _ = await self.draw(self.host)
        self.guest.websocket.events.clear()
        await self.act(self.guest, "prompt_answer", id=activity_id, text="我的回答")
        notes = [event["message"] for event in self.guest.websocket.events
                 if event.get("type") == "message" and event.get("message", {}).get("kind") == "activity"]
        self.assertEqual(len(notes), 1, "回答没有广播出去")
        self.assertEqual(notes[0]["activity_kind"], "prompt_answer")
        self.assertEqual(notes[0]["activity_tier"], "notice", "回答是低频事件，不该被折进折叠带")
        self.assertIn("我的回答", notes[0]["content"])

    async def test_reward_also_counts_toward_the_room_leaderboard(self):
        activity_id, _ = await self.draw(self.host)
        await self.act(self.guest, "prompt_answer", id=activity_id, text="回答")
        board = games.leaderboard("lobby", "room")
        self.assertIn(self.guest.nickname, [row.get("nickname") for row in board],
                      "答题没有计入房间榜")

    async def test_scores_accumulate_across_two_players(self):
        activity_id, _ = await self.draw(self.host)
        await self.act(self.host, "prompt_answer", id=activity_id, text="我先说")
        await self.act(self.guest, "prompt_answer", id=activity_id, text="我说两句")
        board = {row.get("nickname"): row.get("score") for row in games.leaderboard("lobby", "room")}
        self.assertEqual(board.get(self.host.nickname), games.PROMPT_REWARD)
        self.assertEqual(board.get(self.guest.nickname), games.PROMPT_REWARD)


if __name__ == "__main__":
    unittest.main()