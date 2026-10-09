"""使用临时数据库回归金币与房间互动，绝不修改教师机现有数据。"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_import_data = tempfile.TemporaryDirectory(prefix="lan-chat-import-")
os.environ["CHAT_DATA_DIR"] = _import_data.name
os.environ["CHAT_ANNOUNCE_DIR"] = os.path.join(_import_data.name, "公告")
import app as chat
import interactions as games


class Socket:
    def __init__(self):
        self.events = []

    async def send_json(self, data):
        self.events.append(data)


class InteractionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lan-chat-test-")
        chat.DB_PATH = Path(self.temp.name) / "chatroom.db"
        chat.manager.sessions.clear()
        chat.sessions.clear()
        chat.init_db()
        games.init_interactions()
        self.host = self.user("host")
        self.friend = self.user("friend")

    def tearDown(self):
        chat.manager.sessions.clear()
        chat.sessions.clear()
        # get_db() 现在按线程复用连接（不再每次新开），必须在删临时目录之前显式关掉：
        # 否则 Windows 上 chatroom.db 还被这个句柄占着，TemporaryDirectory.cleanup()
        # 会抛 [WinError 32] 另一个程序正在使用此文件。
        chat.close_db()
        self.temp.cleanup()

    def user(self, nickname, room="lobby"):
        with closing(chat.get_db()) as db:
            cursor = db.execute("INSERT INTO users(username,password_hash,nickname,created_at) VALUES(?,?,?,?)", (nickname, "unused", nickname, chat.now_text()))
            db.commit()
        user = chat.Session(nickname, str(cursor.lastrowid), nickname, "user", nickname)
        user.websocket = Socket()
        # room 可选：有些玩法是「一个房间同时只能有一个」（如真心话/大冒险），跨玩法连着测时要分房间。
        user.room_id = room
        chat.sessions[user.token] = user
        chat.manager.sessions[user.user_id] = user
        return user

    async def act(self, user, action, **fields):
        user.last_activity = 0
        # 大喇叭有 60 秒独立冷却，测试里必须一并归零，否则只有第一条能发出去。
        user.last_horn = 0
        user.websocket.events.clear()
        await games.handle_action(user, {"action": action, **fields})
        return next(event for event in user.websocket.events if event["type"] in ("interaction_result", "interaction_error"))

    def stored(self, user):
        with closing(chat.get_db()) as db:
            return dict(db.execute("SELECT * FROM users WHERE id=?", (user.user_id,)).fetchone())

    async def test_lottery_and_wheel_do_not_earn_chat_income(self):
        with patch.object(games.secrets, "choice", return_value=0):
            result = await self.act(self.host, "lottery")
        self.assertEqual(result["result"]["index"], 0)
        self.assertEqual(self.host.coins, 95)
        with patch.object(games.secrets, "choice", return_value=5):
            await self.act(self.host, "wheel")
        self.assertEqual(self.host.coins, 85)
        # 转盘用翻倍奖池，钻石档由 ×1 变 ×2。
        self.assertEqual(self.host.inventory["gift_diamond"], 2)
        self.assertEqual(self.host.exp, 0)
        self.assertEqual(self.stored(self.host)["coins"], 85)

    def prize_ev(self, prizes):
        """奖池期望收益：金币按面值，道具按**回收价 × 数量**折算。

        ★ 为什么道具不能按 0 算：道具能拿去 /api/shop/recycle 换成金币，
          对玩家来说就是钱。只算 coins 档会得出「4.50 < 5，安全」的假结论
          （真实 5.20 > 5，等于每次净赚 0.2 金币，可以无限刷）。
        """
        catalog = chat.item_catalog()
        weight = sum(prize["weight"] for prize in prizes)
        total = 0
        for prize in prizes:
            if "coins" in prize:
                total += prize["coins"] * prize["weight"]
            else:
                total += chat.recycle_price(catalog[prize["item"]]) * prize["count"] * prize["weight"]
        return total / weight

    async def test_wheel_prize_pool_is_doubled_but_still_a_sink(self):
        # 转盘与抽奖共用同一奖池时「贵一倍、奖品一样」，玩家没有理由选转盘。
        # 现在两套池子：转盘金额翻倍（20 金币档 → 40 金币），期望仍低于成本，不印钱。
        with patch.object(games.secrets, "choice", return_value=3):
            lottery = await self.act(self.host, "lottery")
        self.assertEqual(lottery["result"]["prize"], "20 金币")
        self.assertEqual(self.host.coins, 115)
        with patch.object(games.secrets, "choice", return_value=3):
            wheel = await self.act(self.host, "wheel")
        self.assertEqual(wheel["result"]["prize"], "40 金币")
        self.assertEqual(self.host.coins, 145)
        # ★ 两套池子的期望**必须把道具按回收价折算**（见 prize_ev），且都要低于各自成本。
        lottery_ev = self.prize_ev(games.PRIZES)
        wheel_ev = self.prize_ev(games.WHEEL_PRIZES)
        self.assertLess(lottery_ev, 5, f"抽奖成了正期望（EV {lottery_ev}），可以无限刷币")
        self.assertLess(wheel_ev, 10, f"转盘成了正期望（EV {wheel_ev}），可以无限刷币")
        self.assertAlmostEqual(wheel_ev, lottery_ev * games.WHEEL_TIMES, places=6)
        # 权重合计必须还是 100（概率文案按 weight 百分比下发到前端）。
        self.assertEqual(sum(prize["weight"] for prize in games.PRIZES), 100)

    async def test_admin_spends_through_the_unlimited_coin_gate(self):
        """管理员余额是展示用的 ∞ —— 花金币的玩法都要走 can_spend_coins / spend_coins。

        直接比 session.coins 会出现「界面写着 ∞，却提示需要 5 金币」；
        直接 -= 会把管理员余额扣成负数（控制台的成员台账照库内真值显示）。
        """
        self.host.role = "admin"
        self.host.coins = 0
        self.assertEqual(chat.coins_display(self.host), "∞")

        with patch.object(games.secrets, "choice", return_value=0):
            lottery = await self.act(self.host, "lottery")
        self.assertEqual(lottery["type"], "interaction_result")
        self.assertEqual(self.host.coins, 0)          # 不扣币、也不被写成负数

        redpack = await self.act(self.host, "redpack_create", total=20, count=2)
        self.assertEqual(redpack["type"], "interaction_result")
        self.assertEqual(self.host.coins, 0)

        started = await self.act(self.friend, "prediction_start")
        self.assertEqual(started["type"], "interaction_result")
        # prediction_start 的回执里没有 id（前端靠活动列表刷新），所以从活动表里取。
        prediction = next(a for a in games.activities("lobby") if a["kind"] == "prediction")
        vote = await self.act(self.host, "prediction_vote", id=prediction["id"], choice="单数")
        self.assertEqual(vote["type"], "interaction_result")
        self.assertEqual(self.host.coins, 0)

        # 闸门不是「总是放行」：普通用户余额不够照样被拒，且一分钱不扣。
        self.friend.coins = 0
        blocked = await self.act(self.friend, "lottery")
        self.assertEqual(blocked["type"], "interaction_error")
        self.assertIn("需要 5 金币", blocked["message"])
        self.assertEqual(self.friend.coins, 0)

    async def test_guest_cannot_start_prediction(self):
        """竞猜是金币玩法，游客不能通过发起动作绕过权限闸门。"""
        guest = chat.Session("guest-token", "guest-1", "游客", "guest", "游客")
        guest.websocket = Socket()
        guest.room_id = "lobby"
        result = await self.act(guest, "prediction_start")
        self.assertEqual(result["type"], "interaction_error")
        self.assertIn("游客不能使用金币功能", result["message"])
        self.assertFalse(any(a["kind"] == "prediction" for a in games.activities("lobby")))

    async def test_number_game_allows_only_one_guess_per_person(self):
        """小游戏答题必须「每人每轮一次」。

        少了这道闸门就是零成本印钞机：答错会提示「大了 / 小了」，一个人用二分法
        7 次必中，而这条路既不花金币也不要求注册，答对就是 +10。
        """
        with patch.object(games.secrets, "randbelow", return_value=41):
            started = await self.act(self.host, "game_start", game="number")
        self.assertEqual(started["type"], "interaction_result")
        activity = next(a for a in games.activities("lobby") if a["kind"] == "number")
        self.assertEqual(activity["answer"], 42)      # 钉住答案，避开 1% 的随机命中

        first = await self.act(self.host, "game_answer", id=activity["id"], answer="1")
        self.assertEqual(first["type"], "interaction_result")
        self.assertEqual(first["result"]["hint"], "小了，再大一点")

        again = await self.act(self.host, "game_answer", id=activity["id"], answer="99")
        self.assertEqual(again["type"], "interaction_error")
        self.assertIn("每人每轮只能猜一次", again["message"])

        # 换个人还能猜 —— 锁的是「同一人反复猜」，不是把整轮锁死。
        other = await self.act(self.friend, "game_answer", id=activity["id"], answer="99")
        self.assertEqual(other["type"], "interaction_result")

        # 「谁已经猜过」是服务端记账，不该随活动下发到前端。
        self.assertNotIn("answered", games.visible(games.find(activity["id"], "lobby")))

    async def test_wealth_leaderboard_survives_an_online_admin(self):
        """管理员在线上时，public() 里的 coins 是字符串 "∞"。

        榜单直接拿它排序会抛 TypeError（bad operand type for unary -: 'str'），
        表现是「只要有管理员在线，谁点开财富榜都是 500」。
        """
        self.host.role = "admin"
        self.host.coins = 7
        rows = games.leaderboard("lobby", "wealth")
        self.assertTrue(rows)
        admin_row = next(row for row in rows if row["id"] == self.host.user_id)
        self.assertEqual(admin_row["score"], 7)       # 台账取库内真值，不是 "∞"
        self.assertTrue(all(isinstance(row["score"], (int, float)) for row in rows))
        self.assertTrue(json.dumps(rows, allow_nan=False))   # 能直接进 JSON（没有 inf/nan）

    async def test_dice_action_is_removed(self):
        # 「摇骰子」已下线：零消耗零产出、且与竞猜的开奖骰子重复，保留一个就少一个维护面。
        # 这条断言锁死删除结果，防止后续误加回来。
        result = await self.act(self.host, "dice")
        self.assertEqual(result["type"], "interaction_error")
        self.assertEqual(self.host.coins, 100)

    async def test_redpacket_conservation_duplicate_claim_and_room_scope(self):
        start = await self.act(self.host, "redpack_create", total=20, count=3)
        key = start["result"]["id"]
        activity = games.find(key, "lobby")
        self.assertEqual(sum(activity["remaining"]), 20)
        self.assertTrue(all(value >= 1 for value in activity["remaining"]))
        self.assertEqual(self.host.coins, 80)
        claimed = await self.act(self.friend, "redpack_claim", id=key)
        self.assertEqual(self.friend.coins, 100 + claimed["result"]["amount"])
        balance = self.friend.coins
        duplicate = await self.act(self.friend, "redpack_claim", id=key)
        self.assertEqual(duplicate["type"], "interaction_error")
        self.assertEqual(self.friend.coins, balance)
        self.friend.room_id = "music"
        wrong_room = await self.act(self.friend, "redpack_claim", id=key)
        self.assertEqual(wrong_room["type"], "interaction_error")
        activity = games.find(key, "lobby")
        leftover = sum(activity["remaining"])
        games.finish(activity)
        self.assertEqual(self.host.coins, 80 + leftover)
        self.assertEqual(self.host.coins + self.friend.coins, 200)
        games.finish(activity)
        self.assertEqual(self.host.coins + self.friend.coins, 200)

    async def test_offline_refund_and_restart_persistence(self):
        result = await self.act(self.host, "redpack_create", total=30, count=3)
        key = result["result"]["id"]
        chat.manager.sessions.pop(self.host.user_id)
        chat.sessions.pop(self.host.token)
        games.init_interactions()
        activity = games.find(key, "lobby")
        games.finish(activity)
        self.assertEqual(self.stored(self.host)["coins"], 100)
        games.finish(games.find(key, "lobby"))
        self.assertEqual(self.stored(self.host)["coins"], 100)

    async def test_prediction_votes_and_single_settlement(self):
        await self.act(self.host, "prediction_start")
        activity = games.activities("lobby")[0]
        await self.act(self.host, "prediction_vote", id=activity["id"], choice="单数")
        await self.act(self.friend, "prediction_vote", id=activity["id"], choice="双数")
        duplicate = await self.act(self.friend, "prediction_vote", id=activity["id"], choice="单数")
        self.assertEqual(duplicate["type"], "interaction_error")
        with patch.object(games.secrets, "randbelow", return_value=2):
            games.finish(games.find(activity["id"], "lobby"))
        self.assertEqual(self.host.coins, 105)
        self.assertEqual(self.friend.coins, 95)
        games.finish(games.find(activity["id"], "lobby"))
        self.assertEqual(self.host.coins, 105)

    async def test_games_secret_answers_rewards_and_idiom_rules(self):
        await self.act(self.host, "game_start", game="number")
        activity = games.activities("lobby")[0]
        self.assertNotIn("answer", games.visible(activity))
        await self.act(self.friend, "game_answer", id=activity["id"], answer=str(activity["answer"]))
        self.assertEqual(self.friend.coins, 110)
        second = await self.act(self.host, "game_answer", id=activity["id"], answer=str(activity["answer"]))
        self.assertEqual(second["type"], "interaction_error")
        await self.act(self.host, "game_start", game="song")
        song = games.activities("lobby")[0]
        self.assertNotIn("answer", games.visible(song))
        await self.act(self.friend, "game_answer", id=song["id"], answer=song["answer"])
        self.assertEqual(self.friend.coins, 120)
        await self.act(self.host, "game_start", game="idiom")
        idiom = games.activities("lobby")[0]
        await self.act(self.host, "game_answer", id=idiom["id"], answer="意气风发")
        self.assertEqual(self.host.coins, 102)
        repeated_user = await self.act(self.host, "game_answer", id=idiom["id"], answer="发扬光大")
        self.assertEqual(repeated_user["type"], "interaction_error")
        await self.act(self.friend, "game_answer", id=idiom["id"], answer="发扬光大")
        self.assertEqual(self.friend.coins, 122)
        ranks = games.leaderboard("lobby", "room")
        self.assertEqual(ranks[0]["id"], self.friend.user_id)
        self.assertEqual(ranks[0]["score"], 22)
        self.assertEqual(games.leaderboard("music", "room")[0]["score"], 0)

    async def test_truth_dare_and_all_rankings(self):
        # ⚠️ 两种玩法必须**换房间跑**：同一房间同时只能有一道真心话/大冒险
        # （防狂抽题狂自答刷金币的闸门），连着抽第二个必然被拒。这是产品行为，不是缺陷。
        rooms = {"truth": "lobby", "dare": "games"}
        for action in ("truth", "dare"):
            user = self.host
            if rooms[action] != user.room_id:
                user = self.user(f"抽{action}", room=rooms[action])
            result = await self.act(user, action)
            self.assertTrue(result["result"]["prompt"], result)
        self.assertEqual(self.host.coins, 100)
        self.friend.charm = 5
        chat.persist_session(self.friend)
        chat.save_message(self.host, "hello", text_style="classic", text_effect="rainbow")
        self.assertEqual(games.leaderboard("lobby", "active")[0]["score"], 1)
        self.assertEqual(games.leaderboard("lobby", "charm")[0]["id"], self.friend.user_id)
        self.assertEqual(games.leaderboard("lobby", "wealth")[0]["id"], self.host.user_id)
        with closing(chat.get_db()) as db:
            row = db.execute("SELECT * FROM messages WHERE kind='text'").fetchone()
        self.assertEqual(row["text_style"], "classic")
        self.assertEqual(row["text_effect"], "rainbow")

    async def test_invalid_values_do_not_take_coins(self):
        for total, count in ((1, 2), (10, 0), (1000, 2), (-1, 2), ("bad", 2), (True, 1)):
            result = await self.act(self.host, "redpack_create", total=total, count=count)
            self.assertEqual(result["type"], "interaction_error")
            self.assertEqual(self.host.coins, 100)
        self.host.coins = 0
        result = await self.act(self.host, "wheel")
        self.assertEqual(result["type"], "interaction_error")
        self.assertEqual(self.host.coins, 0)

    async def test_expiry_task_refunds_only_once(self):
        result = await self.act(self.host, "redpack_create", total=10, count=2)
        activity = games.find(result["result"]["id"], "lobby")
        activity["expires_at"] = 0
        games.store(activity)
        await games.expire_rooms()
        self.assertEqual(self.host.coins, 100)
        await games.expire_rooms()
        self.assertEqual(self.host.coins, 100)

    async def test_redpack_claim_order_is_randomized(self):
        """领取随机摘一份，不再固定 pop() 末尾：否则首个生成的小份总被先抢的人领走。"""
        start = await self.act(self.host, "redpack_create", total=20, count=3)
        key = start["result"]["id"]
        activity = games.find(key, "lobby")
        activity["remaining"] = [1, 5, 14]
        games.store(activity)
        with patch.object(games.secrets, "randbelow", return_value=1):
            claimed = await self.act(self.friend, "redpack_claim", id=key)
        self.assertEqual(claimed["result"]["amount"], 5)
        self.assertEqual(games.find(key, "lobby")["remaining"], [1, 14])

    async def test_horn_costs_coins_and_broadcasts_globally(self):
        """大喇叭是纯金币出口：扣费、跨房间广播一条 horn 供前端滚动，并在原房间聊天记录里留痕。"""
        seen = []

        async def spy(payload, room_id=None):
            seen.append((payload, room_id))

        with patch.object(chat.manager, "broadcast", spy):
            result = await self.act(self.host, "horn", content="大家好，我是小明")
        self.assertEqual(result["type"], "interaction_result")
        self.assertEqual(result["result"]["cost"], games.HORN_COST)
        self.assertEqual(result["result"]["content"], "大家好，我是小明")
        self.assertEqual(self.host.coins, 100 - games.HORN_COST)
        self.assertEqual(self.stored(self.host)["coins"], 100 - games.HORN_COST)
        horns = [(payload, room) for payload, room in seen if payload.get("type") == "horn"]
        self.assertEqual(len(horns), 1)
        payload, room = horns[0]
        self.assertIsNone(room)
        self.assertEqual(payload["nickname"], "host")
        self.assertEqual(payload["content"], "大家好，我是小明")
        self.assertEqual(payload["sender_id"], self.host.user_id)
        with closing(chat.get_db()) as db:
            row = db.execute("SELECT content, kind FROM messages WHERE kind='activity'").fetchone()
        self.assertIn("大家好，我是小明", row["content"])

    async def test_horn_reaches_other_rooms_without_broadcasting_chat_history(self):
        self.friend.room_id = "games"
        await self.act(self.host, "horn", content="跨房间喊话")
        horns = [event for event in self.friend.websocket.events if event["type"] == "horn"]
        self.assertEqual(len(horns), 1)
        self.assertEqual(horns[0]["content"], "跨房间喊话")
        self.assertFalse(any(event["type"] == "message" for event in self.friend.websocket.events))

    async def test_horn_cooldown_blocks_repeat_without_charging(self):
        """冷却期内重复喊话必须被挡，且不能白扣一次金币（失败不收费）。"""
        self.host.last_activity = 0
        self.host.last_horn = 0
        await games.handle_action(self.host, {"action": "horn", "content": "第一条"})
        self.assertEqual(self.host.coins, 90)
        self.host.last_activity = 0
        self.host.websocket.events.clear()
        await games.handle_action(self.host, {"action": "horn", "content": "第二条"})
        error = next(event for event in self.host.websocket.events if event["type"] == "interaction_error")
        self.assertIn("秒", error["message"])
        self.assertEqual(self.host.coins, 90)
        # 冷却过后能正常再喊一次。
        self.host.last_horn = 0
        self.host.last_activity = 0
        self.host.websocket.events.clear()
        await games.handle_action(self.host, {"action": "horn", "content": "第三条"})
        self.assertEqual(self.host.coins, 80)

    async def test_horn_rejects_bad_input_without_charging(self):
        """空内容、违规词、金币不足、游客 —— 全部拒绝且一分钱不扣。"""
        for content in ("", "   "):
            result = await self.act(self.host, "horn", content=content)
            self.assertEqual(result["type"], "interaction_error")
            self.assertEqual(self.host.coins, 100)
        with patch.object(chat, "moderate_content", return_value=("***", True)):
            result = await self.act(self.host, "horn", content="违规内容")
        self.assertEqual(result["type"], "interaction_error")
        self.assertIn("违规词", result["message"])
        self.assertEqual(self.host.coins, 100)
        self.host.coins = games.HORN_COST - 1
        result = await self.act(self.host, "horn", content="没钱也要喊")
        self.assertEqual(result["type"], "interaction_error")
        self.assertEqual(self.host.coins, games.HORN_COST - 1)
        self.host.coins = 100
        self.host.role = "guest"
        result = await self.act(self.host, "horn", content="游客喊话")
        self.assertEqual(result["type"], "interaction_error")
        self.assertEqual(self.host.coins, 100)

    async def test_horn_cooldown_message_takes_priority_over_insufficient_coins(self):
        """冷却中即使余额不足，也必须提示等待，而不是误导成金币不足。"""
        self.host.coins = games.HORN_COST - 1
        self.host.last_horn = games.time.time() - 1
        self.host.websocket.events.clear()
        await games.handle_action(self.host, {"action": "horn", "content": "冷却中的喊话"})
        error = next(event for event in self.host.websocket.events if event["type"] == "interaction_error")
        self.assertEqual(error["action"], "horn")
        self.assertIn("秒", error["message"])
        self.assertIn("再等", error["message"])
        self.assertNotIn("需要 10 金币", error["message"])
        self.assertEqual(self.host.coins, games.HORN_COST - 1)

    async def test_horn_config_is_published_to_frontend(self):
        """价格与冷却由服务端下发，避免前端硬写一份「界面在说谎」。"""
        config = games.snapshot("lobby")["horn"]
        self.assertEqual(config["cost"], games.HORN_COST)
        self.assertEqual(config["cooldown"], games.HORN_COOLDOWN)
        self.assertEqual(config["max_length"], games.HORN_MAX_LENGTH)
        self.assertGreater(games.HORN_COST, 0)

    async def test_prediction_announcement_when_nobody_wins(self):
        """无人猜中时不能播报“猜中者每人获得 10 金币”，否则学生会以为金币丢了。"""
        await self.act(self.host, "prediction_start")
        activity = games.activities("lobby")[0]
        activity["expires_at"] = 0
        games.store(activity)
        seen = []

        async def spy(payload, room_id=None):
            seen.append(payload)

        with patch.object(chat.manager, "broadcast", spy):
            await games.expire_rooms()
        notes = [item.get("message", "") for item in seen if item.get("type") == "system"]
        self.assertTrue(any("无人猜中" in note for note in notes), notes)
        self.assertFalse(any("猜中者每人获得" in note for note in notes), notes)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        _import_data.cleanup()
