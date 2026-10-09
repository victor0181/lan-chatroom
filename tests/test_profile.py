"""个人资料功能测试：改资料按处计费、查看他人资料收费、付费字段不泄露。

这里直接调用端点的协程函数（FastAPI 的 @app.post 原样返回函数），
配一个只提供 headers 的最小 Request 替身 —— 不需要起真实服务，
但走的是真正的计费/落库代码路径。

每次调用用 asyncio.run() 起一个干净的事件循环：端点在测试里不会产生后台任务
（manager.sessions 是空的，broadcast 直接返回），所以不存在收尾卡死的风险。
"""
import asyncio
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as chat

BOM = b"\xef\xbb\xbf"
TEST_WORDS = "傻逼\r\n傻b\r\nfuck\r\n他妈\r\nsb\r\n我操\r\n"


class FakeRequest:
    """端点只用到 request.headers，测试里给个最小替身。"""

    def __init__(self, token: str | None = None):
        self.headers = {"X-Session-Token": token} if token else {}


def run(coro):
    """每次都用全新的循环跑完就关：不留残任务，也不和别的用例串状态。"""
    return asyncio.run(coro)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lan-chat-profile-")
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
        chat.WORDS_CHECK_INTERVAL = 0.0
        chat.reset_wordlist_cache()

        chat.sessions.clear()
        chat.manager.sessions.clear()
        chat.init_db()

    def tearDown(self):
        chat.WORDS_FILE = self.original_words_file
        chat.WORDS_CHECK_INTERVAL = self.original_interval
        chat.reset_wordlist_cache()
        chat.sessions.clear()
        chat.manager.sessions.clear()
        # 复用连接必须先真关掉，临时目录才删得掉（否则 WinError 32）。
        chat.close_db()
        chat.ANNOUNCE_DIR = self.original_announce_dir
        chat.LEGACY_ANNOUNCE_FILE = self.original_legacy
        self.temp.cleanup()

    # ------------------------------------------------------------ 工具
    def make_user(self, user_id="1", username="tester", nickname="测试用户",
                  role="user", coins=100, online=True, **extra):
        """建一个真实账号 + 会话（可选择「在线」，用于重名/查看资料这类判据）。"""
        with closing(chat.get_db()) as db:
            db.execute(
                "INSERT OR REPLACE INTO users (id, username, password_hash, nickname, role, coins, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, username, chat.hash_password("old-pw-123"), nickname, role, coins, chat.now_text()),
            )
            db.commit()
            row = dict(db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
        session = chat.Session(f"token-{user_id}", user_id, nickname, role, username, row)
        session.coins = coins
        # 只有 websocket 非空才会出现在在线名单里 / 参与「在线重名」判定。
        session.websocket = object() if online else None
        chat.sessions[session.token] = session
        return session

    def edit(self, session, **fields):
        return run(chat.profile(chat.ProfileUpdate(**fields), FakeRequest(session.token)))

    def view(self, session, target_id):
        return run(chat.view_profile(chat.ProfileViewRequest(user_id=target_id), FakeRequest(session.token)))

    def assert_http_error(self, status, detail_part, func, *args, **kwargs):
        try:
            run(func(*args, **kwargs))
        except chat.HTTPException as error:
            self.assertEqual(error.status_code, status, f"状态码不对：{error.status_code} {error.detail}")
            if detail_part:
                self.assertIn(detail_part, str(error.detail))
            return error
        self.fail(f"本该抛 {status}，结果一路通过了")

    def db_row(self, user_id="1"):
        with closing(chat.get_db()) as db:
            return dict(db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())

    # ------------------------------------------------------------ 坏数据的容错
    def test_broken_inventory_never_blocks_login(self):
        """inventory 里出现坏值时：只丢坏的那一条，绝不能让这个账号连登录都起不来。

        ★ `int("abc")` 抛的是 ValueError，早先只兜 TypeError / json.JSONDecodeError ——
          异常一路逃到登录路径，这个账号**永远登不上**（每次都 500），而他什么都没做错。
        """
        broken = chat.Session("t1", "1", "甲", "user", "tester",
                              {"inventory": '{"basic_flower": "abc", "gift_rose": 2}'})
        self.assertEqual(broken.inventory, {"gift_rose": 2}, "坏的该丢掉、好的该留下")

        # 整栏不是 JSON：照样能开会话（空物品栏 + 补发初始礼物）。
        for raw in ("{不是 JSON", "oops"):
            session = chat.Session("t2", "2", "乙", "user", "tester", {"inventory": raw})
            self.assertEqual(session.inventory, dict(chat.STARTER_INVENTORY))

        # 是 dict 但值全是坏的：不补发初始礼物（说明发过了），但也不能崩。
        empty = chat.Session("t3", "3", "丙", "user", "tester", {"inventory": '{"basic_flower": null}'})
        self.assertEqual(empty.inventory, {})
        empty2 = chat.Session("t3", "3", "丙", "user", "tester", {"inventory": '{"basic_flower": {"o": 1}}'})
        self.assertEqual(empty2.inventory, {})

        # 真实登录路径：从库里读出坏行也必须活下来。
        with closing(chat.get_db()) as db:
            db.execute("UPDATE users SET inventory = ? WHERE id = ?", ('{"数量": "三"}', "1"))
            db.commit()
            row = dict(db.execute("SELECT * FROM users WHERE id = ?", ("1",)).fetchone())
        session = chat.Session("t4", "1", "甲", "user", "tester", row)
        self.assertEqual(session.inventory, {})

    # ------------------------------------------------------------ 隐私边界
    def test_paid_fields_never_leak_into_public(self):
        """public() 会广播给全房间 —— 付费字段绝不能混进去，否则 1 金币查看形同虚设。

        个性签名是唯一的例外：它要显示在在线列表里给所有人看，本来就该跟着广播。
        这条判据守的是真实姓名 / 班级 / 账号名。
        """
        session = chat.Session("t", "1", "甲", "user", "tester")
        session.real_name, session.class_name, session.bio = "张三", "八年级3班", "好好学习"
        public = session.public()
        for field in ("real_name", "class_name", "username"):
            self.assertNotIn(field, public, f"{field} 混进了公开广播")
        # 反向对照：签名确实在广播里 —— 少了它，在线列表上就是一片空白且不报错。
        self.assertEqual(public["bio"], "好好学习", "签名没进 public()，在线列表就看不到它")
        mine = session.self_public()
        self.assertEqual(mine["real_name"], "张三")
        self.assertEqual(mine["class_name"], "八年级3班")
        self.assertEqual(mine["bio"], "好好学习")
        self.assertEqual(mine["username"], "tester")

    # ------------------------------------------------------------ 改资料计费
    def test_edit_charges_five_per_changed_field(self):
        session = self.make_user()
        result = self.edit(session, nickname="新名字", class_name="八年级3班")
        self.assertEqual(result["charged"], 2 * chat.PROFILE_EDIT_COST)
        self.assertEqual(session.coins, 100 - 2 * chat.PROFILE_EDIT_COST)
        self.assertEqual(sorted(result["changes"]), ["class_name", "nickname"])
        self.assertIn("昵称", result["message"])
        self.assertIn("班级", result["message"])
        self.assertIn("扣除 10 金币", result["message"])
        # 真的要落库，不能只改内存
        row = self.db_row()
        self.assertEqual(row["nickname"], "新名字")
        self.assertEqual(row["class_name"], "八年级3班")
        self.assertEqual(row["coins"], 90)

    def test_unchanged_fields_are_free(self):
        """传了但和现在一模一样 → 不算改动，不收费；一处都没变就整单拒绝。"""
        session = self.make_user()
        self.assert_http_error(400, "没有发现要修改的内容", lambda: self.edit(
            session, nickname=session.nickname, real_name=""))
        self.assertEqual(session.coins, 100)
        self.assertEqual(self.db_row()["coins"], 100)

    def test_only_changed_fields_are_charged(self):
        session = self.make_user()
        result = self.edit(session, nickname=session.nickname, real_name="李四")
        self.assertEqual(result["changes"], ["real_name"])
        self.assertEqual(session.coins, 100 - chat.PROFILE_EDIT_COST)

    def test_avatar_change_costs_five(self):
        session = self.make_user()
        result = self.edit(session, avatar="😎")
        self.assertEqual(result["charged"], chat.PROFILE_EDIT_COST)
        self.assertEqual(session.avatar, "😎")
        self.assertEqual(result["user"]["avatar"], "😎")

    def test_avatar_must_be_owned(self):
        session = self.make_user()
        self.assert_http_error(400, "头像", lambda: self.edit(session, avatar="/avatars/没买过的.png"))
        self.assertEqual(session.coins, 100)

    def test_pet_adoption_requires_level_five_for_registered_accounts(self):
        for role, level in (("user", 1), ("user", 4), ("admin", 4), ("guest", 5)):
            with self.subTest(role=role, level=level):
                session = self.make_user(role=role)
                session.level = level
                self.assert_http_error(403, "宠物", chat.adopt_pet,
                                       chat.PetAdoptRequest(pet_type="chicken"), FakeRequest(session.token))
                self.assertEqual(session.pet_type, "")
                self.assertEqual(session.coins, 100)

    def test_existing_pet_is_preserved_while_level_gate_blocks_actions(self):
        session = self.make_user()
        session.level, session.exp = 4, 150
        session.pet_type = "cat"
        session.pet_hunger, session.pet_thirst, session.pet_cleanliness = 30, 40, 50
        session.pet_updated_at = time.time()
        session.inventory.update({"pet_food": 1, "pet_water": 1, "pet_clean": 1})
        chat.persist_session(session)
        before = self.db_row()
        self.assert_http_error(403, "Lv.5", chat.adopt_pet,
                               chat.PetAdoptRequest(pet_type="dog"), FakeRequest(session.token))
        for action in ("feed", "water", "clean"):
            self.assert_http_error(403, "Lv.5", chat.pet_action,
                                   chat.PetActionRequest(action=action), FakeRequest(session.token))
        self.assertEqual(self.db_row(), before)
        session.level, session.exp = 5, 200
        result = run(chat.adopt_pet(chat.PetAdoptRequest(pet_type="cat"), FakeRequest(session.token)))
        self.assertEqual(result["user"]["pet"]["type"], "cat")
        self.assertEqual(session.coins, before["coins"])
        self.assertLessEqual(result["user"]["pet"]["hunger"], 30)

    def test_level_five_unlocks_free_adoption_and_pet_care(self):
        session = self.make_user()
        session.level, session.exp = 5, 200
        result = run(chat.adopt_pet(chat.PetAdoptRequest(pet_type="chicken"), FakeRequest(session.token)))
        self.assertEqual(result["user"]["pet"]["type"], "chicken")
        self.assertEqual(result["user"]["pet_min_level"], 5)
        self.assertEqual(session.coins, 100)
        for action, item in (("feed", "pet_food"), ("water", "pet_water"), ("clean", "pet_clean")):
            session.inventory[item] = 1
            result = run(chat.pet_action(chat.PetActionRequest(action=action), FakeRequest(session.token)))
            self.assertEqual(result["user"]["inventory"][item], 0)
        result = run(chat.adopt_pet(chat.PetAdoptRequest(pet_type="dog"), FakeRequest(session.token)))
        self.assertEqual(result["user"]["pet"]["type"], "dog")
        self.assertEqual(session.coins, 100 - chat.PET_CHANGE_COST)

    def test_chat_level_up_unlocks_pet_without_relogin(self):
        session = self.make_user()
        session.level, session.exp = 4, 198
        message = chat.save_message(session, "升级到五级")
        self.assertEqual(message["sender_level"], 5)
        result = run(chat.adopt_pet(chat.PetAdoptRequest(pet_type="cat"), FakeRequest(session.token)))
        self.assertEqual(result["user"]["pet"]["type"], "cat")

    def test_history_uses_current_avatar_even_after_sender_logs_out(self):
        sender = self.make_user()
        viewer = self.make_user(user_id="2", username="viewer", nickname="看的人")
        public_message = chat.save_message(sender, "换头像之前的公聊")
        private_message = chat.save_message(sender, "换头像之前的私聊", "private",
                                            room_id="private", recipient_id=viewer.user_id)
        self.edit(sender, avatar="😎")
        self.edit(sender, avatar="🌟")
        chat.sessions.pop(sender.token)
        for room_id, private_with, expected_id in (
            ("lobby", None, public_message["id"]),
            ("private", sender.user_id, private_message["id"]),
        ):
            with self.subTest(private_with=private_with):
                result = run(chat.messages(FakeRequest(viewer.token), room_id=room_id,
                                           private_with=private_with, limit=100))["messages"]
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["id"], expected_id)
                self.assertEqual(result[0]["sender_avatar"], "🌟")
                self.assertNotIn("current_sender_avatar", result[0])

    def test_history_avatar_does_not_change_other_senders_or_guests(self):
        sender = self.make_user()
        viewer = self.make_user(user_id="2", username="viewer", nickname="看的人")
        guest = chat.Session("guest-token", "guest-history", "游客", "guest")
        guest.avatar = "🌻"
        original = chat.save_message(sender, "第一人的消息")
        other = chat.save_message(viewer, "第二人的消息")
        guest_message = chat.save_message(guest, "游客的消息")
        self.edit(sender, avatar="😎")
        result = run(chat.messages(FakeRequest(viewer.token), room_id="lobby",
                                   private_with=None, limit=100))["messages"]
        self.assertEqual([message["id"] for message in result],
                         [original["id"], other["id"], guest_message["id"]])
        self.assertEqual([message["sender_avatar"] for message in result],
                         ["😎", other["sender_avatar"], "🌻"])

    # ------------------------------------------------------------ 原子性
    def test_insufficient_coins_rejects_the_whole_order(self):
        """金币不够 → 整单拒绝：既不扣钱，也不许改一半。"""
        session = self.make_user(coins=chat.PROFILE_EDIT_COST - 1)
        self.assert_http_error(402, "金币", lambda: self.edit(
            session, nickname="改不了", class_name="八年级3班"))
        self.assertEqual(session.coins, chat.PROFILE_EDIT_COST - 1)
        self.assertEqual(session.nickname, "测试用户")
        self.assertEqual(session.class_name, "")
        row = self.db_row()
        self.assertEqual(row["coins"], chat.PROFILE_EDIT_COST - 1)
        self.assertEqual(row["nickname"], "测试用户")
        self.assertEqual(row["class_name"], "")

    def test_admin_edits_are_free(self):
        session = self.make_user(role="admin", nickname="管理员")
        result = self.edit(session, real_name="王老师", class_name="八年级2班")
        self.assertEqual(result["charged"], 0)
        self.assertEqual(session.coins, 100)
        # 页面上不再出现「管理员免费」这类多余文字：message 只报改了哪几处。
        self.assertNotIn("管理员免费", result["message"])
        self.assertNotIn("金币", result["message"])
        self.assertEqual(self.db_row()["real_name"], "王老师")

    def test_guest_cannot_edit_profile(self):
        session = chat.Session("g", "guest-1", "路人", "guest")
        chat.sessions[session.token] = session
        self.assert_http_error(400, "注册", lambda: self.edit(session, nickname="新名字"))

    # ------------------------------------------------------------ 校验
    def test_nickname_is_screened_and_limited(self):
        session = self.make_user()
        self.assert_http_error(400, "不合适的词", lambda: self.edit(session, nickname="傻逼"))
        self.assert_http_error(400, "最多", lambda: self.edit(session, nickname="字" * 21))
        self.assert_http_error(400, "不能为空", lambda: self.edit(session, nickname="   "))
        self.assertEqual(session.coins, 100)

    def test_nickname_must_be_unique_among_online_users(self):
        session = self.make_user()
        self.make_user(user_id="2", username="other", nickname="已经有人叫这名")
        self.assert_http_error(400, "已经有人叫这个名字", lambda: self.edit(session, nickname="已经有人叫这名"))
        # 占了名字的人不在线时不算冲突（在线列表里根本看不到他）
        off = self.make_user(user_id="3", username="offline", nickname="离线的人", online=False)
        result = self.edit(session, nickname="离线的人")
        self.assertEqual(result["changes"], ["nickname"])
        self.assertEqual(off.nickname, "离线的人")

    def test_profile_text_length_limits(self):
        session = self.make_user()
        self.assert_http_error(400, "真实姓名最多", lambda: self.edit(session, real_name="名" * 21))
        self.assert_http_error(400, "班级最多", lambda: self.edit(session, class_name="班" * 21))
        self.assert_http_error(400, "个性签名最多", lambda: self.edit(session, bio="签" * 41))
        self.assertEqual(session.coins, 100)

    def test_class_name_only_accepts_dropdown_options(self):
        """班级改成「年级 + 班级号」下拉后，服务端必须挡住自由文本 —— 否则直连接口照样能把班级写乱。"""
        session = self.make_user()
        for value, expected in (("八年级3班", "八年级3班"), ("七年级", "七年级"), ("10班", "10班")):
            session.class_name = ""
            self.edit(session, class_name=value)
            self.assertEqual(self.db_row()["class_name"], expected, f"输入 {value!r}")
        # 留空 = 不填。要先把班级设成有值，清空才算「改了一处」——
        # 本来就是空的时候再传空，服务端应当拒绝（没有发现要修改的内容）。
        result = self.edit(session, class_name="")
        self.assertEqual(result["changes"], ["class_name"])
        self.assertEqual(self.db_row()["class_name"], "")
        # 自由文本、越界的班级号，一律拒绝
        for bad in ("教务处", "八", "3", "八年级11班", "八年级0班"):
            self.assert_http_error(400, "班级", lambda bad=bad: self.edit(session, class_name=bad))
        # 年级写成了七八九以外，提示语是另一条
        self.assert_http_error(400, "年级只能是", lambda: self.edit(session, class_name="一年级2班"))
        self.assertGreater(session.coins, 0)

    def test_class_name_normalizes_old_writing(self):
        """库里可能存着旧格式的自由文本，重新提交时要归一化成标准写法，而不是报错。"""
        session = self.make_user()
        for value in ("八（3）班", "八(3)班", "8年级3班", "八年级 3 班"):
            session.class_name = ""
            self.edit(session, class_name=value)
            self.assertEqual(self.db_row()["class_name"], "八年级3班", f"输入 {value!r}")

    def test_clearing_a_field_counts_as_a_change(self):
        session = self.make_user()
        self.edit(session, real_name="张三")
        self.assertEqual(self.db_row()["real_name"], "张三")
        result = self.edit(session, real_name="")
        self.assertEqual(result["changes"], ["real_name"])
        self.assertEqual(self.db_row()["real_name"], "")

    # ------------------------------------------------------------ 改密码
    def test_password_change_requires_the_old_password(self):
        session = self.make_user()
        before = self.db_row()["password_hash"]
        self.assert_http_error(400, "原密码", lambda: self.edit(session, new_password="new-pw-456"))
        self.assert_http_error(400, "原密码不对", lambda: self.edit(
            session, old_password="猜的密码", new_password="new-pw-456"))
        self.assertEqual(self.db_row()["password_hash"], before, "原密码错却把密码改了")
        self.assertEqual(session.coins, 100, "校验失败不该扣金币")

    def test_password_change_succeeds_and_costs_five(self):
        session = self.make_user()
        result = self.edit(session, old_password="old-pw-123", new_password="new-pw-456")
        self.assertEqual(result["charged"], chat.PROFILE_EDIT_COST)
        self.assertEqual(session.coins, 100 - chat.PROFILE_EDIT_COST)
        after = self.db_row()["password_hash"]
        self.assertTrue(chat.verify_password("new-pw-456", after))
        self.assertFalse(chat.verify_password("old-pw-123", after))

    def test_reusing_the_same_password_is_rejected(self):
        session = self.make_user()
        self.assert_http_error(400, "一样", lambda: self.edit(
            session, old_password="old-pw-123", new_password="old-pw-123"))
        self.assertEqual(session.coins, 100)

    # ------------------------------------------------------------ 改名与历史消息
    def test_rename_updates_history_signature(self):
        """改了昵称，历史消息里的旧名字一起改掉 —— 否则同一个人显示成两个名字。"""
        session = self.make_user()
        chat.save_message(session, "改名前说的话")
        old_name = self.db_row()["nickname"]
        self.edit(session, nickname="改名后")
        with closing(chat.get_db()) as db:
            names = [row[0] for row in db.execute("SELECT sender_name FROM messages WHERE sender_id = ?", ("1",)).fetchall()]
        self.assertTrue(names, "历史消息不见了")
        self.assertTrue(all(name == "改名后" for name in names), f"历史署名没同步：{names}")
        self.assertNotEqual(old_name, "改名后")

    def test_rename_keeps_sender_id_so_admin_can_still_trace(self):
        session = self.make_user()
        chat.save_message(session, "一句话")
        self.edit(session, nickname="换个名字")
        with closing(chat.get_db()) as db:
            row = db.execute("SELECT sender_id FROM messages").fetchone()
        self.assertEqual(row[0], "1", "改名把 sender_id 也换掉了，管理员就追不到人了")

    # ------------------------------------------------------------ 查看他人资料
    def test_view_costs_one_coin(self):
        viewer = self.make_user(user_id="1", username="viewer", nickname="看的人")
        target = self.make_user(user_id="2", username="target", nickname="被看的人")
        target.real_name, target.class_name, target.bio = "李四", "八年级2班", "我是签名"
        result = self.view(viewer, "2")
        self.assertEqual(result["charged"], chat.PROFILE_VIEW_COST)
        self.assertEqual(viewer.coins, 100 - chat.PROFILE_VIEW_COST)
        self.assertEqual(result["card"]["real_name"], "李四")
        self.assertEqual(result["card"]["class_name"], "八年级2班")
        self.assertEqual(result["card"]["bio"], "我是签名")
        self.assertEqual(self.db_row("1")["coins"], 100 - chat.PROFILE_VIEW_COST)

    def test_viewing_the_same_person_twice_only_charges_once(self):
        viewer = self.make_user(user_id="1", username="viewer", nickname="看的人")
        self.make_user(user_id="2", username="target", nickname="被看的人")
        self.view(viewer, "2")
        again = self.view(viewer, "2")
        self.assertEqual(again["charged"], 0, "同一次登录里看同一个人被扣了两次")
        self.assertEqual(viewer.coins, 100 - chat.PROFILE_VIEW_COST)
        # 别人是另算的
        self.make_user(user_id="3", username="other", nickname="第三个人")
        self.view(viewer, "3")
        self.assertEqual(viewer.coins, 100 - 2 * chat.PROFILE_VIEW_COST)

    def test_view_self_is_free(self):
        viewer = self.make_user()
        result = self.view(viewer, "1")
        self.assertEqual(result["charged"], 0)
        self.assertEqual(viewer.coins, 100)

    def test_admin_view_is_free(self):
        admin = self.make_user(user_id="9", username="admin", nickname="管理员", role="admin")
        self.make_user(user_id="2", username="stu", nickname="学生")
        result = self.view(admin, "2")
        self.assertEqual(result["charged"], 0)
        self.assertEqual(admin.coins, 100)

    def test_admin_sees_the_account_name_but_students_do_not(self):
        viewer = self.make_user(user_id="1", username="viewer", nickname="看的人")
        admin = self.make_user(user_id="9", username="admin", nickname="管理员", role="admin")
        self.make_user(user_id="2", username="stu_zhang", nickname="学生")
        self.assertEqual(self.view(admin, "2")["card"]["username"], "stu_zhang")
        self.assertNotIn("username", self.view(viewer, "2")["card"])

    def test_view_is_blocked_without_coins(self):
        viewer = self.make_user(user_id="1", username="viewer", nickname="穷学生", coins=0)
        self.make_user(user_id="2", username="target", nickname="被看的人")
        self.assert_http_error(402, "金币", lambda: self.view(viewer, "2"))
        self.assertEqual(viewer.coins, 0)

    def test_view_rejects_a_target_that_is_not_online(self):
        """在线列表里点不到的人（已登录但连接断了），不该花 1 金币换一份过期资料。"""
        viewer = self.make_user(user_id="1", username="viewer", nickname="看的人")
        self.make_user(user_id="2", username="offline", nickname="不在线的人", online=False)
        self.assert_http_error(404, "不在线", lambda: self.view(viewer, "2"))
        self.assert_http_error(404, "不在线", lambda: self.view(viewer, "查无此人"))
        self.assertEqual(viewer.coins, 100)

    def test_admin_can_view_an_offline_account_for_free(self):
        """老师要能随时查（哪怕学生已经断开连接）。"""
        admin = self.make_user(user_id="9", username="admin", nickname="管理员", role="admin")
        target = self.make_user(user_id="2", username="offline", nickname="已断线的学生", online=False)
        target.class_name = "八年级1班"
        result = self.view(admin, "2")
        self.assertEqual(result["charged"], 0)
        self.assertEqual(result["card"]["class_name"], "八年级1班")
        self.assertEqual(admin.coins, 100)

    def test_freshly_registered_session_knows_its_join_time(self):
        """注册后直接进来时，会话也得带上「加入时间」。

        真实浏览器里点开资料卡发现这一栏写着「未知」—— 根因是 /api/register 走的
        create_session 没传库里的行。这条用例守住它。
        """
        self.make_user(user_id="7", username="fresh", nickname="新同学")
        chat.sessions.clear()          # 模拟「刚注册完，还没登录过」
        fresh = chat.create_session("7", "fresh", "新同学", "user")
        self.assertTrue(fresh["user"]["created_at"], "注册后直接进入时「加入时间」是空的")
        self.assertEqual(fresh["user"]["created_at"], self.db_row("7")["created_at"])

    def test_guest_session_still_has_no_join_time(self):
        """游客没有数据库记录，不该为了这个字段去查库、更不该报错。"""
        guest = chat.create_session("guest-x", None, "路人", "guest")
        self.assertEqual(guest["user"]["created_at"], "")

    def test_guest_cannot_view_profiles(self):
        guest = chat.Session("g", "guest-1", "路人", "guest")
        chat.sessions[guest.token] = guest
        self.make_user(user_id="2", username="stu", nickname="学生")
        self.assert_http_error(400, "注册", lambda: self.view(guest, "2"))


if __name__ == "__main__":
    unittest.main()
