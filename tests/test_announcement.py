"""房间公告（「公告/<房间号>.txt」）的文件层测试：读取、热更新、编码容错、按房间隔离。

公告是老师自己在项目根目录的「公告」文件夹里维护的 txt，测试必须用**自己的临时目录**，
否则老师改一句公告就能让测试变红。这里所有用例都指向临时目录。

分工说明：
  · 本文件测「文件层」—— 怎么读、怎么写、编码兼容、房间之间互不干扰；
  · 权限层（谁能改哪个房间的公告）在 test_room_owner.py 里测。
"""
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as chat

BOM = b"\xef\xbb\xbf"


def default_body(room_id: str = "lobby") -> str:
    """某个房间的内置默认公告正文（去掉 # 说明行后的样子），用来和读出来的比。"""
    return chat._announcement_body(chat.default_announcement(room_id))


class AnnouncementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lan-chat-announce-")
        self.root = Path(self.temp.name)

        # ★ 顺序很要紧：init_db() 结尾有一步「把老的全局公告.txt 搬进 公告/lobby.txt」的迁移。
        #   所以 ANNOUNCE_DIR 和 LEGACY_ANNOUNCE_FILE 必须在 init_db() **之前**隔离好，
        #   否则测试会顺手在你正在用的项目目录里生成一份 公告/lobby.txt（本轮真踩过）。
        self.original_dir = chat.ANNOUNCE_DIR
        chat.ANNOUNCE_DIR = self.root / "公告"
        self.original_legacy = chat.LEGACY_ANNOUNCE_FILE
        chat.LEGACY_ANNOUNCE_FILE = self.root / "没有这个旧公告.txt"

        # ★ 数据库也要隔离：default_announcement() 要按房间号查房间名（lobby → 综合大厅）。
        self.original_db = chat.DB_PATH
        chat.DB_PATH = self.root / "chatroom.db"
        chat.init_db()

        self.original_interval = chat.ANNOUNCE_CHECK_INTERVAL
        # 生产环境每秒才查一次改动，测试里改成每次调用都查，省掉 sleep。
        chat.ANNOUNCE_CHECK_INTERVAL = 0.0

        chat.reset_announcement_cache()

    def tearDown(self):
        chat.ANNOUNCE_DIR = self.original_dir
        chat.ANNOUNCE_CHECK_INTERVAL = self.original_interval
        chat.LEGACY_ANNOUNCE_FILE = self.original_legacy
        chat.reset_announcement_cache()
        chat.close_db()
        chat.DB_PATH = self.original_db
        self.temp.cleanup()

    # ------------------------------------------------------------ 工具
    def path(self, room_id: str = "lobby") -> Path:
        return chat.announcement_path(room_id)

    def save(self, content: str, room_id: str = "lobby", encoding: str = "utf-8", bom: bool = True) -> None:
        """改写公告文件后**不**手动清缓存，走真实的「发现文件变了就重载」路径。"""
        target = self.path(room_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((BOM if bom else b"") + content.encode(encoding))

    # ============================================================ 读取与生成
    def test_missing_file_is_created_with_defaults(self):
        self.assertFalse(self.path().exists())
        text = chat.room_announcement("lobby")
        self.assertTrue(self.path().exists(), "公告文件缺失时应自动生成一份，老师才有得改")
        self.assertTrue(self.path().read_bytes().startswith(BOM), "生成的公告应带 BOM，记事本才不会乱码")
        self.assertEqual(text, default_body("lobby"))
        self.assertIn("欢迎来到综合大厅", text, "默认公告里要写清这是哪个房间")

    def test_generated_file_matches_the_builtin_constant(self):
        """自动生成的文件必须与 app.py 里的常量逐行一致，否则自检会误报。"""
        chat.room_announcement("lobby")
        generated = self.path().read_bytes().decode("utf-8-sig").replace("\r\n", "\n").strip()
        self.assertEqual(generated, chat.default_announcement("lobby").replace("\r\n", "\n").strip())

    def test_comment_lines_are_never_shown(self):
        self.save("# 这是说明，不该出现在界面\n真正的公告\n# 又一行说明\n第二行正文\n")
        self.assertEqual(chat.room_announcement("lobby"), "真正的公告\n第二行正文")

    def test_line_breaks_are_kept(self):
        self.save("第一行\n第二行\n第三行\n")
        self.assertEqual(chat.room_announcement("lobby"), "第一行\n第二行\n第三行")

    def test_edit_takes_effect_without_restart(self):
        self.save("旧公告\n")
        self.assertEqual(chat.room_announcement("lobby"), "旧公告")
        self.save("新公告，今天有公开课\n")   # 进程没有任何重启动作
        self.assertEqual(chat.room_announcement("lobby"), "新公告，今天有公开课", "公告改了却要重启才生效")

    def test_empty_or_comment_only_file_falls_back_to_default(self):
        """整份清空或只留说明时不留空白块，直接回到内置默认公告。"""
        self.save("")
        self.assertEqual(chat.room_announcement("lobby"), default_body("lobby"))
        chat.reset_announcement_cache()
        self.save("# 只剩说明\n\n   \n")
        self.assertEqual(chat.room_announcement("lobby"), default_body("lobby"))

    def test_accepts_gbk_and_bom_less_utf8(self):
        # 记事本「另存为 ANSI」后文件是 GBK，读不出来等于公告整份失效。
        self.save("今天是运动会\n", encoding="gbk", bom=False)
        self.assertEqual(chat.room_announcement("lobby"), "今天是运动会")
        chat.reset_announcement_cache()
        self.save("今天是运动会\n", encoding="utf-8", bom=False)
        self.assertEqual(chat.room_announcement("lobby"), "今天是运动会")

    def test_unreadable_file_does_not_break_the_room(self):
        """公告文件读不出来时退回内置默认公告，绝不能把服务拖挂。

        造法：把公告「目录」的位置占成一个普通文件 —— 这时连 mkdir 都会失败，
        等价于「目录不可用」，比模拟权限问题更稳定（Windows 上 chmod 不可靠）。
        """
        blocked = self.root / "被占用的公告目录"
        blocked.write_text("我是文件，不是目录", encoding="utf-8")
        chat.ANNOUNCE_DIR = blocked
        chat.reset_announcement_cache()
        self.assertEqual(chat.room_announcement("lobby"), default_body("lobby"))

    def test_announcement_is_not_profanity_filtered(self):
        """公告由老师维护，不走违禁词过滤（否则老师会被自己的公告弄糊涂）。"""
        words = self.root / "违禁词库.txt"
        words.write_bytes(BOM + "傻逼\r\n".encode("utf-8"))
        original_words = chat.WORDS_FILE
        original_interval = chat.WORDS_CHECK_INTERVAL
        chat.WORDS_FILE = words
        chat.WORDS_CHECK_INTERVAL = 0.0
        chat.reset_wordlist_cache()
        try:
            self.save("傻逼两个字只是举例子\n")
            self.assertEqual(chat.room_announcement("lobby"), "傻逼两个字只是举例子")
            # 对照组：同样的文字走普通消息过滤时确实会被打码，证明上面那句不是空断言。
            self.assertEqual(chat.moderate_content("傻逼两个字只是举例子")[0], "**两个字只是举例子")
        finally:
            chat.WORDS_FILE = original_words
            chat.WORDS_CHECK_INTERVAL = original_interval
            chat.reset_wordlist_cache()

    # ============================================================ 按房间隔离
    def test_each_room_reads_its_own_file(self):
        """★ 两个房间各读各的文件，改一个不影响另一个。"""
        self.save("大厅的公告\n", room_id="lobby")
        self.save("游戏房的公告\n", room_id="games")
        self.assertEqual(chat.room_announcement("lobby"), "大厅的公告")
        self.assertEqual(chat.room_announcement("games"), "游戏房的公告")
        self.assertNotEqual(self.path("lobby"), self.path("games"))
        self.assertEqual(self.path("games").name, "games.txt")

    def test_default_announcement_carries_the_room_name(self):
        """每个房间自动生成的公告里，标题和欢迎语都写着**自己**的房间名。"""
        self.assertIn("欢迎来到游戏讨论", chat.room_announcement("games"))
        self.assertIn("欢迎来到学习交流", chat.room_announcement("study"))

    def test_saving_one_room_leaves_the_other_untouched(self):
        """★ 页面里保存 A 房间的公告，B 房间的内容必须一字不动（这是房主权限的底线）。"""
        self.save("游戏房原公告\n", room_id="games")
        self.assertEqual(chat.room_announcement("games"), "游戏房原公告")
        chat.save_room_announcement("lobby", "大厅被改了")
        self.assertEqual(chat.room_announcement("games"), "游戏房原公告")
        self.assertEqual(chat.room_announcement("lobby"), "大厅被改了")

    def test_saved_announcement_is_readable_right_away(self):
        """页面上保存后不必等，下一次读取立刻是新内容（缓存那一格要失效掉）。"""
        chat.room_announcement("lobby")          # 先让缓存热起来
        chat.save_room_announcement("lobby", "刚写的新公告")
        self.assertEqual(chat.room_announcement("lobby"), "刚写的新公告")

    def test_saved_file_uses_bom_and_crlf(self):
        """页面上保存的公告也要带 BOM + CRLF，老师事后用记事本打开不会乱码。"""
        chat.save_room_announcement("lobby", "第一行\n第二行")
        raw = self.path("lobby").read_bytes()
        self.assertTrue(raw.startswith(BOM))
        self.assertIn(b"\r\n", raw)

    def test_room_id_cannot_escape_the_announcement_folder(self):
        """★ 房间号会被当成文件名，必须挡掉 "../" 之类的目录穿越。"""
        evil = chat.announcement_path("../../偷偷放出去")
        self.assertEqual(evil.parent, Path(chat.ANNOUNCE_DIR), "文件必须仍然落在公告目录里")
        self.assertNotIn("..", evil.name)
        self.assertNotIn("/", evil.name)
        self.assertNotIn("\\", evil.name)
        self.assertEqual(evil.name, "偷偷放出去.txt", "去掉斜杠和点之后应只剩正常字符")

    def test_empty_room_id_falls_back_to_lobby_file(self):
        """房间号传空（或全是不安全字符）时退回大厅，别生成一个叫「.txt」的怪文件。"""
        self.assertEqual(chat.announcement_path("").name, "lobby.txt")
        self.assertEqual(chat.announcement_path("../..").name, "lobby.txt")

    def test_unknown_room_still_gets_a_usable_default(self):
        """房间号查不到房间名时也不能炸，退回通用文案。"""
        text = chat.room_announcement("不存在的房间")
        self.assertTrue(text)
        self.assertIn("欢迎来到", text)


if __name__ == "__main__":
    unittest.main()
