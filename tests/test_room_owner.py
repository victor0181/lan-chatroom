"""房主 / 按房间禁言 / 按房间公告的测试。

要守住的三条契约：
  ① 房主是「某个人 × 某个房间」的授权，不是第四种 role —— 换房间就不再是房主；
  ② 房主禁言只影响本房间（全站禁言是另一个动作，只有管理员能用）；
  ③ 公告每个房间一份，互不干扰；房主只能改自己被授权那个房间的。

这里的会话表有两个，键不一样，容易搞混：
  · chat.sessions        —— 按 token 索引（登录态）
  · chat.manager.sessions —— 按 user_id 索引（在线连接，管理动作靠它找人）
所以下面 make_user() 会两边都登记。
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
TEST_WORDS = "傻逼\r\nfuck\r\n他妈\r\n"


class FakeSocket:
    """替身 websocket：把发出去的消息收集起来，用来断言「有没有报错 / 有没有广播」。"""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, payload):
        self.sent.append(payload)

    def last(self) -> dict:
        return self.sent[-1] if self.sent else {}

    def errors(self) -> list[str]:
        return [str(item.get("message", "")) for item in self.sent if item.get("type") == "error"]


class FakeRequest:
    def __init__(self, token: str | None = None):
        self.headers = {"X-Session-Token": token} if token else {}


def run(coro):
    return asyncio.run(coro)


class RoomOwnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lan-chat-owner-")
        self.root = Path(self.temp.name)
        chat.DB_PATH = self.root / "chatroom.db"
        # ★ 公告目录必须一起隔离，否则测试会往项目根目录的真实「公告」文件夹里写东西。
        self.original_announce_dir = chat.ANNOUNCE_DIR
        chat.ANNOUNCE_DIR = self.root / "公告"
        self.original_save_interval = chat.ANNOUNCEMENT_SAVE_INTERVAL
        # 默认放开限流，只有专门测限流的那个用例才把它调回来。
        chat.ANNOUNCEMENT_SAVE_INTERVAL = 0.0
        self.original_legacy = chat.LEGACY_ANNOUNCE_FILE
        chat.LEGACY_ANNOUNCE_FILE = self.root / "没有这个旧公告.txt"

        self.words = self.root / "违禁词库.txt"
        self.words.write_bytes(BOM + TEST_WORDS.encode("utf-8"))
        self.original_words_file = chat.WORDS_FILE
        self.original_words_interval = chat.WORDS_CHECK_INTERVAL
        chat.WORDS_FILE = self.words
        chat.WORDS_CHECK_INTERVAL = 0.0
        chat.reset_wordlist_cache()

        chat.sessions.clear()
        chat.manager.sessions.clear()
        chat.reset_announcement_cache()
        chat.reset_room_owner_cache()
        chat._ANNOUNCE_SAVED_AT.clear()
        chat.init_db()

    def tearDown(self):
        chat.WORDS_FILE = self.original_words_file
        chat.WORDS_CHECK_INTERVAL = self.original_words_interval
        chat.reset_wordlist_cache()
        chat.ANNOUNCE_DIR = self.original_announce_dir
        chat.ANNOUNCEMENT_SAVE_INTERVAL = self.original_save_interval
        chat.LEGACY_ANNOUNCE_FILE = self.original_legacy
        chat.sessions.clear()
        chat.manager.sessions.clear()
        chat.reset_announcement_cache()
        chat.reset_room_owner_cache()
        chat.close_db()
        self.temp.cleanup()

    # ------------------------------------------------------------ 工具
    def make_user(self, user_id="1", username="tester", nickname="测试用户",
                  role="user", room="lobby", online=True):
        """建一个真实账号 + 会话，并在两张会话表里都登记好。"""
        with closing(chat.get_db()) as db:
            db.execute(
                "INSERT OR REPLACE INTO users (id, username, password_hash, nickname, role, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, username, chat.hash_password("pw-123456"), nickname, role, chat.now_text()),
            )
            db.commit()
            row = dict(db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
        session = chat.Session(f"token-{user_id}", user_id, nickname, role, username, row)
        session.websocket = FakeSocket() if online else None
        session.room_id = room
        chat.sessions[session.token] = session
        # 管理动作是按 user_id 找人的（ConnectionManager 的键就是 user_id）。
        chat.manager.sessions[session.user_id] = session
        return session

    def act(self, session, **payload):
        run(chat.admin_action(session, payload))
        return session.websocket

    def make_owner(self, room="games", user_id="10", **kwargs):
        user = self.make_user(user_id=user_id, username=f"owner{user_id}", nickname=f"房主{user_id}", room=room, **kwargs)
        chat.set_room_owner(room, user_id, True)
        return user

    def post_announcement(self, session, room_id, content):
        return run(chat.update_announcement(
            chat.AnnouncementUpdate(room_id=room_id, content=content), FakeRequest(session.token)
        ))

    def assert_http_error(self, status, detail_part, func, *args, **kwargs):
        try:
            run(func(*args, **kwargs))
        except chat.HTTPException as error:
            self.assertEqual(error.status_code, status, f"状态码不对：{error.status_code} {error.detail}")
            if detail_part:
                self.assertIn(detail_part, str(error.detail))
            return error
        self.fail(f"本该抛 {status}，结果一路通过了")

    # ============================================================ 公告：按房间
    def test_each_room_has_its_own_announcement(self):
        lobby = chat.room_announcement("lobby")
        games = chat.room_announcement("games")
        self.assertNotEqual(lobby, games)
        self.assertIn("综合大厅", lobby)
        self.assertIn("游戏讨论", games)

    def test_default_announcement_files_are_named_by_room_id(self):
        chat.room_announcement("lobby")
        chat.room_announcement("games")
        names = sorted(item.name for item in chat.ANNOUNCE_DIR.glob("*.txt"))
        self.assertEqual(names, ["games.txt", "lobby.txt"], "用房间号当文件名，不用中文房间名")

    def test_announcement_file_has_bom_and_crlf(self):
        """公告文件必须能被记事本正常打开（原来的约定，别改坏）。"""
        chat.room_announcement("lobby")
        raw = chat.announcement_path("lobby").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "缺 UTF-8 BOM")
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\r\r\n", raw)

    def test_saving_one_room_does_not_touch_another(self):
        before = chat.room_announcement("lobby")
        chat.save_room_announcement("games", "今天下午有比赛")
        self.assertEqual(chat.room_announcement("games"), "今天下午有比赛")
        self.assertEqual(chat.room_announcement("lobby"), before)

    def test_comment_lines_are_stripped_on_write_and_read(self):
        """页面上写的 # 说明行和读的规则必须一致，否则会出现「写 3 行读出来 2 行」。"""
        chat.save_room_announcement("games", "# 这是说明\n真正的公告")
        self.assertEqual(chat.room_announcement("games"), "真正的公告")

    def test_announcement_path_cannot_escape_the_directory(self):
        """房间号里塞 ../ 也不能跳出公告目录。"""
        path = chat.announcement_path("../../../etc/passwd")
        self.assertEqual(path.parent, chat.ANNOUNCE_DIR, f"跑出目录了：{path}")

    def test_announcement_endpoint_returns_the_rooms_notice(self):
        chat.save_room_announcement("games", "游戏房间的公告")
        body = run(chat.announcement(room_id="games"))
        self.assertEqual(body["room_id"], "games")
        self.assertEqual(body["announcement"], "游戏房间的公告")

    def test_announcement_endpoint_falls_back_for_unknown_room(self):
        body = run(chat.announcement(room_id="不存在的房间"))
        self.assertEqual(body["room_id"], "lobby")

    # ============================================================ 公告：谁能改
    def test_owner_can_update_their_rooms_announcement(self):
        owner = self.make_owner("games", "10")
        body = self.post_announcement(owner, "games", "房主写的公告")
        self.assertEqual(body["announcement"], "房主写的公告")
        self.assertEqual(chat.room_announcement("games"), "房主写的公告")

    def test_admin_can_update_any_rooms_announcement(self):
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        self.post_announcement(admin, "study", "管理员改的公告")
        self.assertEqual(chat.room_announcement("study"), "管理员改的公告")

    def test_owner_of_another_room_cannot_update_this_room(self):
        """★ 房主只管自己的房间 —— 这是「房主」和「管理员」最本质的区别。"""
        owner = self.make_owner("games", "10")
        self.assert_http_error(403, "房主", self.post_announcement, owner, "study", "越权改公告")

    def test_plain_user_cannot_update_the_announcement(self):
        plain = self.make_user("11", "plain", "普通同学", room="games")
        self.assert_http_error(403, "房主", self.post_announcement, plain, "games", "我不是房主")

    def test_guest_cannot_update_the_announcement(self):
        guest = chat.Session("token-guest", "guest-abcd", "路人", "guest")
        guest.websocket = FakeSocket()
        chat.sessions[guest.token] = guest
        self.assert_http_error(403, "", self.post_announcement, guest, "lobby", "游客也想改公告")

    def test_announcement_length_is_limited(self):
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        long_text = "啊" * (chat.ANNOUNCEMENT_MAX_LENGTH + 1)
        self.assert_http_error(400, f"最多 {chat.ANNOUNCEMENT_MAX_LENGTH} 个字",
                               self.post_announcement, admin, "lobby", long_text)

    def test_announcement_is_profanity_filtered(self):
        """页面写进来的内容要过滤（房主也是学生）；老师直接改文件那条路不过滤。"""
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        self.assert_http_error(400, "不合适的词", self.post_announcement, admin, "lobby", "你他妈真好")

    def test_empty_announcement_is_rejected(self):
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        self.assert_http_error(400, "不能是空的", self.post_announcement, admin, "lobby", "   ")

    def test_comment_only_announcement_is_rejected(self):
        """只写 # 说明行 → 正文为空，等于把公告清了，必须拦下。"""
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        self.assert_http_error(400, "不能是空的", self.post_announcement, admin, "lobby", "# 只有说明行")

    def test_unknown_room_is_rejected(self):
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        self.assert_http_error(404, "房间不存在", self.post_announcement, admin, "no-such-room", "随便写")

    def test_saving_too_fast_is_rejected(self):
        """连点保存要被拦下，否则会把磁盘写爆。"""
        chat.ANNOUNCEMENT_SAVE_INTERVAL = 3.0
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        self.post_announcement(admin, "lobby", "第一次")
        self.assert_http_error(429, "太频繁", self.post_announcement, admin, "lobby", "第二次")

    def test_server_just_started_can_still_save(self):
        """★ 回归：限流不能用 time.monotonic() 和 0 做差 ——
        服务刚起来头几秒，那个差值会小于间隔，把头一次保存误判成「太频繁」。"""
        chat.ANNOUNCEMENT_SAVE_INTERVAL = 60.0
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        chat._ANNOUNCE_SAVED_AT.clear()
        body = self.post_announcement(admin, "lobby", "服务器刚起来也要能保存")
        self.assertEqual(body["announcement"], "服务器刚起来也要能保存")

    # ============================================================ 公告：更新提示
    def test_updating_announcement_posts_a_system_notice(self):
        """★ 房主改完公告，聊天区要插一条系统消息 ——
        只让左侧面板悄悄换字的话，正在打字的学生根本不会发现公告变了。"""
        owner = self.make_owner("games", "10")
        owner.websocket.sent.clear()
        self.post_announcement(owner, "games", "今天下午大扫除，请带好抹布")
        notes = [item for item in owner.websocket.sent if item.get("type") == "system"]
        self.assertEqual(len(notes), 1, f"应当只有一条系统提示，实际收到：{owner.websocket.sent}")
        self.assertIn("房主", notes[0]["message"])
        self.assertIn("房主10", notes[0]["message"])
        self.assertIn("更新了本房间公告", notes[0]["message"])

    def test_admin_update_is_announced_as_admin(self):
        """同一条链路，管理员改的时候落款要说「管理员」而不是「房主」。"""
        admin = self.make_user("9000", "admin", "王老师", role="admin", room="games")
        admin.websocket.sent.clear()
        self.post_announcement(admin, "games", "管理员改的公告")
        notes = [item for item in admin.websocket.sent if item.get("type") == "system"]
        self.assertEqual(len(notes), 1, f"应当只有一条系统提示，实际收到：{admin.websocket.sent}")
        self.assertIn("管理员 王老师 更新了本房间公告", notes[0]["message"])

    def test_the_notice_reaches_the_whole_room_and_nobody_else(self):
        """提示的范围必须跟公告一样是「本房间」—— 别房间的人不该看到。"""
        owner = self.make_owner("games", "10")
        mate = self.make_user("11", "mate", "同房同学", room="games")
        outsider = self.make_user("12", "out", "别房同学", room="study")
        for someone in (owner, mate, outsider):
            someone.websocket.sent.clear()
        self.post_announcement(owner, "games", "games 的新公告")
        for someone in (owner, mate):
            self.assertTrue(
                [item for item in someone.websocket.sent if item.get("type") == "system"],
                f"{someone.nickname} 就在 games 里，应当收到提示",
            )
        self.assertFalse(
            [item for item in outsider.websocket.sent if item.get("type") == "system"],
            "study 的人不该收到 games 的公告提示",
        )

    def test_admin_notice_lands_in_the_target_room(self):
        """管理员人在大厅、改的是游戏讨论区的公告 —— 提示要落在被改的那个房间。"""
        admin = self.make_user("9000", "admin", "王老师", role="admin", room="lobby")
        mate = self.make_user("11", "mate", "同房同学", room="games")
        admin.websocket.sent.clear()
        mate.websocket.sent.clear()
        self.post_announcement(admin, "games", "跨房间改的公告")
        self.assertTrue(
            [item for item in mate.websocket.sent if item.get("type") == "system"],
            "被改房间的学生应当收到提示",
        )

    def test_unchanged_content_does_not_spam_the_notice(self):
        """★ 正文一个字都没动就不再插提示 —— 房主反复点保存不该刷屏。"""
        owner = self.make_owner("games", "10")
        self.post_announcement(owner, "games", "一成不变的公告")
        owner.websocket.sent.clear()
        body = self.post_announcement(owner, "games", "一成不变的公告")   # 原样再存一次
        self.assertEqual(body["announcement"], "一成不变的公告")
        self.assertEqual(
            [item for item in owner.websocket.sent if item.get("type") == "system"], [],
            "内容没变却又插了一条提示",
        )

    def test_content_refresh_comes_before_the_notice(self):
        """顺序：先刷新公告内容、再插提示 —— 学生看到提示时已能看到新内容。"""
        owner = self.make_owner("games", "10")
        owner.websocket.sent.clear()
        self.post_announcement(owner, "games", "顺序测试用的公告")
        kinds = [item.get("type") for item in owner.websocket.sent]
        self.assertIn("announcement_updated", kinds)
        self.assertIn("system", kinds)
        self.assertLess(
            kinds.index("announcement_updated"), kinds.index("system"),
            f"提示早于内容刷新：{kinds}",
        )

    # ============================================================ 房主授权
    def test_admin_can_appoint_and_revoke(self):
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        student = self.make_user("10", "stu", "小甲", room="games")
        self.assertEqual(chat.room_owners("games"), [])

        self.act(admin, action="set_owner", target_id="10", room_id="games")
        self.assertEqual(chat.room_owners("games"), ["10"])
        self.assertTrue(chat.is_room_owner("10", "games"))

        self.act(admin, action="unset_owner", target_id="10", room_id="games")
        self.assertEqual(chat.room_owners("games"), [])
        self.assertFalse(chat.is_room_owner("10", "games"))
        self.assertTrue(student.websocket.sent, "撤销后也该有反馈")

    def test_appointing_is_admin_only(self):
        owner = self.make_owner("games", "10")
        other = self.make_user("11", "stu2", "小乙", room="games")
        self.act(owner, action="set_owner", target_id="11", room_id="games")
        self.assertIn("只有管理员", " ".join(owner.websocket.errors()))
        self.assertFalse(chat.is_room_owner("11", "games"))

    def test_role_is_not_changed_by_appointment(self):
        """★ 任命房主不改 role —— 他仍然是「普通用户」，只是多了一条按房间的授权。"""
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        student = self.make_user("10", "stu", "小甲", room="games")
        self.act(admin, action="set_owner", target_id="10", room_id="games")
        with closing(chat.get_db()) as db:
            role = db.execute("SELECT role FROM users WHERE id = ?", ("10",)).fetchone()[0]
        self.assertEqual(role, "user", "房主不是第四种 role")
        self.assertEqual(student.role, "user")

    def test_only_registered_users_can_be_owners(self):
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        guest = chat.Session("token-g", "guest-xyz", "路人", "guest")
        guest.websocket = FakeSocket()
        chat.sessions[guest.token] = guest
        chat.manager.sessions[guest.user_id] = guest

        self.act(admin, action="set_owner", target_id=guest.user_id, room_id="lobby")
        self.assertIn("注册用户", " ".join(admin.websocket.errors()))

        other_admin = self.make_user("9001", "admin2", "管理员2", role="admin")
        self.act(admin, action="set_owner", target_id="9001", room_id="lobby")
        self.assertIn("注册用户", " ".join(admin.websocket.errors()))
        self.assertFalse(chat.is_room_owner("9001", "lobby"))

    def test_owner_of_one_room_is_not_owner_of_another(self):
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        self.make_user("10", "stu", "小甲", room="games")
        self.act(admin, action="set_owner", target_id="10", room_id="games")
        self.assertTrue(chat.is_room_owner("10", "games"))
        self.assertFalse(chat.is_room_owner("10", "study"))

    def test_same_person_can_own_several_rooms(self):
        """★ 一对多：同一个人可以在多个房间都是房主。"""
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        self.make_user("10", "stu", "小甲", room="games")
        self.act(admin, action="set_owner", target_id="10", room_id="games")
        self.act(admin, action="set_owner", target_id="10", room_id="water")
        self.assertTrue(chat.is_room_owner("10", "games"))
        self.assertTrue(chat.is_room_owner("10", "water"))

    def test_one_room_can_have_several_owners(self):
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        for index in range(2):
            self.make_user(str(20 + index), f"stu{index}", f"小{index}", room="games")
            self.act(admin, action="set_owner", target_id=str(20 + index), room_id="games")
        self.assertEqual(sorted(chat.room_owners("games")), ["20", "21"])

    def test_owner_count_is_limited(self):
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        for index in range(chat.ROOM_OWNER_LIMIT):
            self.make_user(str(30 + index), f"stu{index}", f"小{index}", room="games")
            self.act(admin, action="set_owner", target_id=str(30 + index), room_id="games")
        extra = self.make_user("99", "stu99", "多出来的", room="games")
        self.act(admin, action="set_owner", target_id="99", room_id="games")
        self.assertIn("最多", " ".join(admin.websocket.errors()))
        self.assertFalse(chat.is_room_owner("99", "games"), "超限就该被挡下")
        self.assertTrue(extra.websocket.sent is not None)

    def test_re_appointing_an_existing_owner_is_not_an_error(self):
        """已经是房主再点一次「设为房主」不该报「超过上限」。"""
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        self.make_user("10", "stu", "小甲", room="games")
        self.act(admin, action="set_owner", target_id="10", room_id="games")
        self.act(admin, action="set_owner", target_id="10", room_id="games")
        self.assertEqual(chat.room_owners("games"), ["10"])
        self.assertEqual(admin.websocket.errors(), [])

    def test_owner_flag_in_public_follows_the_room(self):
        """★ public() 里的 owner 是「当前房间」的房主标识，换房间就变 False。"""
        owner = self.make_owner("games", "10")
        self.assertTrue(owner.public()["owner"], "在自己被授权的房间应为房主")
        owner.room_id = "study"
        self.assertFalse(owner.public()["owner"], "换到别的房间就不是房主了")
        # 反向确认：同房间里的普通人不带 owner 标记，避免「人人都是房主」的假绿。
        other = self.make_user("11", "stu2", "小乙", room="games")
        self.assertFalse(other.public()["owner"], "普通用户不该带房主标记")

    def test_guest_can_never_be_owner(self):
        guest = chat.Session("token-g", "guest-xyz", "路人", "guest")
        self.assertFalse(chat.can_manage_room(guest, "lobby"))

    # ============================================================ 房主的管理权限
    def test_owner_can_mute_someone_in_their_room(self):
        owner = self.make_owner("games", "10")
        noisy = self.make_user("11", "stu2", "话多的同学", room="games")
        self.act(owner, action="mute", target_id="11", minutes=2)
        self.assertGreater(noisy.muted_until_in("games"), time.time())
        self.assertEqual(noisy.websocket.errors(), [])

    def test_owner_cannot_mute_someone_in_another_room(self):
        """★ 房主只能管本房间 —— 目标人在别的房间时，权限不成立。"""
        owner = self.make_owner("games", "10")
        other = self.make_user("11", "stu2", "别的房间的人", room="study")
        self.act(owner, action="mute", target_id="11", minutes=2)
        self.assertIn("不是这个房间的房主", " ".join(owner.websocket.errors()))
        self.assertEqual(other.muted_until_in("study"), 0)

    def test_owner_cannot_mute_an_admin(self):
        owner = self.make_owner("games", "10")
        admin = self.make_user("9000", "admin", "管理员", role="admin", room="games")
        self.act(owner, action="mute", target_id="9000", minutes=2)
        self.assertIn("普通用户和游客", " ".join(owner.websocket.errors()))
        self.assertEqual(admin.muted_until_in("games"), 0)

    def test_owner_cannot_mute_themselves(self):
        owner = self.make_owner("games", "10")
        self.act(owner, action="mute", target_id="10", minutes=2)
        self.assertIn("普通用户和游客", " ".join(owner.websocket.errors()))
        self.assertEqual(owner.muted_until_in("games"), 0)

    def test_owner_cannot_kick(self):
        owner = self.make_owner("games", "10")
        self.make_user("11", "stu2", "小乙", room="games")
        self.act(owner, action="kick", target_id="11")
        self.assertIn("只有管理员", " ".join(owner.websocket.errors()))

    def test_owner_cannot_delete_messages(self):
        owner = self.make_owner("games", "10")
        self.act(owner, action="delete", message_id=1)
        self.assertIn("只有管理员", " ".join(owner.websocket.errors()))

    def test_owner_cannot_use_global_mute(self):
        """★ 全站禁言是管理员专属 —— 房主用了就会越界影响别的房间。"""
        owner = self.make_owner("games", "10")
        self.make_user("11", "stu2", "小乙", room="games")
        self.act(owner, action="mute_all", target_id="11", minutes=30)
        self.assertIn("只有管理员", " ".join(owner.websocket.errors()))

    def test_muted_owner_cannot_moderate(self):
        """被禁言的房主不该还能禁言别人。"""
        admin = self.make_user("9000", "admin", "管理员", role="admin", room="games")
        owner = self.make_owner("games", "10")
        self.make_user("11", "stu2", "小乙", room="games")
        self.act(admin, action="mute", target_id="10", minutes=2)
        self.act(owner, action="mute", target_id="11", minutes=2)
        self.assertIn("正在被禁言", " ".join(owner.websocket.errors()))

    def test_admin_still_can_do_everything(self):
        admin = self.make_user("9000", "admin", "管理员", role="admin", room="games")
        target = self.make_user("11", "stu2", "小乙", room="games")
        self.act(admin, action="mute", target_id="11", minutes=2)
        self.assertGreater(target.muted_until_in("games"), time.time())
        self.act(admin, action="unmute", target_id="11")
        self.assertEqual(target.muted_until_in("games"), 0)
        self.act(admin, action="mute_all", target_id="11", minutes=30)
        self.assertGreater(target.muted_until_in("games"), time.time())
        self.act(admin, action="unmute_all", target_id="11")
        self.assertEqual(target.muted_until_in("games"), 0)
        self.assertEqual(admin.websocket.errors(), [])

    def test_unknown_action_is_rejected(self):
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        self.act(admin, action="take_over_the_world", target_id="1")
        self.assertIn("未知的管理操作", " ".join(admin.websocket.errors()))

    # ============================================================ 禁言：房间隔离
    def test_room_mute_only_affects_that_room(self):
        """★ 这条是「房主只影响本房间」的核心：在 A 房被禁言，去 B 房照样能说话。"""
        owner = self.make_owner("games", "10")
        noisy = self.make_user("11", "stu2", "话多的同学", room="games")
        self.act(owner, action="mute", target_id="11", minutes=2)
        self.assertGreater(noisy.muted_until_in("games"), time.time())
        self.assertEqual(noisy.muted_until_in("study"), 0)
        self.assertEqual(noisy.muted_until_in("lobby"), 0)

    def test_global_mute_affects_every_room(self):
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        noisy = self.make_user("11", "stu2", "话多的同学", room="games")
        self.act(admin, action="mute_all", target_id="11", minutes=30)
        for room in ("lobby", "games", "study", "water", "music"):
            self.assertGreater(noisy.muted_until_in(room), time.time(), f"{room} 没被全站禁言覆盖")

    def test_unmute_clears_only_the_room_mute(self):
        """解除本房间禁言不该顺手把全站禁言也解掉（那是另一个动作）。"""
        admin = self.make_user("9000", "admin", "管理员", role="admin", room="games")
        target = self.make_user("11", "stu2", "小乙", room="games")
        self.act(admin, action="mute_all", target_id="11", minutes=30)
        self.act(admin, action="mute", target_id="11", minutes=2)
        self.act(admin, action="unmute", target_id="11")
        self.assertGreater(target.muted_until_in("games"), time.time(), "全站禁言不该被顺手清掉")
        self.act(admin, action="unmute_all", target_id="11")
        self.assertEqual(target.muted_until_in("games"), 0)

    def test_room_mute_is_persisted(self):
        """按房间禁言要落库，重启后仍然有效。"""
        owner = self.make_owner("games", "10")
        noisy = self.make_user("11", "stu2", "话多的同学", room="games")
        self.act(owner, action="mute", target_id="11", minutes=2)
        fresh = chat.user_room_mutes("11")
        self.assertGreater(fresh.get("games", 0), time.time())
        self.assertNotIn("study", fresh)

    def test_global_mute_is_persisted_for_registered_users(self):
        """★ 原来是 `if target.role == "user"`（白名单反面）——
        加身份之后被漏掉的人会「内存里禁言、重启就失效」，还不报错。"""
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        noisy = self.make_user("11", "stu2", "话多的同学", room="games")
        self.act(admin, action="mute_all", target_id="11", minutes=30)
        with closing(chat.get_db()) as db:
            value = float(db.execute("SELECT muted_until FROM users WHERE id = ?", ("11",)).fetchone()[0])
        self.assertGreater(value, time.time(), "全站禁言没写进 users 表")

    def test_guest_global_mute_does_not_error(self):
        """游客没有数据库记录，全站禁言只落内存，但不能报错。"""
        quest = chat.Session("token-g", "guest-xyz", "路人", "guest")
        self.assertIsNone(chat.persist_global_mute(quest, time.time() + 60))

    def test_public_muted_flag_follows_the_room(self):
        """在线列表上那个「禁言中」标记要跟房间走。"""
        admin = self.make_user("9000", "admin", "管理员", role="admin", room="games")
        target = self.make_user("11", "stu2", "小乙", room="games")
        self.act(admin, action="mute", target_id="11", minutes=2)
        self.assertGreater(target.public()["muted_until"], time.time())
        target.room_id = "study"
        self.assertEqual(target.public()["muted_until"], 0)

    # ============================================================ 判据本身
    def test_can_manage_room_matrix(self):
        admin = self.make_user("9000", "admin", "管理员", role="admin")
        owner = self.make_owner("games", "10")
        plain = self.make_user("11", "stu2", "小乙", room="games")
        guest = chat.Session("token-g", "guest-xyz", "路人", "guest")
        self.assertTrue(chat.can_manage_room(admin, "games"))
        self.assertTrue(chat.can_manage_room(admin, "study"))
        self.assertTrue(chat.can_manage_room(owner, "games"))
        self.assertFalse(chat.can_manage_room(owner, "study"))
        self.assertFalse(chat.can_manage_room(plain, "games"))
        self.assertFalse(chat.can_manage_room(guest, "games"))

    def test_owner_cache_is_invalidated_on_change(self):
        self.assertFalse(chat.is_room_owner("10", "games"))
        chat.set_room_owner("games", "10", True)
        self.assertTrue(chat.is_room_owner("10", "games"), "任命后缓存没失效")
        chat.set_room_owner("games", "10", False)
        self.assertFalse(chat.is_room_owner("10", "games"), "撤销后缓存没失效")


if __name__ == "__main__":
    unittest.main(verbosity=2)
