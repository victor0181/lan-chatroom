"""Inventory recycling uses a temporary database and exercises real endpoint code."""
import asyncio
import json
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as chat


class Request:
    def __init__(self, token):
        self.headers = {"X-Session-Token": token}


class Socket:
    def __init__(self):
        self.events = []

    async def send_json(self, data):
        self.events.append(data)


class RecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lan-chat-recycle-")
        self.old_announce_dir = chat.ANNOUNCE_DIR
        self.old_legacy_announcement = chat.LEGACY_ANNOUNCE_FILE
        chat.ANNOUNCE_DIR = Path(self.temp.name) / "公告"
        chat.LEGACY_ANNOUNCE_FILE = Path(self.temp.name) / "没有这个旧公告.txt"
        self.old_db = chat.DB_PATH
        chat.close_db()
        chat.DB_PATH = Path(self.temp.name) / "chatroom.db"
        chat.sessions.clear()
        chat.manager.sessions.clear()
        chat.init_db()
        with closing(chat.get_db()) as db:
            cursor = db.execute("INSERT INTO users(username,password_hash,nickname,created_at) VALUES(?,?,?,?)",
                                ("recycler", "unused", "Recycler", chat.now_text()))
            db.commit()
        self.user = chat.Session("recycle-token", str(cursor.lastrowid), "Recycler", "user", "recycler")
        self.user.inventory = {"gift_rose": 4, "gift_diamond": 2, "badge_heart": 3, "badge_star": 1, "basic_flower": 5}
        self.user.badge_id = "badge_heart"
        self.user.charm = 18
        self.user.websocket = Socket()
        chat.sessions[self.user.token] = self.user
        chat.manager.sessions[self.user.user_id] = self.user
        chat.persist_session(self.user)

    def tearDown(self):
        chat.sessions.clear()
        chat.manager.sessions.clear()
        chat.close_db()
        chat.DB_PATH = self.old_db
        chat.ANNOUNCE_DIR = self.old_announce_dir
        chat.LEGACY_ANNOUNCE_FILE = self.old_legacy_announcement
        self.temp.cleanup()

    async def recycle(self, *pairs, user=None):
        user = user or self.user
        payload = chat.RecycleRequest(items=[{"item_id": key, "quantity": amount} for key, amount in pairs])
        return await chat.recycle_items(payload, Request(user.token))

    def stored(self):
        with closing(chat.get_db()) as db:
            return dict(db.execute("SELECT * FROM users WHERE id=?", (self.user.user_id,)).fetchone())

    async def test_batch_persists_coins_and_inventory_without_other_rewards(self):
        result = await self.recycle(("gift_rose", 2), ("gift_diamond", 1), ("badge_heart", 2))
        self.assertEqual(result["earned"], 42)
        self.assertEqual(self.user.coins, 142)
        self.assertEqual(self.user.inventory["gift_rose"], 2)
        self.assertEqual(self.user.inventory["badge_heart"], 1)
        self.assertEqual(self.user.badge_id, "badge_heart")
        self.assertEqual((self.user.exp, self.user.charm), (0, 18))
        row = self.stored()
        self.assertEqual(row["coins"], 142)
        self.assertEqual(json.loads(row["inventory"]), self.user.inventory)
        events = self.user.websocket.events
        self.assertTrue(any("inventory" in event["user"] for event in events))
        self.assertTrue(any("inventory" not in event["user"] for event in events))

    async def test_selling_last_equipped_badge_updates_badge_and_broadcast(self):
        await self.recycle(("badge_heart", 3))
        self.assertEqual(self.user.badge_id, "")
        self.assertNotIn("badge_heart", self.user.inventory)
        self.assertEqual(self.user.badge_icon(), "⭐")
        self.assertEqual(self.stored()["badge_id"], "")
        self.assertEqual(self.user.websocket.events[-1]["user"]["badge"], "⭐")

    async def test_selling_all_badges_clears_visible_badge(self):
        await self.recycle(("badge_heart", 3), ("badge_star", 1))
        self.assertEqual(self.user.badge_icon(), "")

    async def test_base_gifts_recover_one_coin_each(self):
        # 基础礼物定价 1 金币后，回收价由 max(1, 1 // 2) 保底为 1（详见 recycle_price）。
        result = await self.recycle(("basic_flower", 5))
        self.assertEqual(result["earned"], 5)
        self.assertEqual(self.user.coins, 105)
        self.assertNotIn("basic_flower", self.user.inventory)

    async def test_empty_inventory_stays_empty_after_relogin(self):
        await self.recycle(*list(self.user.inventory.items()))
        restored = chat.Session("other-token", self.user.user_id, "Recycler", "user", "recycler", self.stored())
        self.assertEqual(restored.inventory, {})
        new_user = chat.Session("new-token", "new", "New", "user")
        self.assertEqual(new_user.inventory, chat.STARTER_INVENTORY)

    async def test_invalid_batch_does_not_partially_sell(self):
        for pair in [("gift_diamond", 3), ("unknown", 1), ("pet_food", 1), ("basic_flower", 6)]:
            with self.subTest(pair=pair):
                with self.assertRaises(chat.HTTPException):
                    await self.recycle(("gift_rose", 1), pair)
                self.assertEqual(self.user.coins, 100)
                self.assertEqual(self.user.inventory["gift_rose"], 4)
                self.assertEqual(self.stored()["coins"], 100)

    async def test_duplicate_item_ids_rejected(self):
        with self.assertRaises(chat.HTTPException):
            await self.recycle(("gift_rose", 1), ("gift_rose", 1))
        self.assertEqual(self.user.inventory["gift_rose"], 4)

    async def test_two_requests_cannot_sell_same_last_item(self):
        results = await asyncio.gather(self.recycle(("badge_star", 1)), self.recycle(("badge_star", 1)), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(isinstance(result, chat.HTTPException) for result in results), 1)
        self.assertEqual(self.user.coins, 115)

    async def test_guests_and_missing_tokens_rejected(self):
        guest = chat.Session("guest-token", "guest", "Guest", "guest")
        guest.inventory = {"gift_rose": 1}
        chat.sessions[guest.token] = guest
        with self.assertRaises(chat.HTTPException) as error:
            await self.recycle(("gift_rose", 1), user=guest)
        self.assertEqual(error.exception.status_code, 400)
        payload = chat.RecycleRequest(items=[{"item_id": "gift_rose", "quantity": 1}])
        with self.assertRaises(chat.HTTPException) as error:
            await chat.recycle_items(payload, Request("missing"))
        self.assertEqual(error.exception.status_code, 401)

    async def test_admin_balance_stays_unlimited(self):
        self.user.role = "admin"
        result = await self.recycle(("gift_rose", 1))
        self.assertEqual(self.user.coins, 100)
        self.assertEqual(result["user"]["coins"], "∞")
        self.assertEqual(self.user.inventory["gift_rose"], 3)
        # 文案必须如实：管理员余额是展示用的 ∞，实际不加币。
        # 若这里仍写「获得 N 金币」，提示与余额并列就会显得像是扣错了。
        self.assertIn("管理员", result["message"])
        self.assertNotIn("获得", result["message"])

    async def test_database_failure_restores_session(self):
        inventory = self.user.inventory.copy()
        with patch.object(chat, "persist_session", side_effect=RuntimeError("write failed")):
            with self.assertRaises(RuntimeError):
                await self.recycle(("badge_heart", 3))
        self.assertEqual(self.user.inventory, inventory)
        self.assertEqual((self.user.coins, self.user.badge_id), (100, "badge_heart"))

    async def test_shop_exposes_server_recovery_prices(self):
        prices = {item["id"]: item["recycle_price"] for item in (await chat.shop())["items"]}
        self.assertEqual(prices["gift_diamond"], 12)
        self.assertEqual(prices["badge_heart"], 10)
        # 基础礼物定价 1 金币 → 回收价保底 1，不再是 0
        self.assertEqual(prices["basic_flower"], 1)
        self.assertEqual(prices["basic_kiss"], 1)
        self.assertEqual(prices["basic_applause"], 1)
        self.assertEqual(prices["basic_fireworks"], 1)

    def test_recycle_price_floor_only_lifts_one_coin_items(self):
        """「保底 1 金币」只抬起 1 金币物品；≥2 金币仍严格等于 price // 2，不可回收类型恒 0。"""
        self.assertEqual(chat.recycle_price({"price": 1, "item_type": "gift"}), 1)
        self.assertEqual(chat.recycle_price({"price": 2, "item_type": "gift"}), 1)
        self.assertEqual(chat.recycle_price({"price": 3, "item_type": "gift"}), 1)
        self.assertEqual(chat.recycle_price({"price": 10, "item_type": "gift"}), 5)
        self.assertEqual(chat.recycle_price({"price": 12, "item_type": "badge"}), 6)
        self.assertEqual(chat.recycle_price({"price": 1, "item_type": "pet_food"}), 0)
        self.assertEqual(chat.recycle_price({"price": 99, "item_type": "avatar"}), 0)

    def test_quantity_requires_positive_integer_and_nonempty_batch(self):
        for quantity in [0, -1, 1.5, "1", True, 1000001]:
            with self.subTest(quantity=quantity), self.assertRaises(ValidationError):
                chat.RecycleItem(item_id="gift_rose", quantity=quantity)
        with self.assertRaises(ValidationError):
            chat.RecycleRequest(items=[])


if __name__ == "__main__":
    unittest.main()
