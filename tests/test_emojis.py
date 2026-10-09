"""自定义表情目录发现、中文文件名和短代码兼容性回归测试。"""
import re
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as chat


class EmojiOptionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lan-chat-emojis-")
        self.original_dir = chat.EMOJI_DIR
        chat.EMOJI_DIR = Path(self.temp.name)

    def tearDown(self):
        chat.EMOJI_DIR = self.original_dir
        self.temp.cleanup()

    def image(self, filename):
        (chat.EMOJI_DIR / filename).write_bytes(b"image-fixture")

    def test_empty_or_unsupported_directory_returns_no_custom_emojis(self):
        self.assertEqual(chat.emoji_options(), [])
        self.image(".gitkeep")
        self.image("说明.txt")
        (chat.EMOJI_DIR / "子目录.gif").mkdir()
        self.assertEqual(chat.emoji_options(), [])

    def test_chinese_filenames_are_loaded_with_stable_safe_tokens(self):
        for filename in ("暴怒.gif", "微笑.png", "😃.webp"):
            self.image(filename)
        options = chat.emoji_options()
        self.assertEqual(len(options), 3)
        self.assertEqual(options, chat.emoji_options())
        self.assertEqual({item["label"] for item in options}, {"暴怒", "微笑", "😃"})
        for item in options:
            self.assertRegex(item["name"], r"^[A-Za-z0-9_-]{1,40}$")
            self.assertEqual(item["token"], f":{item['name']}:")
            self.assertEqual(re.findall(r":([A-Za-z0-9_-]{1,40}):", item["token"]), [item["name"]])
            self.assertTrue((chat.EMOJI_DIR / unquote(item["src"].removeprefix("/emojis/"))).is_file())

    def test_existing_ascii_tokens_are_preserved(self):
        self.image("smile.gif")
        self.image("happy-face_2.PNG")
        self.assertEqual({item["token"] for item in chat.emoji_options()},
                         {":smile:", ":happy-face_2:"})

    def test_colliding_tokens_and_same_stem_extensions_are_distinct(self):
        for filename in ("1.gif", "开心1.gif", "难过1.gif", "smile.gif", "smile.png", "a" * 42 + ".gif", "a" * 43 + ".gif"):
            self.image(filename)
        options = chat.emoji_options()
        self.assertEqual(len(options), 7)
        self.assertEqual(len({item["token"] for item in options}), 7)
        self.assertTrue(all(len(item["name"]) <= 40 for item in options))

    def test_directory_changes_are_discovered_without_database_records(self):
        self.image("微笑.gif")
        initial = chat.emoji_options()[0]
        self.image("再见.gif")
        self.assertIn(initial, chat.emoji_options())
        (chat.EMOJI_DIR / "微笑.gif").unlink()
        self.assertEqual([item["label"] for item in chat.emoji_options()], ["再见"])


if __name__ == "__main__":
    unittest.main()
