from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from urllib.parse import quote
from collections import deque
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("CHAT_DATA_DIR", str(BASE_DIR / "data"))).resolve()
DB_PATH = DATA_DIR / "chatroom.db"
STATIC_DIR = BASE_DIR / "static"
AVATAR_DIR = BASE_DIR / "avatars"
AVATAR_DIR.mkdir(parents=True, exist_ok=True)
EMOJI_DIR = BASE_DIR / "emojis"
EMOJI_DIR.mkdir(parents=True, exist_ok=True)
ADMIN_USERNAME = os.getenv("CHAT_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("CHAT_ADMIN_PASSWORD", "admin123")
MUTE_MINUTES = 2
KICK_COOLDOWN_SECONDS = 3 * 60
# ==================== 防刷屏 ====================
# 发言频率：滑动窗口，SPEAK_WINDOW_SECONDS 秒内最多 SPEAK_MAX_IN_WINDOW 条。
SPEAK_WINDOW_SECONDS = 1.0
SPEAK_MAX_IN_WINDOW = 3
# 重复内容：最近 SPEAK_REPEAT_WINDOW 条里，同一句话出现 SPEAK_REPEAT_LIMIT 次就判刷屏。
# 窗口取 8 而不是 3：连「甲 乙 甲 乙 甲 乙」这种交替刷屏也要一并兜住
#（窗口 5 时交替出现的计数永远只到 2，够不到 LIMIT=3，等于抓不到）。
SPEAK_REPEAT_WINDOW = 8
SPEAK_REPEAT_LIMIT = 3
# 发言收益冷却：同一人 SPEAK_INCOME_INTERVAL 秒内只计一次「说话」的金币与经验，
# 否则刷屏就等于刷金币（实测改动前 170 条能刷出 240 金币）。
SPEAK_INCOME_INTERVAL = 3.0
# 拒收提示的节流：同一人、同一条提示 SPEAK_NOTICE_INTERVAL 秒内只发一次。
# 前端会把 error 落进消息流，不节流的话「提示」自己就成了新的刷屏源。
SPEAK_NOTICE_INTERVAL = 2.0
# 「无意义内容」判定：只拦极端形态，正常表达（哈哈哈哈、！！！、👍👍）照常发。
SPAM_JUNK_MIN_LEN = 6        # 纯标点串至少这么长才拦
SPAM_SAME_CHAR_MIN_LEN = 12  # 同一个字符重复这么多遍才拦
# ==================== 个人资料 ====================
# 改资料按「实际改了几处」收费：昵称 / 头像 / 真实姓名 / 班级 / 个性签名 / 密码，
# 每处这么多金币。一次改两处就扣两份；传了但和现在一模一样的字段不算改动、不收钱。
PROFILE_EDIT_COST = 5
# 查看他人资料一次多少金币（看自己、管理员查看都免费）。
PROFILE_VIEW_COST = 1
# 同一次登录里对同一个人只扣一次（「解锁」式），免得手一抖连点两下被扣两回。
# 想改成「每看一次都扣」，把这个开关设成 False 即可。
PROFILE_VIEW_CACHE_PER_SESSION = True
# 各项长度上限（昵称与注册保持一致）。
NICKNAME_MAX_LENGTH = 20
REAL_NAME_MAX_LENGTH = 20
CLASS_NAME_MAX_LENGTH = 20
BIO_MAX_LENGTH = 40
# 班级不是一个自由文本框，而是「年级 + 班级号」两个下拉（界面上每个下拉的首项是「取消」，也就是不填）。
# 服务端按同一份白名单做归一化，认不出来的自由文本（例如「教务处」）直接拒绝 ——
# 否则绕过前端直连接口照样能把班级写乱，前端下拉就只是摆设了。
PROFILE_GRADE_OPTIONS = ("七年级", "八年级", "九年级")
PROFILE_CLASS_OPTIONS = tuple(f"{index}班" for index in range(1, 11))
# 归一化用的别名表：老数据里「七」和「7」都指七年级；班级号只认 1~10。
PROFILE_GRADE_ALIASES = {
    "七": "七年级", "7": "七年级", "八": "八年级", "8": "八年级", "九": "九年级", "9": "九年级",
}
PROFILE_CLASS_NUMBERS = {str(index) for index in range(1, 11)}
# 改了昵称之后，要不要把历史消息里的旧昵称一并改掉。
# 打开：同一个人在房间里不会出现两个名字（sender_id 不变，管理员照样能追溯到人）。
RENAME_UPDATES_HISTORY = True
# 反馈文案用的字段中文名。
PROFILE_FIELD_LABELS = {
    "avatar": "头像", "nickname": "昵称", "password": "密码",
    "real_name": "真实姓名", "class_name": "班级", "bio": "个性签名",
}
LEVEL_TITLES = {
    1: "初来乍到",
    2: "活跃成员",
    3: "聊天达人",
    4: "房间红人",
    5: "资深玩家",
    6: "传奇人物",
    7: "社区明星",
    8: "终极大佬",
}
STARTER_INVENTORY = {"basic_flower": 3, "basic_applause": 3, "basic_kiss": 1, "basic_fireworks": 1}
PET_TYPES = {"chicken": "小鸡", "cat": "小猫", "dog": "小狗"}
PET_MIN_LEVEL = 5
PET_CHANGE_COST = 20
COIN_GIFT_MIN = 1
COIN_GIFT_MAX = 10000
ROOM_OWNER_REWARD = 100
PET_ACTION_ITEMS = {
    "feed": ("pet_food", 28, "喂食"),
    "water": ("pet_water", 32, "喂水"),
    "clean": ("pet_clean", 36, "清洁"),
}
# 让一节 40 分钟课里（按学生平均停留 20 分钟）能明显看到状态变化：
# 20 分钟约下降：饥饿 24、口渴 30、清洁度 16。
PET_DECAY_PER_MINUTE = {"hunger": 1.2, "thirst": 1.5, "cleanliness": 0.8}
# 基础礼物统一定价 1 金币（原为 0）：
#   「0 金币 + 不限购买」等于「免费无限领取」，而且价格 0 时回收价恒为 0，
#   清理库存毫无回报。改成 1 金币后，领取有了成本，回收也能拿回 1 金币。
# ⚠️ 价格改动必须与 recycle_price() 的「保底 1 金币」配套：价格 1 时 1//2 == 0，
#   没有保底的话回收价还是 0，改价就白改了。两者是一组，改一个必须看另一个。
STARTER_ITEMS = {
    "basic_flower": {"id": "basic_flower", "name": "鲜花", "icon": "🌹", "price": 1, "item_type": "gift", "min_level": 1, "description": "基础礼物：送给喜欢的人"},
    "basic_applause": {"id": "basic_applause", "name": "掌声", "icon": "👏", "price": 1, "item_type": "gift", "min_level": 1, "description": "基础礼物：为对方鼓掌"},
    "basic_kiss": {"id": "basic_kiss", "name": "飞吻", "icon": "💋", "price": 1, "item_type": "gift", "min_level": 1, "description": "基础礼物：送出一个飞吻"},
    "basic_fireworks": {"id": "basic_fireworks", "name": "礼花", "icon": "🎉", "price": 1, "item_type": "gift", "min_level": 1, "description": "基础礼物：让聊天热闹起来"},
}


def has_unlimited_coins(session: "Session") -> bool:
    """管理员的金币只作为展示余额，实际消费不受余额限制。"""
    return session.role == "admin"


def coins_display(session: "Session") -> int | str:
    return "∞" if has_unlimited_coins(session) else session.coins


def can_spend_coins(session: "Session", amount: int) -> bool:
    return has_unlimited_coins(session) or session.coins >= amount


def spend_coins(session: "Session", amount: int) -> None:
    if not has_unlimited_coins(session):
        session.coins -= amount

# ==================== 聊天内容违禁词过滤 ====================
# 词库放在项目根目录的「违禁词库.txt」，用记事本就能改；改完存盘即生效，
# 不用重启服务。过滤统一在后端做，所有浏览器看到的内容都从这里出去。
WORDS_FILE = BASE_DIR / "违禁词库.txt"
# 每条消息都去查一次文件时间戳太浪费，最多每秒检查一次改动。
WORDS_CHECK_INTERVAL = 1.0

# 允许夹在违规词字与字之间的“噪声字符”：空白加上常见的中英文标点。
# 刻意不含汉字、字母和数字，否则「傻[x]逼」会跨过正常汉字造成大量误伤。
_SEPARATOR_CHARS = " \t\r\n\u3000\u00a0._-,;:!?~`'\"()[]{}<>/\\|+*=#$%^&@、。，；：！？“”‘’（）【】《》…·～—－×＊"
_PROFANITY_GAP = "[" + re.escape(_SEPARATOR_CHARS) + "]*"
# 全角字母数字统一转成半角，否则「傻Ｂ」「ＳＢ」能绕过词表。
_FULLWIDTH_TABLE = str.maketrans(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
)
# 零宽字符肉眼看不见，却是最常见的拆词绕过手段，直接删掉。
_INVISIBLE_TABLE = str.maketrans("", "", "\u200b\u200c\u200d\ufeff")

# ★ 内置误伤保护：词库里没写后缀标记时自动套用。
# 这样老师把词条删掉再加回来、或者整份重写词库，都不会把「零误伤」改坏。
# 值都是【正则片段】：内置表里可以写字符类，用户词库里写的普通文字读取时会先转义。
_BUILTIN_TRAILING_BLOCK = {"他妈": "妈"}                      # 他妈＋妈＝他妈妈，不算骂人
_BUILTIN_LEADING_BLOCK = {"妈的": "[\u4e00-\u9fff]", "日你": "生"}  # 我妈的病 / 今天我生日你没来
_BUILTIN_WHOLE_WORD = {"sb"}                                  # 只有单独出现才算，USB 不算
# 这些正常词语里恰好含有违规子串（「我操」←「我操作」），匹配前先整体保护起来。
SAFE_PHRASES = ("我操作", "我操心", "我操办", "我操控", "我操场", "我操行")

# 首次运行会自动在项目根目录生成这份词库（UTF-8 带 BOM + CRLF，任何记事本都能正常打开）。
DEFAULT_WORDLIST = """# 局域网互动聊天室 · 违禁词库
# ============================================================
# 怎么改：直接用记事本打开这个文件，改完【保存】就生效，
#         不用重启服务、也不用关控制台；新加的词对历史消息同样有效。
#
# 规则：
#   1. 一行一个词条。
#   2. 以 # 开头的整行是说明，不会当成词条；空行忽略。
#   3. 第二个大段是「默认关闭」的词，把行首的 # 删掉就能启用。
#   4. 插空格、插标点、全角字母都拦得住，不必为这些写法各写一行：
#      「傻 逼」「傻。逼」「傻-逼」「傻Ｂ」「f u c k」都会被屏蔽。
#
# 可选后缀（新加词担心误伤时才需要写）：
#   傻逼 <-我     前面紧挨着「我」就不算违规
#   他妈 ->妈     后面紧挨着「妈」就不算违规
#   fk =          英文/数字词必须独立成词才算
#
# 已经内置的误伤保护（不用你设置，词条删了再加回来也依然有效）：
#   他妈 ->妈       保护「他妈妈是老师」
#   妈的 <-汉字      保护「我妈的病好多了」
#   日你 <-生        保护「今天我生日你没来」
#   sb  =            保护「USB 接口」
#   我操 整体豁免     保护「我操作 / 我去操场打球」
#
# 维护提示：想加词，就在对应分类下面加一行；不想要的词，行首加一个 # 即可。
# 注意：说明文字要单独占一行，别写在词条后面 —— 启用时整行都会被当成词条。
# ============================================================


# ==================== 第一部分：默认生效 ====================


# ---------- 辱骂 / 人身攻击 ----------
草泥马
操你妈
操你妹
操你大爷
草你妈
妈了个逼
妈了个巴子
马勒戈壁
马拉戈壁
麻辣隔壁
你妈逼
你麻痹
你妈的
你妈死了
死全家
全家死光
傻逼
傻比
沙比
沙壁
煞笔
煞逼
傻b
sb
傻叉
傻叼
傻吊
傻屌
二逼
逗逼
呆逼
撕逼
装逼
逼格
傻缺
傻蛋
傻帽
妈的
妈蛋
他妈
特么
尼玛
泥马
尼马
日你
我操
我艹
卧槽
卧草
去你妈
滚你妈
滚蛋
狗东西
狗日的
狗娘养的
野种
畜生
婊子
妓女
骚货
贱人
贱货
荡妇
智障
脑残
弱智
白痴
蠢货
蠢猪
人渣
混蛋
王八蛋
去死吧
吃屎
吃翔
屌
屌丝
你妹 ->妹
你丫

# ---------- 色情 / 低俗 ----------
色情
淫秽
黄片
毛片
黄图
裸照
裸聊
约炮
一夜情
卖淫
嫖娼
招嫖
做爱
强奸
轮奸
猥亵
乱伦
援交
包养
二奶
撸管
打飞机
老色批

# ---------- 暴力 / 威胁 ----------
杀了你
弄死你
打死你
砍死你
废了你
人肉搜索

# ---------- 赌博 / 诈骗 ----------
六合彩
百家乐
时时彩
赌球
赌场
网赌
博彩

# ---------- 英文脏话 / 网络缩写 ----------
fuck
fucking
motherfucker
shit
bullshit
bitch
bastard
asshole
slut
whore
nigger
cunt
wtf
nmsl
tmd
mdzz
mlgb


# ==================== 第二部分：默认关闭 ====================
# 下面这些词都容易打进正常用语，或者你未必想拦。
# 确认需要时，把词条行【最前面那一个 # 号】删掉即可，说明行不用动。
#
# --- 单字（误伤面最广，逐个列出来）---
# 操（操作/操心）、靠（可靠）、日（周日）、草（草莓）、干（干净）、
# 死（死机）、傻（傻瓜）、蠢（蠢萌）、滚（滚动）、贱、骚
#操
#靠
#日
#草
#干
#死
#傻
#蠢
#滚
#贱
#骚
#
# --- 会打进正常词的词（括号里是它牵累的正常说法）---
# 低能：会打进「低能耗」「低能见度」
#低能
# 废物：会打进「废物利用」「废物回收」
#废物
# 垃圾：会打进「垃圾分类」「倒垃圾」
#垃圾
# 杂种：会打进「杂种优势」
#杂种
# 禽兽：会打进「禽兽不如」，语文课会讲到
#禽兽
# 包过：会打进「面包过期」
#包过
# 买春：会打进「买春联」
#买春
# 鸡儿：会打进「小鸡儿」
#鸡儿
# 小三：会打进「小三班」「小学三年级」
#小三
# a片：会打进「data片段」
#a片
# 次奥：会打进「这次奥数比赛」
#次奥
# 神经病：医学语境
#神经病
# 有病：会打进「有病床」「有病人」
#有病
# 变态：会打进「变态反应」「变态发育」
#变态
# 傻瓜：语气很轻，课堂里常见
#傻瓜
# 笨蛋：语气很轻
#笨蛋
# 沙雕：现在多当玩笑说
#沙雕
# 人妖：涉及性别议题的讨论
#人妖
# 骗子：新闻、讨论里会正常出现
#骗子
#
# --- 广告 / 诈骗（平时不拦，出现刷屏广告再放开）---
#刷单
#代练
#外挂
#开挂
#破解版
#免费领
#日赚
#月入过万
#加微信
#加QQ
#扫码进群
#传销
#洗钱
#高利贷
#
# --- 赌博（按需放开）---
#炸金花
#龙虎斗
#押注
#下注
#梭哈
#
# --- 毒品（发现线索比拦住更重要，默认不拦）---
#冰毒
#海洛因
#摇头丸
#大麻
#吸毒
#贩毒
#
# --- 英文（这些词有正常含义，必须独立成词才算，所以带 = 号）---
#dick =
#cock =
#ass =
#piss =
#crap =
#damn =
#retard =
#idiot =
#stupid =
#moron =
#loser =
#jerk =
#stfu
"""


def _decode_text(raw: bytes) -> str:
    """文本配置文件（词库 / 公告）可能是 UTF-8、UTF-8 带 BOM 或记事本存的 ANSI（GBK），三种都要认。"""
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_wordlist(content: str) -> list[tuple[str, str | None, str | None, bool]]:
    """解析词库，返回 [(词条, 前置阻断正则片段, 后置阻断正则片段, 是否整词)]。"""
    entries: list[tuple[str, str | None, str | None, bool]] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        marker, argument = "", ""
        match = re.search(r"<-|->|=", line)
        if match:
            marker = match.group(0)
            argument = line[match.end():].strip()
            term = line[: match.start()].strip()
        else:
            term = line
        if not term:
            continue
        leading = trailing = None
        whole_word = term in _BUILTIN_WHOLE_WORD
        if marker == "<-" and argument:
            leading = re.escape(argument)
        elif marker == "->" and argument:
            trailing = re.escape(argument)
        elif marker == "=":
            whole_word = True
        if leading is None:
            leading = _BUILTIN_LEADING_BLOCK.get(term)
        if trailing is None:
            trailing = _BUILTIN_TRAILING_BLOCK.get(term)
        entries.append((term, leading, trailing, whole_word))
    return entries


def _build_pattern(entries: list[tuple[str, str | None, str | None, bool]]) -> re.Pattern[str] | None:
    if not entries:
        return None
    # 长词优先，避免「傻逼」被「傻」先替换掉一半；同长度按字典序，
    # 保证同样的词库每次编译出的正则完全一致（否则测试会随机红）。
    ordered = sorted(entries, key=lambda entry: (-len(entry[0]), entry[0]))
    parts: list[str] = []
    seen: set[str] = set()
    for term, leading, trailing, whole_word in ordered:
        if term in seen:
            continue
        seen.add(term)
        body = _PROFANITY_GAP.join(re.escape(char) for char in term)
        if trailing:
            body += f"(?!{trailing})"
        if leading:
            body = f"(?<!{leading}){body}"
        if whole_word:
            parts.append(f"(?<![A-Za-z]){body}(?![A-Za-z])")
        elif term.isascii():
            # 英文词只要求前面不是字母，这样 fucker / shitty 也能被 fuck / shit 覆盖。
            parts.append(f"(?<![A-Za-z]){body}")
        else:
            parts.append(body)
    return re.compile("|".join(parts), re.IGNORECASE)


_WORDS_LOCK = threading.Lock()
# loaded=False 表示还没有成功读过盘；stamp 是文件时间戳，变了就重新编译。
_WORDS_CACHE: dict[str, Any] = {"loaded": False, "stamp": None, "pattern": None, "count": 0, "checked": 0.0}


def _wordlist_entries() -> list[tuple[str, str | None, str | None, bool]]:
    """读词库；文件不在就生成一份默认的。读不出来时退回内置默认词表，绝不静默关掉过滤。"""
    if not WORDS_FILE.exists():
        try:
            # 带 BOM 写出，记事本和写字板才能正确认出是 UTF-8。
            WORDS_FILE.write_bytes(b"\xef\xbb\xbf" + DEFAULT_WORDLIST.replace("\n", "\r\n").encode("utf-8"))
            print(f"[违禁词库] 已生成默认词库：{WORDS_FILE}")
        except OSError as error:
            print(f"[违禁词库] 无法创建词库文件（{error}），本次使用内置默认词表")
            return _parse_wordlist(DEFAULT_WORDLIST)
    try:
        raw = WORDS_FILE.read_bytes()
    except OSError as error:
        print(f"[违禁词库] 读取失败（{error}），本次使用内置默认词表")
        return _parse_wordlist(DEFAULT_WORDLIST)
    return _parse_wordlist(_decode_text(raw))


def _wordlist_stamp() -> int | None:
    try:
        return WORDS_FILE.stat().st_mtime_ns
    except OSError:
        return None


def reset_wordlist_cache() -> None:
    """丢掉缓存，让下一次过滤强制重新读盘（测试与手工刷新用）。"""
    with _WORDS_LOCK:
        _WORDS_CACHE["loaded"] = False
        _WORDS_CACHE["checked"] = 0.0


def wordlist_count() -> int:
    """当前生效的词条数量，供自检和命令行排查使用。"""
    sensitive_pattern()
    return int(_WORDS_CACHE["count"])


def sensitive_pattern() -> re.Pattern[str] | None:
    """当前生效的违禁词正则。词库文件一变就重新编译，所以改完存盘即生效。"""
    now = time.monotonic()
    with _WORDS_LOCK:
        if _WORDS_CACHE["loaded"] and now - _WORDS_CACHE["checked"] < WORDS_CHECK_INTERVAL:
            return _WORDS_CACHE["pattern"]
        _WORDS_CACHE["checked"] = now
        stamp = _wordlist_stamp()
        if _WORDS_CACHE["loaded"] and stamp == _WORDS_CACHE["stamp"]:
            return _WORDS_CACHE["pattern"]
        entries = _wordlist_entries()
        _WORDS_CACHE.update(
            loaded=True,
            # 生成/改写文件后要重新取一次时间戳，否则会被当成“又变了”反复重载。
            stamp=_wordlist_stamp(),
            pattern=_build_pattern(entries),
            count=len(entries),
        )
        print(f"[违禁词库] 已加载 {len(entries)} 个词条（{WORDS_FILE.name}）")
        return _WORDS_CACHE["pattern"]


def normalize_text(text: str) -> str:
    """全角字母数字转半角并去掉零宽字符，减少绕过词表的手段。"""
    return str(text).translate(_FULLWIDTH_TABLE).translate(_INVISIBLE_TABLE)


def moderate_content(content: str) -> tuple[str, bool]:
    """屏蔽违规词，返回（处理后的内容，是否违规）。子串替换成等长星号，长度不变。"""
    plain = normalize_text(content)
    pattern = sensitive_pattern()
    if pattern is None:  # 词库为空（被清空且没写任何词条）时不过滤，也不报错
        return plain, False
    text = plain
    shielded: list[str] = []
    # 「我操作」这类正常词先换成占位符，等判定完再把星号还原回去。
    for phrase in SAFE_PHRASES:
        while phrase in text:
            token = f"\x00{len(shielded)}\x00"
            text = text.replace(phrase, token, 1)
            shielded.append(phrase)
    if not pattern.search(text):
        return plain, False
    masked = pattern.sub(lambda match: "*" * len(match.group(0)), text)
    for index, phrase in enumerate(shielded):
        masked = masked.replace(f"\x00{index}\x00", phrase)
    return masked, True


# ==================== 防刷屏 ====================
def _is_symbol_junk(character: str) -> bool:
    """这个字符算不算「纯装饰符号」。汉字/字母/数字不算；emoji 也不算（那是正常表达）。"""
    if character.isalnum() or character.isspace():
        return False
    # So 类里装着绝大多数 emoji（😀 👍 🎉），刻意排除，免得把表情当垃圾拦掉。
    return unicodedata.category(character) in (
        "Po", "Ps", "Pe", "Pd", "Pc", "Pi", "Pf", "Sm", "Sc", "Sk")


def looks_like_junk(content: str) -> bool:
    """整条消息都是无意义内容？（纯标点串 / 同一个字符拖一长串）

    阈值刻意保守：只拦极端形态，让「哈哈哈哈」「！！！」这类正常表达照常发出去。
    """
    text = content.strip()
    if not text:
        return False
    if len(text) >= SPAM_SAME_CHAR_MIN_LEN and len(set(text)) == 1:
        return True
    return len(text) >= SPAM_JUNK_MIN_LEN and all(_is_symbol_junk(ch) for ch in text)


def allow_speak(session: Session) -> bool:
    """发言限流：SPEAK_WINDOW_SECONDS 秒内最多放行 SPEAK_MAX_IN_WINDOW 条。"""
    moment = time.time()
    window = session.speak_times
    while window and moment - window[0] >= SPEAK_WINDOW_SECONDS:
        window.popleft()
    if len(window) >= SPEAK_MAX_IN_WINDOW:
        return False
    window.append(moment)
    return True


def screen_outgoing(session: Session, raw: Any) -> tuple[str | None, bool, str]:
    """发言前的统一预检：空白 / 零宽空白 / 重复刷屏 / 无意义内容 / 频率。

    放行 → (内容, 是否违规, "")；拦下 → (None, False, 提示语)。
    提示语为空串表示「静默丢弃」（纯空白消息走这条，与改动前行为一致）。
    """
    text = str(raw or "").strip()
    if not text:
        return None, False, ""
    content, violated = moderate_content(text[:500])
    # ★ 这里必须再判一次空。零宽字符（\u200b 之类）的 isspace() 是 False、逃得过上面的
    #   strip，而 moderate_content 里的 normalize_text 会把它抹掉 —— 不复查就会放行一条
    #   「内容是空串」的消息：屏幕上出现空气泡，而且照样加金币。
    if not content.strip():
        return None, False, "消息内容是空白，没有发出去"
    repeated = session.recent_speaks.count(content) >= SPEAK_REPEAT_LIMIT
    session.recent_speaks.append(content)
    if repeated:
        return None, False, "不要连着发一样的内容，换个说法吧"
    if looks_like_junk(content):
        return None, False, "这句话里只有符号，没有实际内容，没有发出去"
    if not allow_speak(session):
        return None, False, f"发言太快了，{SPEAK_WINDOW_SECONDS:g} 秒内最多 {SPEAK_MAX_IN_WINDOW} 条，请稍等再发"
    return content, violated, ""


async def reject_speak(session: Session, reason: str) -> None:
    """把被拦下的发言告知本人。同一条提示做节流，避免提示自己变成刷屏源。"""
    if not reason:
        return
    moment = time.time()
    if moment - session.speak_notices.get(reason, 0.0) < SPEAK_NOTICE_INTERVAL:
        return
    session.speak_notices[reason] = moment
    await session.send({"type": "error", "message": reason})


# ==================== 房间公告 ====================
# 每个房间一份公告，放在项目根目录的「公告」文件夹里，文件名就是房间号（lobby.txt / games.txt …）。
# ★ 用房间号（纯字母）当文件名，不用房间中文名 —— 中文文件名在 Windows 的另存为、命令行、
#   打包脚本里反复出问题；而且房间名以后会被改，房间号不会。
# 用记事本就能改；改完存盘即生效，不用重启服务、也不用重新构建前端。
# 房主在页面上改的也是同一个文件，所以「一处内容、两个入口」，不会两处打架。
# ★ 默认放在项目根目录，就是为了让你能用记事本直接打开改。
#   CHAT_ANNOUNCE_DIR 是给测试用的：自动化测试跑在隔离目录里，绝不能去动你正在用的公告文件。
ANNOUNCE_DIR = Path(os.getenv("CHAT_ANNOUNCE_DIR", str(BASE_DIR / "公告"))).resolve()
# 老版本只有一份全局公告（项目根目录的「公告.txt」）。首次启动时会把它搬进「公告/lobby.txt」，
# 原文件保留不动（万一老师还想翻一翻）。
LEGACY_ANNOUNCE_FILE = BASE_DIR / "公告.txt"
# 每次刷新页面都去 stat 一次文件不算贵，但也别每条请求都查，最多每秒检查一次改动。
ANNOUNCE_CHECK_INTERVAL = 1.0
# 后台任务盯着公告文件变化的周期（老师用记事本改完要主动推送，不能等下一次读取）。
ANNOUNCE_WATCH_INTERVAL = 2.0
# 公告字数上限。原来只写在默认公告的说明里靠自觉，现在由服务端把关。
ANNOUNCEMENT_MAX_LENGTH = 200
# 同一个房间两次保存之间的最小间隔，防止有人反复点保存把磁盘写爆。
ANNOUNCEMENT_SAVE_INTERVAL = 3.0
# 一个房间最多几个房主，防止「满房间都是官」。
ROOM_OWNER_LIMIT = 5

# 首次运行会自动给每个房间生成这份公告（UTF-8 带 BOM + CRLF，任何记事本都能正常打开）。
# 以 # 开头的整行是说明文字，读取时会整行丢掉，不会显示给学生。
# {name} 会被替换成房间名（见 default_announcement）。
DEFAULT_ANNOUNCEMENT = """# {name} · 房间公告
# ============================================================
# 怎么改：用记事本打开这个文件，改完【保存】就生效，
#         不用重启服务、不用关控制台、也不用重新构建前端。
#         学生端刷新页面（按 F5）或重新进入聊天室后显示新内容。
#         房主也可以在页面上改本房间的公告，改的是同一个文件。
#
# 规则：
#   1. 一行一行写，以 # 开头的整行是说明，不会显示给学生。
#   2. 其余文字原样显示在左侧「今日公告」和右上角「📢 房间公告」弹窗里。
#   3. 换行会保留，可以分行写；最多 200 字，太长会把侧栏撑高。
#   4. 直接改这个文件不走违禁词过滤（它由老师维护），保存前请自己检查一遍；
#      房主在页面上保存的内容会过一遍违禁词过滤。
# ============================================================

欢迎来到{name}！
请大家文明聊天，友好交流。
"""

_ANNOUNCE_LOCK = threading.Lock()
# 每个房间一份缓存：room_id -> {"loaded", "stamp", "text", "checked"}
# loaded=False 表示还没成功读过盘；stamp 是文件时间戳，变了就重新载入。
_ANNOUNCE_CACHE: dict[str, dict[str, Any]] = {}
# 每个房间上一次保存的时刻，用来限流（见 ANNOUNCEMENT_SAVE_INTERVAL）。
_ANNOUNCE_SAVED_AT: dict[str, float] = {}
# room_id -> 后台任务上一次看到的时间戳（见 watch_announcements）。
# ★ 页面保存也要更新它：页面保存同样会改文件，不记账的话后台任务会把它当成
#   「老师用记事本改了公告」，于是同一条变更被推两遍（面板刷两次、聊天区插两条提示）。
_ANNOUNCE_WATCH_STAMPS: dict[str, int | None] = {}


def default_announcement(room_id: str) -> str:
    """某个房间的内置默认公告（首次运行时会照这个写成文件）。"""
    room = room_info(room_id)
    name = str(room["name"]) if room else "聊天室"
    return DEFAULT_ANNOUNCEMENT.replace("{name}", name)


def announcement_path(room_id: str) -> Path:
    """某个房间的公告文件路径。

    ★ 房间号只保留字母数字和 - _ ，防止有人把 "../" 之类塞进来跳出公告目录。
    """
    safe = "".join(char for char in str(room_id) if char.isalnum() or char in "-_") or "lobby"
    return ANNOUNCE_DIR / f"{safe}.txt"


def _write_announcement_file(path: Path, content: str) -> None:
    """写公告文件：带 BOM + CRLF，记事本、写字板都能正常打开。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    path.write_bytes(b"\xef\xbb\xbf" + normalized.encode("utf-8"))


def _announcement_body(content: str) -> str:
    """去掉 # 说明行与首尾空白，剩下的就是真正要显示的公告正文。"""
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line for line in lines if not line.lstrip().startswith("#")).strip()


def _announcement_stamp(room_id: str) -> int | None:
    try:
        return announcement_path(room_id).stat().st_mtime_ns
    except OSError:
        return None


def _migrate_legacy_announcement() -> None:
    """把老版本的全局「公告.txt」搬成「公告/lobby.txt」—— 只搬一次，原文件不删。"""
    target = announcement_path("lobby")
    if target.exists() or not LEGACY_ANNOUNCE_FILE.exists():
        return
    try:
        raw = LEGACY_ANNOUNCE_FILE.read_bytes()
        _write_announcement_file(target, _decode_text(raw))
    except OSError as error:
        print(f"[房间公告] 老公告文件迁移失败（{error}），本次跳过")
        return
    print(f"[房间公告] 已把老的「{LEGACY_ANNOUNCE_FILE.name}」搬进「{ANNOUNCE_DIR.name}/{target.name}」（原文件保留）")


def _read_announcement(room_id: str) -> str:
    """读某个房间的公告；文件不在就生成一份默认的。读不出来或没有正文时退回内置默认。"""
    path = announcement_path(room_id)
    fallback = _announcement_body(default_announcement(room_id))
    if not path.exists():
        try:
            _write_announcement_file(path, default_announcement(room_id))
            print(f"[房间公告] 已生成默认公告：{path}")
        except OSError as error:
            print(f"[房间公告] 无法创建公告文件（{error}），本次使用内置默认公告")
            return fallback
    try:
        raw = path.read_bytes()
    except OSError as error:
        print(f"[房间公告] 读取失败（{error}），本次使用内置默认公告")
        return fallback
    body = _announcement_body(_decode_text(raw))
    if not body:
        # 文件被清空（或只写了说明）时不留空白块，直接退回默认文案。
        print("[房间公告] 公告文件里没有正文，本次显示内置默认公告")
        return fallback
    return body


def reset_announcement_cache(room_id: str | None = None) -> None:
    """丢掉缓存，让下一次读取强制重新读盘（测试与手工刷新用）。不传房间号就清空全部。"""
    with _ANNOUNCE_LOCK:
        if room_id is None:
            _ANNOUNCE_CACHE.clear()
            _ANNOUNCE_SAVED_AT.clear()
            return
        _ANNOUNCE_CACHE.pop(room_id, None)


def room_announcement(room_id: str = "lobby") -> str:
    """某个房间当前生效的公告。文件一变就重新载入，所以改完存盘即生效。"""
    now = time.monotonic()
    with _ANNOUNCE_LOCK:
        cache = _ANNOUNCE_CACHE.get(room_id)
        if cache is None:
            cache = {"loaded": False, "stamp": None, "text": "", "checked": 0.0}
            _ANNOUNCE_CACHE[room_id] = cache
        if cache["loaded"] and now - cache["checked"] < ANNOUNCE_CHECK_INTERVAL:
            return str(cache["text"])
        cache["checked"] = now
        stamp = _announcement_stamp(room_id)
        if cache["loaded"] and stamp == cache["stamp"]:
            return str(cache["text"])
        text = _read_announcement(room_id)
        cache.update(
            loaded=True,
            # 生成/改写文件后要重新取一次时间戳，否则会被当成“又变了”反复重载。
            stamp=_announcement_stamp(room_id),
            text=text,
        )
        print(f"[房间公告] 已载入 {room_id} 的公告（{announcement_path(room_id).name}，{len(text)} 字）")
        return text


def save_room_announcement(room_id: str, content: str) -> str:
    """把房主/管理员在页面上写的公告落盘，并让下一次读取立刻拿到新内容。

    写进去的是「正文」：以 # 开头的说明行会被丢掉，和读的时候是同一套规则，
    免得「页面上写 3 行、读出来 2 行」这种对不上的情况。
    字数与内容合法性由调用方（接口）先校验，这里只负责写和让缓存失效。
    """
    text = _announcement_body(content)
    path = announcement_path(room_id)
    _write_announcement_file(path, text)
    with _ANNOUNCE_LOCK:
        _ANNOUNCE_SAVED_AT[room_id] = time.monotonic()
        _ANNOUNCE_CACHE.pop(room_id, None)
        # ★ 同步后台任务的时间戳账本 —— 否则它会把「这次页面保存」也当成一次外部改动再推一遍。
        _ANNOUNCE_WATCH_STAMPS[room_id] = _announcement_stamp(room_id)
    print(f"[房间公告] {room_id} 的公告已被更新（{len(text)} 字）")
    return text


# 老师用记事本改完公告文件后，服务端要自己发现并推送 ——
# 「改完存盘即生效」在 README 里承诺的是「同房间已在线的人立刻收到推送」，
# 而原先只有「页面上点保存」那条路会广播；直接改文件要等学生刷 F5 / 切房 / 重连才看得到。
def all_room_ids() -> list[str]:
    """所有房间号（后台任务要挨个盯公告文件）。"""
    with closing(get_db()) as db:
        return [str(row["id"]) for row in db.execute("SELECT id FROM rooms ORDER BY rowid").fetchall()]


async def watch_announcements() -> None:
    """盯着每个房间的公告文件，被记事本改过就广播出去。

    ★ 启动时先对每个房间读一次（文件缺失会顺手生成默认公告）再拍时间戳快照 ——
      不然「服务刚起来、文件本来就还没生成」会被当成「老师刚改过」，白推一条通知。
    ★ 后台任务绝不能因为一次异常整体退出：那样之后公告再也不推送，而且没有任何提示。
    """
    try:
        for room_id in all_room_ids():
            room_announcement(room_id)
    except Exception:  # noqa: BLE001
        pass
    for room_id in all_room_ids():
        _ANNOUNCE_WATCH_STAMPS[room_id] = _announcement_stamp(room_id)
    while True:
        await asyncio.sleep(ANNOUNCE_WATCH_INTERVAL)
        try:
            for room_id in all_room_ids():
                stamp = _announcement_stamp(room_id)
                if room_id not in _ANNOUNCE_WATCH_STAMPS:
                    _ANNOUNCE_WATCH_STAMPS[room_id] = stamp
                    continue
                if stamp == _ANNOUNCE_WATCH_STAMPS[room_id]:
                    continue
                reset_announcement_cache(room_id)
                text = room_announcement(room_id)
                # ★ 记新时间戳要放在「读正文」之后：读的过程可能顺手生成默认公告文件（又改一次时间戳），
                #   不重取的话下一轮会把「自己生成的文件」当成「老师改了公告」再通知一遍。
                _ANNOUNCE_WATCH_STAMPS[room_id] = _announcement_stamp(room_id)
                print(f"[房间公告] 检测到 {room_id} 的公告文件被外部修改，已推送给在线用户")
                await manager.broadcast(
                    {"type": "announcement_updated", "room_id": room_id, "announcement": text}, room_id=room_id
                )
                await manager.broadcast(
                    {"type": "system", "message": "📢 本房间公告已更新"}, room_id=room_id
                )
        except Exception:  # noqa: BLE001
            continue


# ==================== 房主 / 房间级禁言 ====================
# ★「房主」不是第四种 role。role 只有 管理员 / 普通用户 / 游客 三种，是全局单值；
#   而「房主」是「某个人 × 某个房间」的一条授权记录 —— 一个人可以在 A 房间是房主、
#   在 B 房间是普通用户，一个房间也可以有好几个房主。
#   所以它单独存一张表，判据是「(这个房间, 这个人) 在不在表里」，而不是「他的 role 是什么」。
#
# ★「禁言」同理：users.muted_until 是**全站禁言**（对所有房间生效，只有管理员能用）；
#   房主禁言只影响本房间，所以单独存 room_mutes 表，按 (房间, 人) 记一条。


def room_owners(room_id: str) -> list[str]:
    """某个房间当前的房主 user_id 列表（按任命先后）。

    表还没建出来时（测试里直接构造 Session、库是空文件）按「一个房主都没有」处理，不是错误。
    """
    try:
        with closing(get_db()) as db:
            rows = db.execute(
                "SELECT user_id FROM room_owners WHERE room_id = ? ORDER BY granted_at, user_id", (room_id,)
            ).fetchall()
    except sqlite3.Error:
        return []
    return [str(row[0]) for row in rows]


# ★ 内存缓存：room_id -> {user_id}。
#   在线列表每次刷新都会为每个人调一次 public()（那里要判断他是不是本房间房主），
#   一个房间几十个人就是几十次查询。任命/撤销时清掉对应房间的缓存即可，
#   房主只在管理员手动操作时才变，不存在「改了看不见」的窗口。
_OWNER_CACHE: dict[str, set[str]] = {}


def reset_room_owner_cache(room_id: str | None = None) -> None:
    """丢掉房主缓存（测试用；正式流程里由 set_room_owner 自己清）。"""
    with _ANNOUNCE_LOCK:
        if room_id is None:
            _OWNER_CACHE.clear()
            return
        _OWNER_CACHE.pop(room_id, None)


def room_owner_ids(room_id: str) -> set[str]:
    """某个房间的房主集合（走缓存）。"""
    with _ANNOUNCE_LOCK:
        cached = _OWNER_CACHE.get(room_id)
    if cached is None:
        cached = set(room_owners(room_id))
        with _ANNOUNCE_LOCK:
            _OWNER_CACHE[room_id] = cached
    return cached


def is_room_owner(user_id: str, room_id: str) -> bool:
    if not user_id or not room_id:
        return False
    return str(user_id) in room_owner_ids(room_id)


def can_manage_room(session: Session, room_id: str) -> bool:
    """能不能管这个房间：管理员可以管所有房间，房主只能管自己被授权的那个房间。"""
    if session.role == "admin":
        return True
    if session.role == "guest":
        return False
    return is_room_owner(session.user_id, room_id)


def set_room_owner(room_id: str, user_id: str, granted: bool) -> None:
    """任命或撤销一个房主。结果直接落库，重启后仍然有效。"""
    with closing(get_db()) as db:
        if granted:
            db.execute(
                "INSERT OR IGNORE INTO room_owners (room_id, user_id, granted_at) VALUES (?, ?, ?)",
                (room_id, user_id, time.time()),
            )
        else:
            db.execute("DELETE FROM room_owners WHERE room_id = ? AND user_id = ?", (room_id, user_id))
        db.commit()
    reset_room_owner_cache(room_id)


def user_room_mutes(user_id: str) -> dict[str, float]:
    """某个人当前所有「按房间」的禁言记录：room_id -> 截止时刻。过期的顺手清掉。

    表还没建出来时按「没有被禁言」处理（见 room_owners 的说明）。
    """
    now = time.time()
    try:
        with closing(get_db()) as db:
            db.execute("DELETE FROM room_mutes WHERE until <= ?", (now,))
            rows = db.execute("SELECT room_id, until FROM room_mutes WHERE user_id = ?", (user_id,)).fetchall()
            db.commit()
    except sqlite3.Error:
        return {}
    return {str(row[0]): float(row[1]) for row in rows}


def set_room_mute(room_id: str, user_id: str, until: float) -> None:
    """给某个人在某个房间禁言到 until。until <= 现在 就直接删掉这条记录（＝解除）。"""
    with closing(get_db()) as db:
        if until <= time.time():
            db.execute("DELETE FROM room_mutes WHERE room_id = ? AND user_id = ?", (room_id, user_id))
        else:
            db.execute(
                "INSERT OR REPLACE INTO room_mutes (room_id, user_id, until) VALUES (?, ?, ?)",
                (room_id, user_id, until),
            )
        db.commit()


# 商城上架物品：(id, 名称, 图标, 价格, 类型, 说明, 解锁等级)。
# 价格一律用「金币」；宠物消耗品使用 pet_food / pet_water / pet_clean 类型。
SHOP_SEED = [
    ("gift_rose", "玫瑰花束", "💐", 10, "gift", "送给好友，对方的魅力值 +2", 1),
    ("gift_diamond", "钻石", "💎", 25, "gift", "珍贵的闪亮礼物", 1),
    ("gift_cake", "生日蛋糕", "🎂", 30, "gift", "给对方一个生日祝福", 1),
    ("gift_trophy", "冠军奖杯", "🏆", 60, "gift", "稀有的荣誉道具", 1),
    ("badge_heart", "爱心徽章", "💖", 20, "badge", "佩戴后昵称旁出现爱心，人气满满", 1),
    ("badge_star", "星星徽章", "⭐", 30, "badge", "在昵称旁显示星星徽章", 2),
    ("badge_music", "音符徽章", "🎵", 35, "badge", "文艺青年的音符标记", 2),
    ("badge_leaf", "幸运徽章", "🍀", 45, "badge", "四叶草带来一整天的好运", 3),
    ("badge_flame", "火焰徽章", "🔥", 55, "badge", "热情似火，全场瞩目", 3),
    ("badge_medal", "奖牌徽章", "🏅", 70, "badge", "实力派的奖牌认证", 4),
    ("badge_rainbow", "彩虹徽章", "🌈", 90, "badge", "稀有的彩虹，最高级别的荣耀", 5),
    ("pet_food", "宠物口粮", "🍖", 8, "pet_food", "给宠物补充饥饿度", 1),
    ("pet_water", "清甜饮水", "🫗", 7, "pet_water", "给宠物补充口渴度", 1),
    ("pet_clean", "清洁用品", "🧼", 9, "pet_clean", "帮宠物恢复清洁度", 1),
]
BADGE_ICONS = {row[0]: row[2] for row in SHOP_SEED if row[4] == "badge"}
# 没手动佩戴时按价格从高到低自动展示拥有的最高档徽章。
BADGE_ORDER = [row[0] for row in sorted(SHOP_SEED, key=lambda row: -row[3]) if row[4] == "badge"]


def active_badge(badge_id: str, inventory: dict[str, int]) -> str:
    """算出该展示哪枚徽章：优先手动佩戴的那枚，否则自动取拥有的最高档。"""
    if badge_id and inventory.get(badge_id, 0) > 0:
        return BADGE_ICONS.get(badge_id, "")
    for item_id in BADGE_ORDER:
        if inventory.get(item_id, 0) > 0:
            return BADGE_ICONS[item_id]
    return ""


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def level_title(level: int) -> str:
    level = max(1, int(level))
    return LEVEL_TITLES.get(level, f"Lv.{level}高手")


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        salt_hex, digest_hex = encoded.split("$", 1)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 120_000)
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


class _ReusableConnection(sqlite3.Connection):
    """可复用的 SQLite 连接：close() 不真关，只回滚没提交完的事务。

    全项目 26 处调用点都写成 ``with closing(get_db()) as db:`` —— 复用连接后，
    这个 close() 在每个 with 块退出时仍会被调用一次，正好用来补回
    「原来连接一关、半截事务就自动回滚」的语义。少了这一步，
    某条异常路径漏掉的未提交事务会被下一个请求顺带提交，属于静默的数据污染。
    """

    def close(self) -> None:  # noqa: D401
        if self.in_transaction:
            try:
                self.rollback()
            except sqlite3.Error:
                pass


_db_local = threading.local()


def close_db() -> None:
    """真关掉本线程缓存的连接（测试收尾 / 需要换库时用）。"""
    conn = getattr(_db_local, "conn", None)
    _db_local.conn = None
    _db_local.path = None
    if conn is not None:
        try:
            sqlite3.Connection.close(conn)
        except sqlite3.Error:
            pass


def get_db() -> sqlite3.Connection:
    """按线程复用一条连接。

    原实现每次调用都 ``sqlite3.connect()`` 并重跑 ``PRAGMA journal_mode=WAL``，
    本机实测单次 2.0～2.4 ms，而连接复用后只重设 PRAGMA 只要 0.012 ms；
    一条聊天消息原本要开 2 次连接（``save_message`` 取房间名 + 写库），
    高并发下这是延迟雪崩最主要的放大器。

    按线程缓存（``threading.local``）：事件循环线程一条、别的线程各自一条，
    既省掉重开连接的开销，又不会出现两个线程抢同一条连接。
    ``DB_PATH`` 被改掉（测试会改）就自动重建，绝不会读到上一个库。
    """
    path = str(DB_PATH)
    conn = getattr(_db_local, "conn", None)
    if conn is not None:
        if getattr(_db_local, "path", None) == path:
            return conn
        close_db()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, factory=_ReusableConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # WAL + synchronous=NORMAL 是 SQLite 官方推荐的组合：commit 不必每个事务都 fsync。
    # 进程崩溃 / 断电最多丢掉最近几条已提交事务，库本身不会损坏（FULL 也不保证这一点）。
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _db_local.conn = conn
    _db_local.path = path
    return conn


def init_db() -> None:
    with closing(get_db()) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                nickname TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                muted_until REAL NOT NULL DEFAULT 0,
                kicked_until REAL NOT NULL DEFAULT 0,
                avatar TEXT NOT NULL DEFAULT '🙂',
                level INTEGER NOT NULL DEFAULT 1,
                exp INTEGER NOT NULL DEFAULT 0,
                coins INTEGER NOT NULL DEFAULT 100,
                charm INTEGER NOT NULL DEFAULT 0,
                inventory TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                sender_role TEXT NOT NULL,
                room TEXT NOT NULL DEFAULT '大厅',
                room_id TEXT NOT NULL DEFAULT 'lobby',
                recipient_id TEXT,
                sender_avatar TEXT NOT NULL DEFAULT '🙂',
                sender_badge TEXT NOT NULL DEFAULT '',
                sender_level INTEGER NOT NULL DEFAULT 1,
                text_style TEXT NOT NULL DEFAULT '',
                text_effect TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'text',
                activity_kind TEXT NOT NULL DEFAULT '',
                attachment_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rooms (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                icon TEXT NOT NULL DEFAULT '💬',
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shop_items (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                icon TEXT NOT NULL,
                price INTEGER NOT NULL,
                item_type TEXT NOT NULL DEFAULT 'avatar',
                description TEXT NOT NULL DEFAULT '',
                min_level INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS entry_cooldowns (
                client_ip TEXT PRIMARY KEY,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS room_owners (
                room_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                granted_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (room_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS room_mutes (
                room_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                until REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (room_id, user_id)
            );
            """
        )
        for table, column, definition in (
            ("users", "avatar", "TEXT NOT NULL DEFAULT '🙂'"),
            ("users", "kicked_until", "REAL NOT NULL DEFAULT 0"),
            ("users", "level", "INTEGER NOT NULL DEFAULT 1"),
            ("users", "exp", "INTEGER NOT NULL DEFAULT 0"),
            ("users", "coins", "INTEGER NOT NULL DEFAULT 100"),
            ("users", "charm", "INTEGER NOT NULL DEFAULT 0"),
            ("users", "inventory", "TEXT NOT NULL DEFAULT '[]'"),
            ("messages", "room_id", "TEXT NOT NULL DEFAULT 'lobby'"),
            ("messages", "recipient_id", "TEXT"),
            ("messages", "sender_avatar", "TEXT NOT NULL DEFAULT '🙂'"),
            ("messages", "sender_badge", "TEXT NOT NULL DEFAULT ''"),
            ("messages", "sender_level", "INTEGER NOT NULL DEFAULT 1"),
            ("messages", "text_style", "TEXT NOT NULL DEFAULT ''"),
            ("messages", "text_effect", "TEXT NOT NULL DEFAULT ''"),
            ("messages", "activity_kind", "TEXT NOT NULL DEFAULT ''"),
            ("messages", "attachment_json", "TEXT"),
            ("shop_items", "min_level", "INTEGER NOT NULL DEFAULT 1"),
            ("users", "badge_id", "TEXT NOT NULL DEFAULT ''"),
            ("users", "violations", "INTEGER NOT NULL DEFAULT 0"),
            ("users", "violations_at", "REAL NOT NULL DEFAULT 0"),
            ("users", "last_income_at", "REAL NOT NULL DEFAULT 0"),
            ("users", "real_name", "TEXT NOT NULL DEFAULT ''"),
            ("users", "class_name", "TEXT NOT NULL DEFAULT ''"),
            ("users", "bio", "TEXT NOT NULL DEFAULT ''"),
            ("users", "pet_type", "TEXT NOT NULL DEFAULT ''"),
            ("users", "pet_hunger", "REAL NOT NULL DEFAULT 100"),
            ("users", "pet_thirst", "REAL NOT NULL DEFAULT 100"),
            ("users", "pet_cleanliness", "REAL NOT NULL DEFAULT 100"),
            ("users", "pet_updated_at", "REAL NOT NULL DEFAULT 0"),
        ):
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        db.executemany(
            "INSERT OR IGNORE INTO rooms (id, name, icon, description, created_at) VALUES (?, ?, ?, ?, ?)",
            [
                ("lobby", "综合大厅", "🌟", "大家随意聊聊吧", now_text()),
                ("games", "游戏讨论", "🎮", "聊聊最近在玩的游戏", now_text()),
                ("music", "音乐电台", "🎵", "分享喜欢的音乐", now_text()),
                ("water", "灌水乐园", "💧", "天南海北，随便聊点什么", now_text()),
                ("study", "学习交流", "📚", "作业难题、学习方法都可以在这里问", now_text()),
            ],
        )
        # 只把老库里仍是默认名的「音乐房间」改过来，用户自己改过的名字不动。
        db.execute("UPDATE rooms SET name = ? WHERE id = 'music' AND name = ?", ("音乐电台", "音乐房间"))
        db.execute("CREATE INDEX IF NOT EXISTS messages_room_idx ON messages(room_id, id)")
        db.execute("CREATE INDEX IF NOT EXISTS messages_private_idx ON messages(sender_id, recipient_id, id)")
        db.executemany(
            "INSERT OR IGNORE INTO shop_items (id, name, icon, price, item_type, description, min_level) VALUES (?, ?, ?, ?, ?, ?, ?)",
            SHOP_SEED,
        )
        # 历史遗留：送礼消息被写上了 recipient_id，会被房间历史查询过滤掉，这里一次性修正。
        db.execute("UPDATE messages SET recipient_id = NULL WHERE kind IN ('gift', 'use_item') AND room_id <> 'private' AND recipient_id IS NOT NULL")
        existing = db.execute("SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO users (username, password_hash, nickname, role, created_at) VALUES (?, ?, ?, 'admin', ?)",
                (ADMIN_USERNAME, hash_password(ADMIN_PASSWORD), "管理员", now_text()),
            )
        db.commit()
    # 老版本只有一份全局公告（根目录的 公告.txt）。搬进「公告/lobby.txt」，只做一次。
    _migrate_legacy_announcement()


class Credentials(BaseModel):
    username: str = Field(min_length=2, max_length=24)
    password: str = Field(min_length=3, max_length=64)
    nickname: str | None = Field(default=None, min_length=1, max_length=20)


class GuestRequest(BaseModel):
    nickname: str = Field(min_length=1, max_length=20)


class ProfileUpdate(BaseModel):
    """改资料。字段全部可选：只传要改的那几项；没传、以及传了但和现在一样的都不计费。"""

    avatar: str | None = None
    nickname: str | None = None
    real_name: str | None = None
    class_name: str | None = None
    bio: str | None = None
    old_password: str | None = None
    new_password: str | None = None


class ProfileViewRequest(BaseModel):
    """查看他人资料。"""

    user_id: str = Field(min_length=1, max_length=64)


class PetAdoptRequest(BaseModel):
    pet_type: str = Field(min_length=1, max_length=16)


class PetActionRequest(BaseModel):
    action: str = Field(min_length=1, max_length=16)


class AnnouncementUpdate(BaseModel):
    """改某个房间的公告。内容长度先放到 400 字由接口自己判，
    这样超长时能回一句「最多 200 字」而不是 pydantic 那句看不懂的报错。"""

    room_id: str = Field(default="lobby", min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=400)


class RoomOwnerRequest(BaseModel):
    """任命 / 撤销房主。"""

    room_id: str = Field(default="lobby", min_length=1, max_length=32)
    user_id: str = Field(min_length=1, max_length=64)


class RecycleItem(BaseModel):
    item_id: str = Field(min_length=1, max_length=100)
    quantity: int = Field(strict=True, ge=1, le=1_000_000)


class RecycleRequest(BaseModel):
    items: list[RecycleItem] = Field(min_length=1, max_length=100)


class Session:
    def __init__(self, token: str, user_id: str, nickname: str, role: str, username: str | None = None, profile: dict[str, Any] | None = None, client_ip: str | None = None):
        self.token = token
        self.user_id = user_id
        self.nickname = nickname
        self.role = role
        self.username = username
        self.client_ip = client_ip or ""
        profile = profile or {}
        self.muted_until = float(profile.get("muted_until", 0) or 0)
        self.kicked_until = float(profile.get("kicked_until", 0) or 0)
        self.avatar = profile.get("avatar", "🙂") or "🙂"
        self.badge_id = str(profile.get("badge_id") or "")
        self.level = int(profile.get("level", 1))
        self.exp = int(profile.get("exp", 0))
        # 游客没有数据库记录，金币和道具无法保存；因此不发起始金币与道具，避免反复重进白嫖。
        self.coins = int(profile.get("coins", 0 if role == "guest" else 100))
        self.charm = int(profile.get("charm", 0))
        parsed_inventory: Any = None
        raw_inventory = profile.get("inventory", "")
        try:
            parsed_inventory = json.loads(raw_inventory) if isinstance(raw_inventory, str) and raw_inventory else raw_inventory
        except (TypeError, ValueError):
            # ValueError 已经覆盖了 json.JSONDecodeError —— 坏 JSON 不该让这局会话起不来。
            parsed_inventory = None
        if isinstance(parsed_inventory, list):
            self.inventory = {str(item): 1 for item in parsed_inventory}
        elif isinstance(parsed_inventory, dict):
            # ★ 数量可能是坏值（手工改过库、老版本的格式）。**只丢坏的那一条**，其余照常恢复：
            #   整栏清空会顺手抹掉没坏的物品；而把异常抛出去更糟 —— 这里是登录路径，
            #   用户会一直卡在「登录失败」，他什么错都没犯。
            #   （`int("abc")` 抛的是 ValueError，早先只兜 TypeError/JSONDecodeError → 登录 500。）
            self.inventory = {}
            for key, value in parsed_inventory.items():
                try:
                    self.inventory[str(key)] = max(0, int(value))
                except (TypeError, ValueError):
                    continue
        else:
            self.inventory = {}
        if not self.inventory and role != "guest" and not isinstance(parsed_inventory, dict):
            self.inventory = dict(STARTER_INVENTORY)
        self.room_id = "lobby"
        # 按房间的禁言：room_id -> 截止时刻。只有房主施加的禁言写在这里；
        # 全站禁言（上面那个 muted_until）对所有房间生效。见 muted_until_in()。
        self.room_mutes: dict[str, float] = user_room_mutes(user_id)
        self.websocket: WebSocket | None = None
        # 违规计数 + 「上一次违规的时刻」，两者一起构成时间窗判定（见 apply_violation_penalty）。
        # 会写进 users 表：只放内存的话，登出重登、或会话空闲一小时被清理就归零了，
        # 等于给学生留了一条「登出再登入」的无限规避路径。游客没有数据库记录，仍只算内存。
        self.violations = int(profile.get("violations", 0) or 0)
        self.violations_at = float(profile.get("violations_at", 0) or 0)
        self.last_seen = time.time()
        # 大喇叭上次使用时间，用于限制同一人的喊话频率（见 interactions.HORN_COOLDOWN）。
        self.last_horn = 0.0
        # 广播发送队列与消费它的协程：建立 WebSocket 时创建，断开时销毁。
        # 有了它，广播方只需同步塞队列，不会再被某个卡住的客户端拖住（见 ConnectionManager）。
        self.outbox: asyncio.Queue | None = None
        self.sender_task: asyncio.Task | None = None
        # ---- 防刷屏（常量见文件开头同名区域）----
        # 发言时刻的滑动窗口。只放内存：重连清空也无妨，重连本身就要花时间。
        self.speak_times: deque[float] = deque()
        # 最近说过的话，用来抓「连着发同样的内容」；A B A B 这种交替刷屏也能抓到。
        self.recent_speaks: deque[str] = deque(maxlen=SPEAK_REPEAT_WINDOW)
        # 上一次「靠说话拿到金币」的时刻。会落库：只放内存的话，重连一次就重置了，
        # 等于给「重连刷金币」留了一道门。
        self.last_income_at = float(profile.get("last_income_at", 0) or 0)
        # 各类拒收提示上次发出的时刻，用来给提示本身节流。
        self.speak_notices: dict[str, float] = {}
        # ---- 个人资料（真实姓名 / 班级 / 个性签名）----
        # ★ 真实姓名和班级刻意不进 public()：public() 会在每次在线列表刷新时广播给全房间，
        #   放进去就等于免费公开，「花 1 金币查看资料」立刻就形同虚设。
        #   它们只随 self_public()（只发给本人）和 /api/profile/view（收费）下发。
        #   个性签名是公开的（要显示在在线列表里），走 public()，见那里的说明。
        self.real_name = str(profile.get("real_name") or "")
        self.class_name = str(profile.get("class_name") or "")
        self.bio = str(profile.get("bio") or "")
        self.pet_type = str(profile.get("pet_type") or "") if role != "guest" else ""
        def pet_number(key: str) -> float:
            try:
                return float(profile.get(key, 100))
            except (TypeError, ValueError):
                return 100.0
        self.pet_hunger = max(0.0, min(100.0, pet_number("pet_hunger")))
        self.pet_thirst = max(0.0, min(100.0, pet_number("pet_thirst")))
        self.pet_cleanliness = max(0.0, min(100.0, pet_number("pet_cleanliness")))
        self.pet_updated_at = float(profile.get("pet_updated_at", 0) or 0) or time.time()
        # 加入时间，只在资料卡里展示。
        self.created_at = str(profile.get("created_at") or "")
        # 本次登录里已经付过费看过的人：同一人对同一人只扣一次。
        # 只放内存，重连即清空（这是「解锁」式语义，不是「按次」）。
        self.viewed_profiles: set[str] = set()

    def pet_state(self, now: float | None = None) -> dict[str, Any] | None:
        """返回按当前时间折算后的宠物状态，不改变内存或数据库。"""
        if not self.pet_type or self.role == "guest":
            return None
        now = float(now if now is not None else time.time())
        elapsed_minutes = max(0.0, now - self.pet_updated_at) / 60.0
        return {
            "type": self.pet_type,
            "name": PET_TYPES.get(self.pet_type, self.pet_type),
            "hunger": max(0.0, min(100.0, self.pet_hunger - elapsed_minutes * PET_DECAY_PER_MINUTE["hunger"])),
            "thirst": max(0.0, min(100.0, self.pet_thirst - elapsed_minutes * PET_DECAY_PER_MINUTE["thirst"])),
            "cleanliness": max(0.0, min(100.0, self.pet_cleanliness - elapsed_minutes * PET_DECAY_PER_MINUTE["cleanliness"])),
            "updated_at": now,
        }

    def refresh_pet(self, now: float | None = None) -> dict[str, Any] | None:
        """把随时间下降后的状态落回会话，供动作和持久化前调用。"""
        state = self.pet_state(now)
        if not state:
            return None
        self.pet_hunger = state["hunger"]
        self.pet_thirst = state["thirst"]
        self.pet_cleanliness = state["cleanliness"]
        self.pet_updated_at = state["updated_at"]
        return state

    def muted_until_in(self, room_id: str | None = None) -> float:
        """这个人在某个房间的禁言截止时刻。

        全站禁言（self.muted_until）对所有房间生效；房主施加的禁言只写进
        self.room_mutes[房间] —— 所以「在 A 房间被房主禁言，去 B 房间照常说话」。
        取两者中较晚的那个。
        """
        room = room_id or self.room_id
        return max(self.muted_until, float(self.room_mutes.get(room, 0.0)))

    def badge_icon(self) -> str:
        return active_badge(self.badge_id, self.inventory)

    def public(self) -> dict[str, Any]:
        """发给房间内所有人的公开信息，刻意不含物品栏。

        ★ 个性签名在这里（跟着在线列表免费广播给全房间）：它本来就是「给人看的」，
          和昵称、头像一个性质，要花钱才能看一眼反而奇怪。
          真实姓名和班级不在这里 —— 那两项才是花 1 金币才能看的。

        ★「是不是本房间房主」也在这里：谁有权管这个房间，大家本来就该看得见，
          不是什么要花金币的秘密。注意它只表示「这个人在**当前这个房间**是不是房主」——
          同一个人在别的房间可能就是个普通用户，所以换房间后广播的名单会重新算。
        """
        return {
            "id": self.user_id,
            "nickname": self.nickname,
            "role": self.role,
            "avatar": self.avatar,
            "level": self.level,
            "level_title": level_title(self.level),
            "exp": self.exp,
            "coins": coins_display(self),
            "coins_unlimited": has_unlimited_coins(self),
            "charm": self.charm,
            "room_id": self.room_id,
            "badge": self.badge_icon(),
            "badge_id": self.badge_id,
            "bio": self.bio,
            "online": self.websocket is not None,
            # 按房间算的禁言截止时刻：在线列表上那个「禁言中」标记得跟房间走 ——
            # 他在别的房间被房主禁言，这个房间不该显示成禁言。
            "muted_until": self.muted_until_in(self.room_id),
            # 他在「当前这个房间」是不是房主（管理员不算，管理员本来就能管所有房间）。
            "owner": is_room_owner(self.user_id, self.room_id),
        }

    def self_public(self) -> dict[str, Any]:
        """只发给本人的完整信息：额外带上物品栏，以及要回填到设置表单的资料字段。

        ★ 这里是「只发给本人」的通道（ready / /api/me / shop_result / interaction_result）。
          真实姓名、班级、账号名都只走这条路和收费的 /api/profile/view，
          绝不能挪进 public() —— 那条会广播给全房间。
          （个性签名是例外：它是公开的，已经在 public() 里带着了，这里不再重复。）
        """
        pet = self.pet_state()
        return {
            **self.public(),
            "inventory": self.inventory,
            "username": self.username or "",
            "real_name": self.real_name,
            "class_name": self.class_name,
            "created_at": self.created_at,
            "pet": pet,
            "pet_min_level": PET_MIN_LEVEL,
        }

    async def send(self, payload: dict[str, Any]) -> None:
        """给这个人发消息的统一出口。

        有发送队列就走队列 —— 这样「发给本人的消息」与「房间广播」共用同一个
        消费协程，顺序有保证，也不会出现两条协程同时往一个 socket 写。
        没有队列（连接还没建立、或正在拆除）就直接写 socket。
        """
        queue = self.outbox
        if queue is None:
            if self.websocket is not None:
                await self.websocket.send_json(payload)
            return
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            # 已经积压到上限：这条也发不出去了，发送协程会负责把这个连接断开。
            pass


# 每个连接的广播积压上限，以及单条消息的发送超时。
# 这两个闸门合起来保证：一个客户端卡住，最多拖慢它自己，不会再拖住整个房间。
BROADCAST_QUEUE_SIZE = 256
BROADCAST_SEND_TIMEOUT = 5.0
# 关连接的超时。对端卡死时 close() 自己也会挂在写缓冲上，
# 不设上限就会把「新建连接 / 管理员操作 / 断开慢客户端」这些调用方一起拖住。
SOCKET_CLOSE_TIMEOUT = 2.0


class ConnectionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.lock = asyncio.Lock()
        # 兜底清理由 broadcast 触发的「断开慢客户端」任务，避免被垃圾回收。
        self._janitors: set[asyncio.Task] = set()

    async def add(self, session: Session, websocket: WebSocket) -> None:
        previous = self.sessions.get(session.user_id)
        if previous and previous.websocket and previous.websocket is not websocket:
            # 先把旧连接的发送协程停掉再关 socket：否则两条协程会同时往同一个 socket 写。
            old_socket = previous.websocket
            await self.remove(previous, old_socket)
            try:
                await close_socket_safely(old_socket, 4004, "账号在另一个窗口进入")
            except Exception:  # noqa: BLE001
                pass
        async with self.lock:
            existing = session.sender_task
            if existing is not None and not existing.done():
                existing.cancel()
            session.websocket = websocket
            queue: asyncio.Queue = asyncio.Queue(maxsize=BROADCAST_QUEUE_SIZE)
            session.outbox = queue
            session.sender_task = asyncio.create_task(self._sender_loop(session, queue))
            self.sessions[session.user_id] = session

    async def remove(self, session: Session, websocket: WebSocket | None = None) -> None:
        async with self.lock:
            if websocket is not None and session.websocket is not websocket:
                return
            if self.sessions.get(session.user_id) is session:
                self.sessions.pop(session.user_id, None)
            session.websocket = None
            task = session.sender_task
            session.sender_task = None
            session.outbox = None
            session.last_seen = time.time()
        # 发送协程自己收尾时也会走到这里，此时不能取消自己（取消会打断正在做的清理）。
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _sender_loop(self, session: Session, queue: asyncio.Queue) -> None:
        """每条连接一个发送协程：队列里的消息由它独占写进 socket。

        广播方只负责「塞队列」（同步操作、瞬间返回），真正写网络的事情在这里做，并且带超时。
        一个学生网络卡住时，只体现在他自己队列的深度上，不会再拖住整个房间。
        """
        try:
            while True:
                payload = await queue.get()
                websocket = session.websocket
                if websocket is None:
                    continue
                try:
                    await asyncio.wait_for(websocket.send_json(payload), BROADCAST_SEND_TIMEOUT)
                except asyncio.TimeoutError:
                    await self._drop_slow(session, "发送超时")
                    return
                except Exception:  # noqa: BLE001
                    await self.remove(session, websocket)
                    return
        except asyncio.CancelledError:
            raise

    async def _drop_slow(self, session: Session, reason: str) -> None:
        """断开一个已经跟不上的客户端（发送超时，或积压超过上限）。

        必须**先摘出连接表、再关 socket**：``close()`` 自己也会卡在写缓冲上
        （对端就是不读的时候），要是指望它成功之后才摘除，
        这个学生会一直占着在线名单、并且继续被塞广播。
        """
        try:
            websocket = session.websocket
            await self.remove(session, websocket)
            if websocket is not None:
                await close_socket_safely(websocket, 4008, reason)
        except Exception:  # noqa: BLE001
            pass

    async def drain(self) -> None:
        """等所有发送协程与兜底任务收尾。

        关服务、以及测试收尾时调用。这些任务平时靠 ``remove()`` 顺手取消，
        但最后一步总得有个人等它们真的结束 —— 把「未完成的任务」丢给 asyncio
        去清理，关事件循环那一步就可能卡在等它们上。
        """
        while True:
            tasks: list[asyncio.Task] = [
                s.sender_task
                for s in self.sessions.values()
                if s.sender_task is not None and not s.sender_task.done()
            ]
            tasks += [t for t in self._janitors if not t.done()]
            if not tasks:
                return
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)

    async def broadcast(self, payload: dict[str, Any], room_id: str | None = None) -> None:
        """把消息塞进每个订阅者的发送队列，全程不写网络。

        原实现是顺序 ``await send_json``：任何一个客户端 TCP 缓冲区满，整条广播就卡在它身上，
        同房间其他人一起等 —— 一个学生网络差能拖垮一个班。改成塞队列之后本函数没有 await
        挂起点，慢客户端的背压只体现在自己的队列深度上。
        """
        for session in list(self.sessions.values()):
            if not session.websocket or (room_id is not None and session.room_id != room_id):
                continue
            queue = session.outbox
            if queue is None:
                # 没有发送队列（测试里手工塞进 sessions 的假会话，或连接正在拆除中）：
                # 退回直发，保持原有行为。
                try:
                    await session.send(payload)
                except Exception:  # noqa: BLE001
                    await self.remove(session)
                continue
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # 积压到上限说明这个客户端已经严重跟不上了：断开它，避免内存无界增长。
                task = asyncio.create_task(self._drop_slow(session, "消息积压过多"))
                self._janitors.add(task)
                task.add_done_callback(self._janitors.discard)

    def users(self, room_id: str | None = None) -> list[dict[str, Any]]:
        return [s.public() for s in self.sessions.values() if s.websocket and (room_id is None or s.room_id == room_id)]


def presence_payload() -> dict[str, Any]:
    """跨房间的「谁在线 + 每个房间几个人」。

    ★ 这两样信息都**与房间无关**，所以必须能广播给**所有**房间，不能只发当事人所在的那一间：
      私聊是跨房间的（后端按 user_id 找人，不看房间），对方在别的房间退出时，
      只发他本房间的话，另一个房间的学生会一直看到「私聊对象在线」（实测过）。
      左侧房间列表的人数同理 —— 别的房间人走了，那个数字也得跟着变。
    """
    online_ids: list[str] = []
    room_counts: dict[str, int] = {}
    for session in manager.sessions.values():
        if not session.websocket:
            continue
        online_ids.append(str(session.user_id))
        room_counts[session.room_id] = room_counts.get(session.room_id, 0) + 1
    return {"type": "presence", "online_ids": online_ids, "room_counts": room_counts}


def users_payload(room_id: str) -> dict[str, Any]:
    """在线名单的统一下发格式（本房间名单 + 跨房间的在线信息）。

    ★ 除了本房间的名单，还要带上**全站在线的人的 id**：私聊是**跨房间**的（后端按 user_id 找人，
      不看房间），前端如果只看本房间名单，对方一换房间就会被误判成「已离线、发不出消息」。
      room_counts 一起带上：前端收到就能把左侧**所有**房间的人数一次对齐，而不是只改当前那一间。
    """
    payload = presence_payload()
    payload["type"] = "users"
    payload["users"] = manager.users(room_id)
    return payload


async def push_presence() -> None:
    """把跨房间的在线信息推给**所有**房间。

    每次有人加入 / 离开 / 换房间 / 被踢都要调一次 —— 只更新「他所在的那个房间」是不够的。
    """
    await manager.broadcast(presence_payload())


async def direct_send(websocket: WebSocket, payload: dict[str, Any]) -> None:
    """明确的「直接写 socket」出口。

    只在两类路径上用：① 会话还没建立（token 已失效）；② 马上就关连接（被踢出）。
    常规消息一律走 ``Session.send()`` —— 它把消息排进该连接的发送队列，
    与房间广播共用同一个消费协程，顺序有保证，也不会两条协程抢同一个 socket。
    """
    try:
        await websocket.send_json(payload)
    except Exception:  # noqa: BLE001
        pass


async def close_socket_safely(websocket: WebSocket, code: int, reason: str = "") -> None:
    """关连接时加一道超时。

    对端「只连不读」时，``close()`` 要发的关闭帧同样会卡在写缓冲上 ——
    实测过：不加超时的话，一个不读的学生会一直挂在在线名单里，因为负责断开他的协程
    正堵在 close() 里出不来。调用方可能是新建连接、管理员操作或断开慢客户端，
    都不该被这件事拖住。
    """
    try:
        await asyncio.wait_for(websocket.close(code=code, reason=reason), SOCKET_CLOSE_TIMEOUT)
    except Exception:  # noqa: BLE001
        pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    from interactions import init_interactions, watch_rooms
    init_interactions()
    task = asyncio.create_task(watch_rooms())
    # 公告文件是老师直接用记事本维护的，页面保存只是另一条入口 ——
    # 所以要有个后台任务盯着文件，改完存盘就让在线的人立刻收到（见 watch_announcements）。
    announce_task = asyncio.create_task(watch_announcements())
    try:
        yield
    finally:
        for pending in (task, announce_task):
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
        await manager.drain()


app = FastAPI(title="局域网互动聊天室", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/avatars", StaticFiles(directory=AVATAR_DIR), name="avatars")
app.mount("/emojis", StaticFiles(directory=EMOJI_DIR), name="emojis")
manager = ConnectionManager()
sessions: dict[str, Session] = {}


SESSION_IDLE_SECONDS = 60 * 60


def purge_stale() -> None:
    """清理长时间没有 WebSocket 连接的会话与过期的进入冷却记录，避免长时间运行时内存和数据库只增不减。"""
    deadline = time.time() - SESSION_IDLE_SECONDS
    for token, session in list(sessions.items()):
        if not session.websocket and session.last_seen < deadline:
            sessions.pop(token, None)
    with closing(get_db()) as db:
        db.execute("DELETE FROM entry_cooldowns WHERE expires_at <= ?", (time.time(),))
        db.commit()


def check_entry(client_ip: str, user_id: str | None = None, role: str = "guest") -> None:
    if role == "admin":
        return
    with closing(get_db()) as db:
        until = 0.0
        if user_id:
            account = db.execute("SELECT kicked_until FROM users WHERE id = ?", (user_id,)).fetchone()
            if account:
                until = max(until, float(account[0] or 0))
        else:
            row = db.execute("SELECT expires_at FROM entry_cooldowns WHERE client_ip = ?", (client_ip,)).fetchone()
            until = float(row[0]) if row else 0
    remaining = until - time.time()
    if remaining > 0:
        raise HTTPException(status_code=403, detail=f"你或本机刚被请出聊天室，请 {max(1, int(remaining + 0.999))} 秒后再进入")


def user_from_token(token: str | None) -> Session:
    if not token or token not in sessions:
        raise HTTPException(status_code=401, detail="登录状态已失效")
    session = sessions[token]
    check_entry(session.client_ip, session.user_id, session.role)
    remaining = session.kicked_until - time.time()
    if remaining > 0:
        raise HTTPException(status_code=403, detail=f"你刚被请出聊天室，请 {max(1, int(remaining + 0.999))} 秒后再进入")
    return session


def room_info(room_id: str) -> sqlite3.Row | None:
    with closing(get_db()) as db:
        return db.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()


DEFAULT_AVATAR_OPTIONS = [
    {"value": "🙂", "label": "默认笑脸"},
    {"value": "😎", "label": "酷酷墨镜"},
    {"value": "🌟", "label": "闪亮星星"},
    {"value": "🦊", "label": "小狐狸"},
]


def avatar_options() -> list[dict[str, str]]:
    allowed = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    options: list[dict[str, str]] = []
    for path in sorted(AVATAR_DIR.iterdir(), key=lambda item: item.name.lower()):
        if path.is_file() and path.suffix.lower() in allowed:
            options.append({"value": f"/avatars/{path.name}", "label": path.stem[:24]})
    # 文件夹有图片时只显示自定义头像；没有图片时才回退到内置头像。
    return options or DEFAULT_AVATAR_OPTIONS.copy()


def emoji_options() -> list[dict[str, str]]:
    """读取自定义图片表情，为中文文件名和重名短代码生成稳定标识。"""
    allowed = {".png", ".gif", ".webp", ".jpg", ".jpeg"}
    options: list[dict[str, str]] = []
    used_names: set[str] = set()
    if not EMOJI_DIR.exists():
        return options
    for path in sorted(EMOJI_DIR.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file() or path.suffix.lower() not in allowed:
            continue
        # 短代码只允许安全字符，避免文件名里的标点破坏聊天标记解析。
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", path.stem).strip("-")[:40]
        if not name or name in used_names:
            suffix = hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:16]
            name = f"{name[:23] or 'emoji'}-{suffix}"
        used_names.add(name)
        options.append({
            "name": name,
            "label": path.stem[:24],
            "src": f"/emojis/{quote(path.name)}",
            "token": f":{name}:",
        })
    return options


# persist_session 写回 users 表的列清单。抽成常量是为了让静态检查能核对
# 「写回去的每一列都真实存在于表结构里」——散在 SQL 字符串里的话，加列时最容易漏掉迁移。
PERSISTED_USER_COLUMNS = (
    "exp", "level", "coins", "avatar", "inventory", "kicked_until", "charm",
    "badge_id", "last_income_at", "violations", "violations_at", "muted_until",
    "nickname", "real_name", "class_name", "bio",
    "pet_type", "pet_hunger", "pet_thirst", "pet_cleanliness", "pet_updated_at",
)


def persist_session(session: Session, password_hash: str | None = None) -> None:
    """把会话里会变的状态写回 users 表。

    改资料时「扣掉的金币」与「改后的资料」写在同一条 UPDATE 里：要么都生效、
    要么都不生效，不会出现「扣了金币但资料没改成」这种半截状态。
    """
    if session.role == "guest":
        return
    values: dict[str, Any] = {
        "exp": session.exp, "level": session.level, "coins": session.coins,
        "avatar": session.avatar, "inventory": json.dumps(session.inventory, ensure_ascii=False),
        "kicked_until": session.kicked_until, "charm": session.charm, "badge_id": session.badge_id,
        "last_income_at": session.last_income_at, "violations": session.violations,
        "violations_at": session.violations_at, "muted_until": session.muted_until,
        "nickname": session.nickname,
        "real_name": session.real_name, "class_name": session.class_name, "bio": session.bio,
        "pet_type": session.pet_type, "pet_hunger": session.pet_hunger,
        "pet_thirst": session.pet_thirst, "pet_cleanliness": session.pet_cleanliness,
        "pet_updated_at": session.pet_updated_at,
    }
    columns = list(PERSISTED_USER_COLUMNS)
    if password_hash is not None:
        columns.append("password_hash")
        values["password_hash"] = password_hash
    assignments = ", ".join(f"{name} = ?" for name in columns)
    with closing(get_db()) as db:
        db.execute(f"UPDATE users SET {assignments} WHERE id = ?",
                   [values[name] for name in columns] + [session.user_id])
        db.commit()


def item_catalog() -> dict[str, dict[str, Any]]:
    catalog = dict(STARTER_ITEMS)
    with closing(get_db()) as db:
        for row in db.execute("SELECT * FROM shop_items").fetchall():
            catalog[row["id"]] = dict(row)
    return catalog


# ---- 活动消息分两档 ----
# 判据是「旁观者会不会真的关心」，而不是「它属于哪个功能」：
#   notice —— 低频、一次性的（开局、开奖、红包发出、答对、大喇叭、真心话、大冒险），
#             留在聊天区单独占一行；
#   stream —— 每人都要刷一条的流水（抢红包、投票、接龙、抽奖），前端把这些收成
#             一条「活动消息 N 条」折叠带，不挡真人的话。
# 没列进来的活动类型一律按 notice 处理：以后新加玩法时「漏配」只会退化成不折叠，
# 绝不会把消息藏起来 —— 这种错比误折叠安全得多。
#
# ⚠️ 真心话 / 大冒险在 notice 档、不在折叠档（2026-10-03 改）：它们是**等人回应**的事件，
# 不是「看一眼就过」的流水。折起来别人根本不知道该接话，题就废了 —— 折叠带的收益
# （不挡真人的话）远小于「有人回应」的前提。
ACTIVITY_STREAM_KINDS = frozenset({
    "lottery", "wheel", "redpack_claim", "prediction_vote", "idiom",
})


def activity_tier(activity_kind: str) -> str:
    """把活动类型翻成显示档位。全项目只有这一处判据，实时消息与历史记录共用。"""
    return "stream" if activity_kind in ACTIVITY_STREAM_KINDS else "notice"


def with_activity_tier(message: dict[str, Any]) -> dict[str, Any]:
    """给活动消息补上显示档位。

    实时广播的消息来自 save_message()、刷新页面取的历史来自 row_to_message()，
    两条路都必须过这一道 —— 否则会出现「刚玩时是折叠的、按 F5 之后又散开」。
    """
    if message.get("kind") == "activity":
        message["activity_tier"] = activity_tier(str(message.get("activity_kind") or ""))
    return message


def row_to_message(row: sqlite3.Row) -> dict[str, Any]:
    message = dict(row)  # dict() 是新对象，下面就地改写不会污染内存里的消息
    current_avatar = message.pop("current_sender_avatar", None)
    if current_avatar is not None:
        message["sender_avatar"] = current_avatar
    attachment_json = message.pop("attachment_json", None)
    try:
        message["attachment"] = json.loads(attachment_json) if attachment_json else None
    except json.JSONDecodeError:
        message["attachment"] = None
    # 下发历史时再过一遍词库：这样新加的违禁词对旧消息同样生效。
    # 昵称也一起过滤，兜住历史库里可能存在的违规昵称。
    for field in ("content", "sender_name"):
        value = message.get(field)
        if isinstance(value, str) and value:
            message[field] = moderate_content(value)[0]
    # 档位跟着消息一起下发；非活动消息不带这个字段，前端便无从误折叠。
    return with_activity_tier(message)


def income_gate_open(session: Session, moment: float | None = None) -> bool:
    """「说话 / 送礼」这道收益闸门此刻是不是开着的。

    ★ 一次赠送会同时产生两笔收益：送出方的金币与经验（在 save_message 里算）、
      收礼方的魅力（在 award_gift_charm 里算）。两处**必须共用这一个判据** ——
      魅力当初就是「自己另写一份判断」才完全没有闸门，两个人对送同一件礼物
      （道具绕一圈回到手里）就能无限刷：实测 10 个来回 = 双方各 +20。
      所以这道比较式**整份源码里只允许出现这一处**（L1 静态判据会盯着）。
    """
    return (moment if moment is not None else time.time()) - session.last_income_at >= SPEAK_INCOME_INTERVAL


def award_gift_charm(sender: Session, target: Session, item: dict[str, Any]) -> int:
    """给收礼方加魅力，返回实际加了多少（0 = 闸门没开）。

    魅力是物品价值换算来的（price // 5，最低 1）。这是**唯一**写 charm 的地方。
    """
    if not income_gate_open(sender):
        return 0
    gain = max(1, int(item.get("price", 0)) // 5)
    target.charm += gain
    return gain


def save_message(session: Session, content: str, kind: str = "text", room_id: str | None = None, recipient_id: str | None = None, attachment: dict[str, Any] | None = None, text_style: str = "", text_effect: str = "", award_income: bool = True, activity_kind: str = "") -> dict[str, Any]:
    room_id = room_id or session.room_id
    room = room_info(room_id)
    room_name = room["name"] if room else "私聊"
    created = now_text()
    previous_level = session.level
    # 只有「能被反复触发、又不消耗本人金币」的动作才需要收益冷却：这样刷屏换不来金币。
    # ★ 送礼原先「不套这道闸门」，理由是「送礼物本身在消耗道具」—— 但道具有**收礼方**，
    #   两个人来回送同一件礼物时道具根本没消失，每送一次双方各 +1 金币 / +2 经验，就是一台印钞机。
    #   所以送礼和说话走同一道闸门。
    if kind in ("text", "private", "gift"):
        moment = time.time()
        # ★ 判据走 income_gate_open —— 跟送礼那边给收礼方加魅力用的是**同一个**函数，
        #   不允许在这里再手写一遍比较式（两处各写一份，迟早有一处忘了改）。
        earn_now = award_income and income_gate_open(session, moment)
        if earn_now:
            session.last_income_at = moment
    elif kind in ("use_item", "coin_gift"):
        # ★「使用头像 / 佩戴徽章」**不消耗任何道具**，只改一个展示字段。
        #   给它收益等于白送：连点 10 次就是 +10 金币 / +20 经验（实测过）。所以一律不给。
        earn_now = False
    else:
        earn_now = award_income
    session.exp += 0 if kind == "activity" or not earn_now else 2
    session.level = 1 + session.exp // 50
    session.coins += 0 if kind == "activity" or not earn_now else 1
    level_reward = 0
    if session.level > previous_level:
        level_reward = 10 * (session.level - previous_level)
        session.coins += level_reward
    with closing(get_db()) as db:
        cursor = db.execute(
            "INSERT INTO messages (sender_id, sender_name, sender_role, room, room_id, recipient_id, sender_avatar, sender_badge, sender_level, text_style, text_effect, content, kind, activity_kind, attachment_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session.user_id, session.nickname, session.role, room_name, room_id, recipient_id, session.avatar, session.public()["badge"], session.level, text_style, text_effect, content, kind, activity_kind, json.dumps(attachment, ensure_ascii=False) if attachment else None, created),
        )
        if session.role != "guest":
            db.execute("UPDATE users SET exp = ?, level = ?, coins = ?, avatar = ?, inventory = ?, kicked_until = ?, charm = ?, badge_id = ?, last_income_at = ? WHERE id = ?", (session.exp, session.level, session.coins, session.avatar, json.dumps(session.inventory, ensure_ascii=False), session.kicked_until, session.charm, session.badge_id, session.last_income_at, session.user_id))
        db.commit()
        message_id = cursor.lastrowid
    return with_activity_tier({
        "id": message_id, "sender_id": session.user_id, "sender_name": session.nickname,
        "sender_role": session.role, "sender_avatar": session.avatar, "sender_badge": session.public()["badge"], "sender_level": session.level, "level_title": level_title(session.level), "room": room_name,
        "room_id": room_id, "recipient_id": recipient_id, "content": content,
        "kind": kind, "attachment": attachment, "created_at": created,
        "level_up": session.level > previous_level, "level_reward": level_reward,
        "text_style": text_style, "text_effect": text_effect, "activity_kind": activity_kind,
    })


VIOLATION_MUTE_THRESHOLD = 3
VIOLATION_MUTE_SECONDS = 60
# 违规计数的时间窗：只有落在同一个窗口内的违规才累计到禁言阈值。
# 原来没有任何时间窗，上午一句、中午一句、下午一句会被算成「短时间内连犯三次」。
VIOLATION_WINDOW_SECONDS = 10 * 60


def apply_violation_penalty(session: Session) -> int:
    """扣除违规发言金币并立即持久化，返回实际扣除数量。

    游客没有金币可扣，只扣金币等于毫无惩罚；因此额外累计违规次数，
    在 VIOLATION_WINDOW_SECONDS 这个时间窗内连犯达到阈值就临时禁言。
    计数、时刻与**禁言截止时刻**一并落库（都走 persist_session），学生登出重登也清不掉。
    """
    before = max(0, int(session.coins))
    deducted = 0 if has_unlimited_coins(session) else min(5, before)
    if not has_unlimited_coins(session):
        session.coins = before - deducted
    now = time.time()
    # 距上一次违规已经超出时间窗 → 重新开始计数（这才是「短时间内连犯」）。
    if now - session.violations_at > VIOLATION_WINDOW_SECONDS:
        session.violations = 0
    session.violations += 1
    session.violations_at = now
    if session.violations >= VIOLATION_MUTE_THRESHOLD:
        session.violations = 0
        session.violations_at = 0.0
        session.muted_until = max(session.muted_until, now + VIOLATION_MUTE_SECONDS)
    persist_session(session)
    return deducted


def violation_notice(deducted: int, session: Session | None = None) -> str:
    if session is not None and session.muted_until_in(session.room_id) > time.time():
        return f"⚠️ 你的发言包含违规内容，已自动屏蔽；短时间内多次违规，已被禁言 {VIOLATION_MUTE_SECONDS} 秒。"
    if deducted >= 5:
        return "⚠️ 你的发言包含违规内容，已自动屏蔽，并扣除 5 金币。"
    if deducted > 0:
        return f"⚠️ 你的发言包含违规内容，已自动屏蔽，并扣除剩余的 {deducted} 金币。"
    # 金币为 0 时不要说“扣除 0 金币”，那句话自相矛盾。
    return "⚠️ 你的发言包含违规内容，已自动屏蔽。请文明发言，多次违规将被禁言。"


def sanitize_text_format(raw_style: Any, raw_effect: Any) -> tuple[str, str]:
    """只保留一个文字格式；效果优先，避免红字和彩虹等叠加。"""
    valid_styles = ("blue", "purple", "red", "bold")
    style = next((part for part in str(raw_style).split() if part in valid_styles), "")
    effect = str(raw_effect) if raw_effect in ("glow", "rainbow") else ""
    return ("", effect) if effect else (style, "")


@app.get("/")
async def index() -> FileResponse:
    dist_index = STATIC_DIR / "dist" / "index.html"
    if not dist_index.exists():
        raise HTTPException(status_code=503, detail="前端资源还没构建，请在开发机执行 npm run build 后重启服务")
    return FileResponse(dist_index)


@app.post("/api/register")
async def register(payload: Credentials, request: Request) -> dict[str, Any]:
    check_entry(request.client.host if request.client else "unknown")
    username = payload.username.strip()
    nickname = (payload.nickname or username).strip()
    if not username or not nickname:
        raise HTTPException(status_code=400, detail="账号和昵称不能为空")
    if moderate_content(nickname)[1]:
        # 昵称会出现在在线列表、进入横幅和每条消息上，必须和正文一样审核。
        raise HTTPException(status_code=400, detail="昵称包含违规内容，请换一个再来")
    with closing(get_db()) as db:
        try:
            cursor = db.execute(
                "INSERT INTO users (username, password_hash, nickname, role, created_at) VALUES (?, ?, ?, 'user', ?)",
                (username, hash_password(payload.password), nickname, now_text()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="这个账号已经注册过了")
        db.commit()
        user_id = str(cursor.lastrowid)
    return create_session(user_id, username, nickname, "user", client_ip=request.client.host if request.client else None)


@app.post("/api/login")
async def login(payload: Credentials, request: Request) -> dict[str, Any]:
    with closing(get_db()) as db:
        row = db.execute("SELECT * FROM users WHERE username = ?", (payload.username.strip(),)).fetchone()
    if not row or not verify_password(payload.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="账号或密码不正确")
    check_entry(request.client.host if request.client else "unknown", str(row["id"]), row["role"])
    session_data = create_session(str(row["id"]), row["username"], row["nickname"], row["role"], dict(row), request.client.host if request.client else None)
    session = sessions.get(session_data["token"])
    if session is not None:
        # 复用已有会话时，也要把数据库里的禁言/踢出状态同步进来，并返回本人完整信息。
        session.muted_until = max(session.muted_until, float(row["muted_until"] or 0))
        session.kicked_until = max(session.kicked_until, float(row["kicked_until"] or 0))
        session.last_seen = time.time()
        session_data["user"] = session.self_public()
    return session_data


@app.post("/api/guest")
async def guest(payload: GuestRequest, request: Request) -> dict[str, Any]:
    nickname = payload.nickname.strip()
    client_ip = request.client.host if request.client else "unknown"
    check_entry(client_ip)
    if moderate_content(nickname)[1]:
        raise HTTPException(status_code=400, detail="昵称包含违规内容，请换一个再来")
    guest_id = f"guest-{secrets.token_hex(4)}"
    taken = {item.nickname for item in sessions.values()}
    if nickname in taken:
        # 游客昵称重名会让 @提醒 产生歧义，这里自动加序号区分。
        index = 2
        while f"{nickname}({index})" in taken:
            index += 1
        nickname = f"{nickname}({index})"[:20]
    return create_session(guest_id, None, nickname, "guest", client_ip=client_ip)


def create_session(user_id: str, username: str | None, nickname: str, role: str, profile: dict[str, Any] | None = None, client_ip: str | None = None) -> dict[str, Any]:
    # 兜底：昵称统一过一遍审核，防止历史库里已有的违规昵称继续出现在在线列表和每条消息上。
    nickname = moderate_content(nickname)[0]
    if role != "guest":
        if profile is None:
            # 注册完是「直接就进来了」，调用方还没查过库。这里补查一次，
            # 否则「加入时间」这类只在资料卡里用到的字段会是空的（真实浏览器里点开显示「未知」）。
            with closing(get_db()) as db:
                row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            profile = dict(row) if row else None
        existing = next((item for item in sessions.values() if item.user_id == user_id), None)
        if existing:
            # 同一账号再次进入时复用原会话，但要把进入 IP 与昵称刷新为最新一次，踢人按 IP 判定才准确。
            if client_ip:
                existing.client_ip = client_ip
            if nickname:
                existing.nickname = nickname
            existing.last_seen = time.time()
            return {"token": existing.token, "user": existing.self_public()}
    token = secrets.token_urlsafe(32)
    session = Session(token, user_id, nickname, role, username, profile, client_ip)
    sessions[token] = session
    return {"token": token, "user": session.self_public()}


@app.post("/api/logout")
async def logout(request: Request) -> dict[str, bool]:
    token = request.headers.get("X-Session-Token")
    if token:
        session = sessions.pop(token, None)
        if session:
            websocket = session.websocket
            await manager.remove(session)
            if websocket:
                await close_socket_safely(websocket, 1000)
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request) -> dict[str, Any]:
    session = user_from_token(request.headers.get("X-Session-Token"))
    return {"user": session.self_public()}


@app.post("/api/pet/adopt")
async def adopt_pet(payload: PetAdoptRequest, request: Request) -> dict[str, Any]:
    session = user_from_token(request.headers.get("X-Session-Token"))
    if session.role == "guest":
        raise HTTPException(status_code=403, detail="游客不能养宠物，请注册账号后再来")
    if session.level < PET_MIN_LEVEL:
        raise HTTPException(status_code=403, detail=f"等级达到 Lv.{PET_MIN_LEVEL} 后才能养宠物")
    pet_type = payload.pet_type.strip().lower()
    if pet_type not in PET_TYPES:
        raise HTTPException(status_code=400, detail="只能选择小鸡、小猫或小狗")
    has_pet = bool(session.pet_type)
    changing_pet = bool(session.pet_type and pet_type != session.pet_type)
    if changing_pet and not can_spend_coins(session, PET_CHANGE_COST):
        raise HTTPException(status_code=400, detail=f"更换宠物需要 {PET_CHANGE_COST} 金币，你只有 {session.coins} 金币")
    if has_pet:
        # 更换外观前先结算这段时间的自然衰减，三项状态不能因为换宠物被重置。
        session.refresh_pet()
        if changing_pet:
            spend_coins(session, PET_CHANGE_COST)
    else:
        session.pet_hunger = 100.0
        session.pet_thirst = 100.0
        session.pet_cleanliness = 100.0
    session.pet_type = pet_type
    session.pet_updated_at = time.time()
    persist_session(session)
    if changing_pet:
        message = f"已更换为{PET_TYPES[pet_type]}，扣除 {PET_CHANGE_COST} 金币，原来的状态已保留。"
    elif has_pet:
        message = f"你已经养着{PET_TYPES[pet_type]}了，状态保持不变。"
    else:
        message = f"已领养{PET_TYPES[pet_type]}，好好照顾它吧！"
    return {"user": session.self_public(), "message": message}


@app.post("/api/pet/action")
async def pet_action(payload: PetActionRequest, request: Request) -> dict[str, Any]:
    session = user_from_token(request.headers.get("X-Session-Token"))
    if session.role == "guest":
        raise HTTPException(status_code=403, detail="游客不能养宠物，请注册账号后再来")
    if session.level < PET_MIN_LEVEL:
        raise HTTPException(status_code=403, detail=f"等级达到 Lv.{PET_MIN_LEVEL} 后才能养宠物")
    if not session.pet_type:
        raise HTTPException(status_code=400, detail="请先领养一只宠物")
    action = payload.action.strip().lower()
    item_info = PET_ACTION_ITEMS.get(action)
    if not item_info:
        raise HTTPException(status_code=400, detail="这个宠物动作不存在")
    item_id, amount, label = item_info
    if session.inventory.get(item_id, 0) < 1:
        raise HTTPException(status_code=400, detail="对应用品用完了，请到等级商城购买")
    session.refresh_pet()
    if action == "feed":
        session.pet_hunger = min(100.0, session.pet_hunger + amount)
    elif action == "water":
        session.pet_thirst = min(100.0, session.pet_thirst + amount)
    else:
        session.pet_cleanliness = min(100.0, session.pet_cleanliness + amount)
    session.inventory[item_id] = session.inventory.get(item_id, 0) - 1
    persist_session(session)
    return {"user": session.self_public(), "message": f"{label}成功，{PET_TYPES[session.pet_type]}开心地回应了你。"}


def profile_card(target: Session, viewer: Session) -> dict[str, Any]:
    """「查看对方资料」返回的完整资料卡。

    ★ 真实姓名 / 班级刻意不放进 public()：那个会在每次在线列表刷新时广播给
      全房间，放进去就等于免费公开，收费查看立刻形同虚设。只有管理员能看到对方的
      账号名（学生之间互相看不到，少一处被人猜密码的入口）。
      （个性签名是公开的，public() 已经带着了，这里不用再写一遍。）
    """
    card = {
        **target.public(),
        "real_name": target.real_name,
        "class_name": target.class_name,
        "created_at": target.created_at,
        "online": target.websocket is not None,
    }
    if viewer.role == "admin":
        card["username"] = target.username or ""
    return card


def normalize_profile_text(raw: Any, label: str, limit: int) -> str:
    """真实姓名 / 班级 / 个性签名共用的清洗：去首尾空白、限长、过一遍词库。"""
    text = str(raw or "").strip()
    if len(text) > limit:
        raise HTTPException(status_code=400, detail=f"{label}最多 {limit} 个字")
    if text and moderate_content(text)[1]:
        raise HTTPException(status_code=400, detail=f"{label}里有不合适的词，改一下吧")
    return text


def normalize_class_name(raw: Any) -> str:
    """把班级归一化成「八年级3班」这种标准写法。

    允许：七八九年级 + 1~10 班，可以只写其一（「八年级」「3班」），也可以留空 —— 留空就是「不填」。
    兼容旧写法：「八（3）班」「8年级3班」「八年级 3 班」都认得出来，并归一化成标准格式。
    认不出来的自由文本直接拒绝 —— 前端下拉框只决定体验，**这里才是「不会乱」的保证**。
    """
    text = re.sub(r"[\s\u3000]+", "", str(raw or ""))
    if len(text) > CLASS_NAME_MAX_LENGTH:
        raise HTTPException(status_code=400, detail=f"班级最多 {CLASS_NAME_MAX_LENGTH} 个字")
    if not text:
        return ""

    grade = ""
    grade_matched = re.search(r"([0-9一二三四五六七八九]{1,3})年级", text)
    if grade_matched:
        # 写明了年级就必须是七八九：「一年级2班」这种不能放进来。
        grade = PROFILE_GRADE_ALIASES.get(grade_matched.group(1), "")
        if not grade:
            raise HTTPException(status_code=400, detail="年级只能是七年级、八年级或九年级")
    else:
        # 老写法「八（3）班」：年级后面直接跟括号，没有「年级」两个字。
        for chinese in ("七", "八", "九"):
            if text.startswith((f"{chinese}（", f"{chinese}(")):
                grade = f"{chinese}年级"
                break

    class_number = ""
    number_matched = re.search(r"(?<!\d)(\d{1,2})班", text)
    if not number_matched:
        number_matched = re.search(r"[（(](\d{1,2})[）)]", text)
    if number_matched:
        # 同理，写明了班级号就必须是 1~10 班：「八年级11班」「八年级0班」都拒掉，
        # 否则「11班」会被当成「1班」静静地改错。
        value = number_matched.group(1)
        if value not in PROFILE_CLASS_NUMBERS:
            raise HTTPException(status_code=400, detail="班级只能是 1 班到 10 班")
        class_number = f"{value}班"

    if not grade and not class_number:
        raise HTTPException(status_code=400, detail="班级请从下拉框里选「年级」和「班级」")
    return grade + class_number


@app.post("/api/profile")
async def profile(payload: ProfileUpdate, request: Request) -> dict[str, Any]:
    """修改自己的资料，按「实际改了几处」收金币。

    计费口径：昵称 / 头像 / 真实姓名 / 班级 / 个性签名 / 密码，每处 PROFILE_EDIT_COST 金币。
    传了但和现在一模一样的字段不算改动、不收钱；一处都没变就整单拒绝。
    金币不够时整单拒绝 —— 绝不出现「改了一半」的中间状态。
    """
    session = user_from_token(request.headers.get("X-Session-Token"))
    if session.role == "guest":
        raise HTTPException(status_code=400, detail="游客不能修改资料，请注册账号后再来")

    changes: dict[str, Any] = {}

    if payload.nickname is not None:
        nickname = payload.nickname.strip()
        if not nickname:
            raise HTTPException(status_code=400, detail="昵称不能为空")
        if len(nickname) > NICKNAME_MAX_LENGTH:
            raise HTTPException(status_code=400, detail=f"昵称最多 {NICKNAME_MAX_LENGTH} 个字")
        if moderate_content(nickname)[1]:
            raise HTTPException(status_code=400, detail="这个昵称里有不合适的词，换一个吧")
        if nickname != session.nickname:
            # 在线列表里重名会让人分不清谁是谁，所以在线范围内不允许撞名。
            taken = {item.nickname for item in sessions.values() if item is not session and item.websocket}
            if nickname in taken:
                raise HTTPException(status_code=400, detail="房间里已经有人叫这个名字了，换一个吧")
            changes["nickname"] = nickname

    if payload.avatar is not None:
        chosen = payload.avatar.strip()
        if chosen != session.avatar:
            # 自定义头像存在时，界面会隐藏内置头像；但已有账号仍应能继续使用原来的默认头像。
            allowed = [item["value"] for item in DEFAULT_AVATAR_OPTIONS]
            allowed += [item["value"] for item in avatar_options()]
            with closing(get_db()) as db:
                owned = db.execute("SELECT id, icon FROM shop_items WHERE item_type='avatar'").fetchall()
            allowed += [row["icon"] for row in owned if session.inventory.get(row["id"], 0) > 0]
            allowed += [key for key, count in session.inventory.items() if count > 0 and key.startswith("/avatars/")]
            if chosen not in allowed:
                raise HTTPException(status_code=400, detail="请选择系统头像，或头像文件夹中的头像")
            changes["avatar"] = chosen

    if payload.real_name is not None:
        real_name = normalize_profile_text(payload.real_name, "真实姓名", REAL_NAME_MAX_LENGTH)
        if real_name != session.real_name:
            changes["real_name"] = real_name

    if payload.class_name is not None:
        class_name = normalize_class_name(payload.class_name)
        if class_name != session.class_name:
            changes["class_name"] = class_name

    if payload.bio is not None:
        bio = normalize_profile_text(payload.bio, "个性签名", BIO_MAX_LENGTH)
        if bio != session.bio:
            changes["bio"] = bio

    if payload.new_password is not None:
        # 改密码必须验原密码：token 是长期有效的，不验的话谁捡到别人的电脑就能改走密码。
        if session.username is None:
            raise HTTPException(status_code=400, detail="当前账号不能修改密码")
        if not payload.old_password:
            raise HTTPException(status_code=400, detail="修改密码要先填写原密码")
        with closing(get_db()) as db:
            row = db.execute("SELECT password_hash FROM users WHERE id = ?", (session.user_id,)).fetchone()
        current_hash = row["password_hash"] if row else ""
        if not current_hash or not verify_password(payload.old_password, current_hash):
            raise HTTPException(status_code=400, detail="原密码不对，密码没有修改")
        if len(payload.new_password) < 3:
            raise HTTPException(status_code=400, detail="新密码至少 3 位")
        if len(payload.new_password) > 64:
            raise HTTPException(status_code=400, detail="新密码最多 64 位")
        if verify_password(payload.new_password, current_hash):
            raise HTTPException(status_code=400, detail="新密码和原密码一样，不用改")
        changes["password"] = payload.new_password

    if not changes:
        raise HTTPException(status_code=400, detail="没有发现要修改的内容")

    cost = 0 if session.role == "admin" else PROFILE_EDIT_COST * len(changes)
    if not can_spend_coins(session, cost):
        raise HTTPException(
            status_code=402,
            detail=f"这次要改 {len(changes)} 处，需要 {cost} 金币，你只有 {session.coins} 金币",
        )

    old_nickname = session.nickname
    spend_coins(session, cost)
    if "nickname" in changes:
        session.nickname = changes["nickname"]
    if "avatar" in changes:
        session.avatar = changes["avatar"]
    if "real_name" in changes:
        session.real_name = changes["real_name"]
    if "class_name" in changes:
        session.class_name = changes["class_name"]
    if "bio" in changes:
        session.bio = changes["bio"]
    # 金币与资料写在同一条 UPDATE 里（persist_session）：不可能出现扣了钱没改成。
    persist_session(session, hash_password(changes["password"]) if "password" in changes else None)

    if "nickname" in changes and old_nickname != session.nickname and RENAME_UPDATES_HISTORY:
        # 历史消息里的旧昵称一并改掉，否则同一个人在房间里会出现两个名字。
        # sender_id 不变，管理员照样能追溯到人 —— 这里改的只是展示用的名字。
        with closing(get_db()) as db:
            db.execute("UPDATE messages SET sender_name = ? WHERE sender_id = ?",
                       (session.nickname, session.user_id))
            db.commit()

    await send_profile_update(session)
    labels = "、".join(PROFILE_FIELD_LABELS[key] for key in changes)
    message = f"已修改：{labels}" + (f"，扣除 {cost} 金币" if cost else "")  # 不收费时不追加任何多余字样
    return {"user": session.self_public(), "message": message, "charged": cost,
            "changes": sorted(changes)}


@app.post("/api/profile/view")
async def view_profile(payload: ProfileViewRequest, request: Request) -> dict[str, Any]:
    """查看某人的完整资料，一次 PROFILE_VIEW_COST 金币。

    免费的情形：看自己、管理员查看。PROFILE_VIEW_CACHE_PER_SESSION 为真时，
    同一次登录里对同一个人只扣一次 —— 学生手一抖点两下就被扣两回金币，
    比少收 1 金币更让人难受。
    """
    session = user_from_token(request.headers.get("X-Session-Token"))
    target_id = payload.user_id.strip()
    # ★ 这里是「已登录的会话表」，不等于「在线」—— 在线要看 websocket 有没有连着。
    target = next((item for item in sessions.values() if item.user_id == target_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="这个人已经不在线了")

    # 看自己、管理员查看都免费。管理员不要求对方在线（老师随时要能查）；
    # 学生之间则必须在对方在线时看 —— 入口是在线列表，离线的人根本点不开，
    # 也不该花 1 金币换一份过期的资料。
    if target is session or session.role == "admin":
        return {"card": profile_card(target, session), "charged": 0, "coins": coins_display(session),
                "user": session.self_public()}

    if target.websocket is None:
        raise HTTPException(status_code=404, detail="这个人已经不在线了")

    if session.role == "guest":
        raise HTTPException(status_code=400, detail="游客不能使用金币功能，请注册账号后再来")

    if PROFILE_VIEW_CACHE_PER_SESSION and target_id in session.viewed_profiles:
        return {"card": profile_card(target, session), "charged": 0, "coins": coins_display(session),
                "user": session.self_public(), "message": "这个人你已经看过了，本次不再扣金币"}

    if not can_spend_coins(session, PROFILE_VIEW_COST):
        raise HTTPException(status_code=402,
                            detail=f"查看资料需要 {PROFILE_VIEW_COST} 金币，你只有 {session.coins} 金币")

    spend_coins(session, PROFILE_VIEW_COST)
    session.viewed_profiles.add(target_id)
    persist_session(session)
    return {"card": profile_card(target, session), "charged": PROFILE_VIEW_COST,
            "coins": coins_display(session), "user": session.self_public()}


@app.get("/api/rooms")
async def rooms() -> dict[str, Any]:
    with closing(get_db()) as db:
        rows = db.execute("SELECT * FROM rooms ORDER BY rowid").fetchall()
    return {"rooms": [{**dict(row), "online": len(manager.users(row["id"]))} for row in rows]}


@app.get("/api/avatars")
async def avatars() -> dict[str, Any]:
    return {"avatars": avatar_options()}


@app.get("/api/emojis")
async def emojis() -> dict[str, Any]:
    return {"emojis": emoji_options()}


@app.get("/api/announcement")
async def announcement(room_id: str = Query(default="lobby")) -> dict[str, Any]:
    """某个房间的公告。由「公告/<房间号>.txt」维护，改完存盘即生效，前端不必重新构建。"""
    if not room_info(room_id):
        room_id = "lobby"
    return {"room_id": room_id, "announcement": room_announcement(room_id)}


@app.post("/api/announcement")
async def update_announcement(payload: AnnouncementUpdate, request: Request) -> dict[str, Any]:
    """房主改自己房间的公告、管理员改任意房间的公告。

    ★ 改的其实就是老师用记事本改的那个 txt 文件 —— 一处内容、两个入口。
      所以这里写完必须让缓存失效，否则「页面上保存成功了、读出来还是旧的」。
    ★ 从页面写进来的内容要过违禁词过滤（房主也是学生）；
      老师直接改文件那条路不过滤（那是他自己维护的，见 DEFAULT_ANNOUNCEMENT 里的说明）。
    """
    session = user_from_token(request.headers.get("X-Session-Token"))
    room_id = payload.room_id.strip() or "lobby"
    if not room_info(room_id):
        raise HTTPException(status_code=404, detail="这个房间不存在")
    if not can_manage_room(session, room_id):
        raise HTTPException(status_code=403, detail="只有管理员和这个房间的房主可以改公告")
    # ★ 禁言要一并生效 —— 别的管理动作（admin_action）都是先查禁言再干活，
    #   只有这里漏了，于是「被全站禁言的房主照样能改公告」，和其他限制对不上。
    if session.muted_until_in(room_id) > time.time():
        raise HTTPException(status_code=403, detail="你当前处于禁言状态，不能修改房间公告")
    text = _announcement_body(payload.content.replace("\r\n", "\n").replace("\r", "\n"))
    if not text:
        raise HTTPException(status_code=400, detail="公告不能是空的")
    if len(text) > ANNOUNCEMENT_MAX_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"公告最多 {ANNOUNCEMENT_MAX_LENGTH} 个字，现在是 {len(text)} 个字，精简一下吧",
        )
    if moderate_content(text)[1]:
        raise HTTPException(status_code=400, detail="公告里有不合适的词，改一下吧")
    # ★ 限流判断不能写成 `now - _ANNOUNCE_SAVED_AT.get(room_id, 0.0)`：
    #   time.monotonic() 的起点是开机时刻，服务刚起来几秒时这个差值会小于间隔，
    #   于是「服务器启动后第一次保存」会被误判成「保存太频繁」而拒绝。
    #   用 None 表示「这个房间还没保存过」，就不存在这个启动窗口。
    last = _ANNOUNCE_SAVED_AT.get(room_id)
    if last is not None and time.monotonic() - last < ANNOUNCEMENT_SAVE_INTERVAL:
        raise HTTPException(status_code=429, detail="保存得太频繁了，缓一下再改")
    # ★ 旧正文要在 save 之前读 —— save 会改写文件，之后再读拿到的就是新内容，
    #   「内容到底变没变」就永远判成没变，系统消息一条也发不出来。
    #   这里直读磁盘（不走 room_announcement 的 1 秒缓存），保证比对的是真实旧值。
    previous = _read_announcement(room_id)
    saved = save_room_announcement(room_id, text)
    await manager.broadcast(
        {"type": "announcement_updated", "room_id": room_id, "announcement": saved}, room_id=room_id
    )
    # ★ 光「左侧面板悄悄换内容」是不够的：正在打字的学生根本不会发现公告变了，
    #   所以再往聊天区插一条系统消息。只发给本房间（和别人改公告的范围一致）。
    #   正文一个字都没动就不发 —— 房主反复点保存不该刷屏。
    if saved != previous:
        who = "管理员" if session.role == "admin" else "房主"
        await manager.broadcast(
            {"type": "system", "message": f"📢 {who} {session.nickname} 更新了本房间公告"}, room_id=room_id
        )
    return {"room_id": room_id, "announcement": saved, "message": "公告已更新，本房间的人马上就能看到"}


@app.get("/api/shop")
async def shop() -> dict[str, Any]:
    with closing(get_db()) as db:
        # 头像不属于商城商品；保留数据库里的历史头像记录，避免旧账号已拥有的头像失效。
        rows = db.execute("SELECT * FROM shop_items WHERE item_type <> 'avatar' ORDER BY price, id").fetchall()
    items = list(STARTER_ITEMS.values()) + [dict(row) for row in rows]
    return {"items": [{**item, "recycle_price": recycle_price(item)} for item in items]}


def recycle_price(item: dict[str, Any]) -> int:
    """回收价＝当前商城价的一半（向下取整），**保底 1 金币**。

    保底是为 1 金币的基础礼物（鲜花/掌声/飞吻/礼花）准备的：`1 // 2 == 0`
    会让它们的回收价归零 —— 买得起、退不回来，清理库存毫无回报。
    对价格 ≥ 2 的物品**完全等价于原来的 `price // 2`**（`2//2==1`、`10//2==5`…），
    所以这条只影响基础礼物，其余规则一字未变。
    头像/宠物用品不可回收，恒为 0。
    """
    if item["item_type"] not in ("badge", "gift"):
        return 0
    return max(1, int(item["price"]) // 2)


@app.post("/api/shop/recycle")
async def recycle_items(payload: RecycleRequest, request: Request) -> dict[str, Any]:
    session = user_from_token(request.headers.get("X-Session-Token"))
    if session.role == "guest":
        raise HTTPException(status_code=400, detail="游客不能回收物品，请注册账号后再来")
    catalog = item_catalog()
    quantities: dict[str, int] = {}
    earned = 0
    for entry in payload.items:
        item = catalog.get(entry.item_id)
        if not item or item["item_type"] not in ("badge", "gift"):
            raise HTTPException(status_code=400, detail="只能回收徽章和礼物")
        if entry.item_id in quantities:
            raise HTTPException(status_code=400, detail="同一种物品只能提交一次")
        if session.inventory.get(entry.item_id, 0) < entry.quantity:
            raise HTTPException(status_code=400, detail=f"{item['name']}库存不足，请重新选择数量")
        quantities[entry.item_id] = entry.quantity
        earned += recycle_price(item) * entry.quantity

    previous_inventory, previous_coins, previous_badge = session.inventory, session.coins, session.badge_id
    # 核对、扣库存和落库之间不挂起，避免与送礼/购买交错；余额和库存一并保存。
    session.inventory = {key: count - quantities.get(key, 0) for key, count in session.inventory.items()
                         if count - quantities.get(key, 0) > 0}
    if session.inventory.get(session.badge_id, 0) == 0:
        session.badge_id = ""
    if not has_unlimited_coins(session):
        session.coins += earned
    try:
        persist_session(session)
    except Exception:
        session.inventory, session.coins, session.badge_id = previous_inventory, previous_coins, previous_badge
        raise
    count = sum(quantities.values())
    # 管理员余额是展示用的 ∞，实际不加币（见上面的 has_unlimited_coins 判断）。
    # 文案必须如实说，否则「获得 12 金币」与余额不变并列，会让人以为扣错了。
    message = (f"已回收 {count} 件物品（管理员金币不增加，仅减少库存）"
               if has_unlimited_coins(session) else f"已回收 {count} 件物品，获得 {earned} 金币")
    result = {"user": session.self_public(), "earned": earned, "message": message}
    await session.send({"type": "profile_updated", "user": session.self_public()})
    await send_profile_update(session)
    return result


@app.get("/api/activities")
async def room_activities(request: Request, room_id: str = "lobby") -> dict[str, Any]:
    user_from_token(request.headers.get("X-Session-Token"))
    if not room_info(room_id):
        raise HTTPException(status_code=404, detail="房间不存在")
    from interactions import snapshot
    return snapshot(room_id)


@app.get("/api/rankings")
async def rankings(request: Request, room_id: str = "lobby", kind: str = "room") -> dict[str, Any]:
    user_from_token(request.headers.get("X-Session-Token"))
    if kind not in ("room", "wealth", "charm", "active") or not room_info(room_id):
        raise HTTPException(status_code=400, detail="排行榜类型或房间无效")
    from interactions import leaderboard
    return {"rows": leaderboard(room_id, kind)}


@app.get("/api/messages")
async def messages(request: Request, room_id: str = Query(default="lobby"), private_with: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=200)) -> dict[str, Any]:
    # 公聊历史也必须带 token。以前这条是「谁都能读」，于是「被请出聊天室」的人
    # （IP 已经被 check_entry 拦下、再也换不到新 token）依旧能把整个房间的聊天记录拉走。
    # 私聊分支本来就要 token，现在两条路统一先过 user_from_token（顺带查禁入冷却）。
    session = user_from_token(request.headers.get("X-Session-Token"))
    with closing(get_db()) as db:
        # 历史头像取账号当前值；游客或已删除的账号保留消息中的头像。
        if private_with:
            rows = db.execute(
                "SELECT m.*, u.avatar AS current_sender_avatar FROM messages AS m "
                "LEFT JOIN users AS u ON u.id = m.sender_id "
                "WHERE m.room_id = 'private' AND ((m.sender_id = ? AND m.recipient_id = ?) OR (m.sender_id = ? AND m.recipient_id = ?)) ORDER BY m.id DESC LIMIT ?",
                (session.user_id, private_with, private_with, session.user_id, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT m.*, u.avatar AS current_sender_avatar FROM messages AS m "
                "LEFT JOIN users AS u ON u.id = m.sender_id "
                "WHERE m.room_id = ? AND m.recipient_id IS NULL ORDER BY m.id DESC LIMIT ?",
                (room_id, limit),
            ).fetchall()
    return {"messages": [row_to_message(row) for row in reversed(rows)]}


@app.get("/api/online")
async def online(request: Request) -> dict[str, Any]:
    # 同样必须带 token：在线名单里带 role / owner / bio，是「被踢之后还能继续盯梢」的口子。
    user_from_token(request.headers.get("X-Session-Token"))
    return {"users": manager.users()}


def parse_minutes(raw: Any) -> int:
    """禁言时长只接受 1～120 的整数，其它输入一律回退到默认值。"""
    try:
        minutes = int(str(raw))
    except (TypeError, ValueError):
        return MUTE_MINUTES
    return minutes if 1 <= minutes <= 120 else MUTE_MINUTES


# 每个身份「能做哪些管理动作」，集中写在这里。
# 散在几十行 if 里写权限，漏掉的那一条不会报错、只会静默放行 —— 集中成两张清单就不会漏。
OWNER_ACTIONS = ("mute", "unmute")
ADMIN_ONLY_ACTIONS = ("mute_all", "unmute_all", "kick", "delete", "set_owner", "unset_owner")


def persist_global_mute(target: Session, until: float) -> None:
    """把「全站禁言」写进 users 表。

    ★ 这里原来是 `if target.role == "user":` —— 一个「白名单的反面」：
      只要将来多出任何一种带身份的人，对他禁言就会变成「内存里生效、重启就失效」，
      不报错、当天也看不出问题。改成「有数据库记录的都写」（游客没有记录，跳过），
      就不会再漏人。
    """
    if target.role == "guest":
        return
    with closing(get_db()) as db:
        db.execute("UPDATE users SET muted_until = ? WHERE id = ?", (until, target.user_id))
        db.commit()


async def admin_action(session: Session, data: dict[str, Any]) -> None:
    """管理动作的统一入口。

    权限口径（★ 是这个功能的核心，改动前请先读这几行）：
      · 管理员：所有动作、所有房间。
      · 房主  ：只有 mute / unmute，**且只能作用于自己房间的人**，不能碰管理员。
      · 游客  ：什么都不能做。
    禁言分两种：mute = 只在被操作者当前所在的那个房间生效；
    mute_all = 全站（所有房间都发不了言），只有管理员能用。
    """
    action = str(data.get("action") or "")
    target_id = str(data.get("target_id", ""))
    is_admin = session.role == "admin"

    if action not in OWNER_ACTIONS + ADMIN_ONLY_ACTIONS:
        await session.send({"type": "error", "message": "未知的管理操作"})
        return

    if action == "delete":
        if not is_admin:
            await session.send({"type": "error", "message": "只有管理员可以删除消息"})
            return
        try:
            message_id = int(str(data.get("message_id")))
        except (TypeError, ValueError):
            await session.send({"type": "error", "message": "要删除的消息不存在"})
            return
        with closing(get_db()) as db:
            row = db.execute("SELECT room_id FROM messages WHERE id = ?", (message_id,)).fetchone()
            if not row:
                await session.send({"type": "error", "message": "这条消息已经不存在了"})
                return
            room_id = row["room_id"]
            db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            db.commit()
        await manager.broadcast({"type": "message_deleted", "message_id": message_id}, room_id=room_id)
        return

    if action in ("set_owner", "unset_owner"):
        if not is_admin:
            await session.send({"type": "error", "message": "只有管理员可以任命房主"})
            return
        # 默认把对方设成「管理员当前所在房间」的房主 —— 授权是跟着房间走的。
        room_id = str(data.get("room_id") or session.room_id)
        room = room_info(room_id)
        if not room:
            await session.send({"type": "error", "message": "这个房间不存在"})
            return
        target = manager.sessions.get(target_id)
        if not target or target.role != "user":
            await session.send({"type": "error", "message": "只能把注册用户设为房主"})
            return
        granted = action == "set_owner"
        was_owner = is_room_owner(target.user_id, room_id)
        if granted and not was_owner and len(room_owners(room_id)) >= ROOM_OWNER_LIMIT:
            await session.send({"type": "error", "message": f"一个房间最多 {ROOM_OWNER_LIMIT} 个房主，先撤掉一个吧"})
            return
        set_room_owner(room_id, target.user_id, granted)
        newly_granted = granted and not was_owner
        if newly_granted:
            target.coins += ROOM_OWNER_REWARD
            persist_session(target)
        room_name = str(room["name"])
        reward_note = f"，奖励 {ROOM_OWNER_REWARD} 金币" if newly_granted else ""
        await manager.broadcast(
            {"type": "system", "message": f"管理员已把 {target.nickname} {'设为' if granted else '撤下'}「{room_name}」的房主{reward_note}"},
            room_id=room_id,
        )
        # 房主标识是在线名单里的一栏，授权后必须重发名单，否则大家看到的还是旧身份。
        await manager.broadcast(users_payload(room_id), room_id=room_id)
        await send_profile_update(target)
        await session.send(
            {"type": "info", "message": f"已{'任命' if granted else '撤销'} {target.nickname} 在「{room_name}」的房主身份"}
        )
        return

    # 剩下四个动作都要先找到目标人。
    target = manager.sessions.get(target_id)
    if not target:
        await session.send({"type": "error", "message": "该用户已经不在线了"})
        return
    room_id = target.room_id

    if not is_admin:
        if action not in OWNER_ACTIONS:
            await session.send({"type": "error", "message": "只有管理员可以执行这个操作"})
            return
        if not can_manage_room(session, room_id):
            await session.send({"type": "error", "message": "你不是这个房间的房主，管不了这里的人"})
            return
        if target is session or target.role not in ("user", "guest"):
            await session.send({"type": "error", "message": "房主只能管本房间的普通用户和游客"})
            return
        if session.muted_until_in(session.room_id) > time.time():
            await session.send({"type": "error", "message": "你正在被禁言，暂时不能执行管理操作"})
            return

    who = "管理员" if is_admin else "房主"

    if action == "mute":
        minutes = parse_minutes(data.get("minutes"))
        until = time.time() + minutes * 60
        # ★ 只写 room_mutes，**不碰** target.muted_until —— 后者是全站禁言，
        #   混进来就会变成「房主在 A 房间禁言某人，那人在 B 房间也说不了话」。
        target.room_mutes[room_id] = until
        set_room_mute(room_id, target.user_id, until)
        await manager.broadcast(
            {"type": "system", "message": f"{who}已将 {target.nickname} 在本房间禁言 {minutes} 分钟"}, room_id=room_id
        )
        await send_profile_update(target)
        await session.send({"type": "info", "message": f"已在本房间禁言 {target.nickname} {minutes} 分钟"})
    elif action == "unmute":
        target.room_mutes.pop(room_id, None)
        set_room_mute(room_id, target.user_id, 0)
        await manager.broadcast(
            {"type": "system", "message": f"{who}已解除 {target.nickname} 在本房间的禁言"}, room_id=room_id
        )
        await send_profile_update(target)
        await session.send({"type": "info", "message": f"已解除 {target.nickname} 在本房间的禁言"})
    elif action == "mute_all":
        minutes = parse_minutes(data.get("minutes"))
        until = time.time() + minutes * 60
        target.muted_until = max(target.muted_until, until)
        persist_global_mute(target, target.muted_until)
        await manager.broadcast(
            {"type": "system", "message": f"管理员已将 {target.nickname} 全站禁言 {minutes} 分钟"}, room_id=room_id
        )
        await send_profile_update(target)
        await session.send({"type": "info", "message": f"已全站禁言 {target.nickname} {minutes} 分钟"})
    elif action == "unmute_all":
        target.muted_until = 0
        persist_global_mute(target, 0)
        await manager.broadcast(
            {"type": "system", "message": f"管理员已解除 {target.nickname} 的全站禁言"}, room_id=room_id
        )
        await send_profile_update(target)
        await session.send({"type": "info", "message": f"已解除 {target.nickname} 的全站禁言"})
    else:  # kick
        target.kicked_until = time.time() + KICK_COOLDOWN_SECONDS
        if target.client_ip:
            with closing(get_db()) as db:
                db.execute("INSERT OR REPLACE INTO entry_cooldowns (client_ip, expires_at) VALUES (?, ?)", (target.client_ip, target.kicked_until))
                db.commit()
        persist_session(target)
        for other in list(sessions.values()):
            if other.role != "admin" and (other.user_id == target.user_id or (target.client_ip and other.client_ip == target.client_ip)):
                other.kicked_until = target.kicked_until
        if target.websocket:
            target_socket = target.websocket
            # 先把它摘出连接表（停掉发送协程），再直发通知并关连接，
            # 否则广播协程可能正往同一个 socket 写，消息会交错甚至直接抛异常。
            await manager.remove(target, target_socket)
            await direct_send(target_socket, {"type": "kicked", "message": "你已被管理员请出聊天室，3 分钟后才能重新进入"})
            await close_socket_safely(target_socket, 4001)
        await manager.broadcast({"type": "system", "message": f"管理员将 {target.nickname} 请出了聊天室（3 分钟内不能重新进入）"}, room_id=room_id)
        # 人被摘出连接表了，全站在线名单也要跟着更新（别的房间可能正看着他）。
        await push_presence()


async def send_profile_update(session: Session) -> None:
    await manager.broadcast({"type": "profile_updated", "user": session.public()})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str) -> None:
    await websocket.accept()
    session = sessions.get(token)
    if not session:
        await websocket.send_json({"type": "error", "message": "登录状态已失效，请返回重新进入"})
        await close_socket_safely(websocket, 4003)
        return
    try:
        check_entry(websocket.client.host if websocket.client else "unknown", session.user_id, session.role)
    except HTTPException as error:
        await websocket.send_json({"type": "kicked", "message": error.detail})
        await close_socket_safely(websocket, 4003)
        return
    remaining = session.kicked_until - time.time()
    if remaining > 0:
        await websocket.send_json({"type": "error", "message": f"你刚被请出聊天室，请 {max(1, int(remaining + 0.999))} 秒后再进入"})
        await close_socket_safely(websocket, 4003)
        return
    await manager.add(session, websocket)
    session.client_ip = websocket.client.host if websocket.client else ""
    # 顺带把本房间的公告带上：连上（含断线重连）就能拿到最新内容，老师改完不用重启服务。
    await session.send({"type": "ready", "user": session.self_public(), "room_id": session.room_id, "announcement": room_announcement(session.room_id)})
    await session.send(users_payload(session.room_id))
    await manager.broadcast(users_payload(session.room_id), room_id=session.room_id)
    # 全站 presence（谁在线 + 每个房间几个人）要推给**所有**房间，不是只推本房间。
    await push_presence()
    await manager.broadcast({"type": "presence_notice", "action": "join", "nickname": session.nickname, "level": session.level, "level_title": level_title(session.level), "role": session.role}, room_id=session.room_id)
    try:
        while True:
            data = await websocket.receive_json()
            session.last_seen = time.time()
            if session.kicked_until > time.time():
                # 这一条要确保送达后再关连接：先停掉发送协程，免得两条协程抢同一个 socket。
                await manager.remove(session, websocket)
                await direct_send(websocket, {"type": "kicked", "message": "你已被请出聊天室，3 分钟后才能重新进入"})
                await close_socket_safely(websocket, 4001)
                break
            message_type = data.get("type")
            # ★ 用 muted_until_in(房间)：全站禁言和「本房间禁言」取较晚的那个。
            #   直接读 session.muted_until 会漏掉房主施加的按房间禁言。
            muted_until = session.muted_until_in(session.room_id)
            if muted_until > time.time() and message_type in ("message", "private_message", "gift", "coin_gift", "use_item", "interaction"):
                await session.send({"type": "error", "message": f"你当前处于禁言状态，还需等待 {max(1, int(muted_until - time.time() + 0.999))} 秒"})
                continue
            if message_type == "interaction":
                from interactions import handle_action
                try:
                    await handle_action(session, data)
                except Exception:
                    await session.send({"type": "error", "message": "互动操作失败，请稍后再试"})
            elif message_type == "join_room":
                target_room = str(data.get("room_id", "lobby"))
                if not room_info(target_room):
                    await session.send({"type": "error", "message": "这个房间不存在"})
                    continue
                old_room = session.room_id
                session.room_id = target_room
                # 换个房间 = 换一套禁言记录和一份公告。房主施加的禁言是按房间算的，
                # 不重新取一次就会出现「在 A 房被禁言，到了 B 房还以为自己被禁言」。
                session.room_mutes = user_room_mutes(session.user_id)
                await session.send({"type": "room_joined", "room_id": target_room, "announcement": room_announcement(target_room)})
                await session.send(users_payload(target_room))
                await manager.broadcast(users_payload(old_room), room_id=old_room)
                await manager.broadcast(users_payload(target_room), room_id=target_room)
                if old_room != target_room:
                    await manager.broadcast({"type": "presence_notice", "action": "leave", "nickname": session.nickname, "level": session.level, "level_title": level_title(session.level), "role": session.role}, room_id=old_room)
                # 两边房间的人数都变了，全站 presence 必须跟着发一次。
                await push_presence()
                await manager.broadcast({"type": "presence_notice", "action": "join", "nickname": session.nickname, "level": session.level, "level_title": level_title(session.level), "role": session.role}, room_id=target_room)
            elif message_type == "message":
                content, violated, reason = screen_outgoing(session, data.get("content", ""))
                if content is None:
                    await reject_speak(session, reason)
                    continue
                style, effect = sanitize_text_format(data.get("text_style", ""), data.get("text_effect", ""))
                message = save_message(session, content, text_style=style, text_effect=effect, award_income=not violated)
                if violated:
                    deducted = apply_violation_penalty(session)
                    await session.send({"type": "system", "message": violation_notice(deducted, session)})
                await manager.broadcast({"type": "message", "message": message}, room_id=session.room_id)
                await send_profile_update(session)
            elif message_type == "gift":
                item_id = str(data.get("item_id", ""))
                target_id = str(data.get("target_id", ""))
                target = manager.sessions.get(target_id)
                item = item_catalog().get(item_id)
                if not item or item.get("item_type") not in ("gift", "avatar", "badge"):
                    await session.send({"type": "error", "message": "这个物品不存在"})
                    continue
                if not target or not target.websocket or target is session:
                    await session.send({"type": "error", "message": "请先选择一位在线好友接收礼物"})
                    continue
                if session.inventory.get(item_id, 0) < 1:
                    await session.send({"type": "error", "message": "这个物品已经用完了，请到商城兑换"})
                    continue
                session.inventory[item_id] -= 1
                target.inventory[item_id] = target.inventory.get(item_id, 0) + 1
                # ★ 魅力也是「能被来回刷」的收益：两个人对送同一件礼物，道具绕一圈回到手里，
                #   魅力却每次都在涨（实测 10 个来回 = 双方各 +20）。它跟送出方的金币 / 经验
                #   共用 income_gate_open() 这**同一道闸门**（比较式只有那一个地方）。
                award_gift_charm(session, target, item)
                persist_session(session)
                persist_session(target)
                # 礼物是房间内的公开消息：不能写 recipient_id，否则会被房间历史查询（recipient_id IS NULL）过滤掉。
                message = save_message(session, f"送给 {target.nickname} {item['icon']} {item['name']}", "gift", session.room_id, attachment={"item_id": item_id, "item_name": item["name"], "icon": item["icon"], "target_name": target.nickname})
                await manager.broadcast({"type": "message", "message": message}, room_id=session.room_id)
                # ★ 库存变了，两个人的物品栏都要**立刻**更新，而且必须走 self_public()（带 inventory）：
                #   下面那两个 send_profile_update 广播的是 public()，**刻意不带 inventory** ——
                #   只发它的话，送出方页面还显示物品没少，收礼方的新物品也点不了（要刷新一次才出现）。
                if target.websocket:
                    await target.send({"type": "item_received", "sender_name": session.nickname, "item_name": item["name"], "icon": item["icon"], "user": target.self_public()})
                await session.send({"type": "profile_updated", "user": session.self_public()})
                if target.websocket:
                    await target.send({"type": "profile_updated", "user": target.self_public()})
                await send_profile_update(session)
                await send_profile_update(target)
            elif message_type == "coin_gift":
                target_id = str(data.get("target_id", ""))
                target = manager.sessions.get(target_id)
                if not target or not target.websocket or target is session:
                    await session.send({"type": "error", "message": "请先选择一位在线好友接收金币"})
                    continue
                if session.role == "guest" or target.role == "guest":
                    await session.send({"type": "error", "message": "游客不能使用金币功能"})
                    continue
                try:
                    amount = int(data.get("amount", 0))
                except (TypeError, ValueError):
                    amount = 0
                if amount < COIN_GIFT_MIN or amount > COIN_GIFT_MAX:
                    await session.send({"type": "error", "message": f"金币数量必须是 {COIN_GIFT_MIN}～{COIN_GIFT_MAX} 的整数"})
                    continue
                if not can_spend_coins(session, amount):
                    await session.send({"type": "error", "message": f"金币不足，你只有 {session.coins} 金币"})
                    continue
                spend_coins(session, amount)
                if not has_unlimited_coins(target):
                    target.coins += amount
                persist_session(session)
                persist_session(target)
                message = save_message(
                    session,
                    f"送给 {target.nickname} 💰 {amount} 金币",
                    "coin_gift",
                    session.room_id,
                    attachment={"amount": amount, "target_name": target.nickname, "item_name": f"{amount}金币", "icon": "💰"},
                    award_income=False,
                )
                await manager.broadcast({"type": "message", "message": message}, room_id=session.room_id)
                if target.websocket:
                    await target.send({"type": "coins_received", "sender_name": session.nickname, "amount": amount, "user": target.self_public()})
                await session.send({"type": "profile_updated", "user": session.self_public()})
                if target.websocket:
                    await target.send({"type": "profile_updated", "user": target.self_public()})
                await send_profile_update(session)
                await send_profile_update(target)
            elif message_type == "private_message":
                target_id = str(data.get("target_id", ""))
                target = manager.sessions.get(target_id)
                if target is session:
                    await session.send({"type": "error", "message": "不能和自己私聊"})
                    continue
                if not target or not target.websocket:
                    await session.send({"type": "error", "message": "对方当前不在线"})
                    continue
                content, violated, reason = screen_outgoing(session, data.get("content", ""))
                if content is None:
                    await reject_speak(session, reason)
                    continue
                style, effect = sanitize_text_format(data.get("text_style", ""), data.get("text_effect", ""))
                message = save_message(session, content, "private", "private", target_id, text_style=style, text_effect=effect, award_income=not violated)
                if violated:
                    deducted = apply_violation_penalty(session)
                    await session.send({"type": "system", "message": violation_notice(deducted, session)})
                payload = {"type": "private_message", "message": message}
                await session.send(payload)
                if target is not session:
                    await target.send(payload)
                await send_profile_update(session)
            elif message_type == "shop_buy":
                if session.role == "guest":
                    await session.send({"type": "error", "message": "游客不能使用金币功能，请注册账号后再来"})
                    continue
                item_id = str(data.get("item_id", ""))
                item = item_catalog().get(item_id)
                if not item:
                    await session.send({"type": "error", "message": "物品不存在"})
                    continue
                if item["item_type"] == "avatar":
                    await session.send({"type": "error", "message": "头像无需购买，请在资料设置中选择"})
                    continue
                if session.level < item["min_level"]:
                    await session.send({"type": "error", "message": f"等级达到 Lv.{item['min_level']} 后才能购买这个物品"})
                    continue
                if not can_spend_coins(session, item["price"]):
                    await session.send({"type": "error", "message": "金币不足，多聊天可以获得金币"})
                    continue
                if item["item_type"] in ("avatar", "badge") and session.inventory.get(item_id, 0) > 0:
                    await session.send({"type": "error", "message": "你已经拥有这个物品了"})
                    continue
                spend_coins(session, item["price"])
                session.inventory[item_id] = session.inventory.get(item_id, 0) + 1
                if item["item_type"] == "avatar":
                    session.avatar = item["icon"]
                elif item["item_type"] == "badge":
                    # 刚兑换的徽章直接戴上，否则玩家看不出兑换成功。
                    session.badge_id = item_id
                persist_session(session)
                # 必须回 self_public：前端收到会直接覆盖 user，少了 inventory 道具栏就会空掉。
                await session.send({"type": "shop_result", "message": f"购买成功：{item['name']}", "user": session.self_public()})
                await send_profile_update(session)
            elif message_type == "use_item":
                item_id = str(data.get("item_id", ""))
                item = item_catalog().get(item_id)
                if not item or item.get("item_type") not in ("avatar", "badge") or session.inventory.get(item_id, 0) < 1:
                    await session.send({"type": "error", "message": "这个物品不能在“用道具”中使用"})
                    continue
                # 徽章的「使用」＝佩戴：同一时间只戴一枚，戴上后 public() 里的 badge 立刻变。
                if item.get("item_type") == "avatar":
                    session.avatar = item["icon"]
                else:
                    session.badge_id = item_id
                persist_session(session)
                verb = "佩戴了" if item.get("item_type") == "badge" else "使用了"
                message = save_message(session, f"{session.nickname} {verb} {item['icon']} {item['name']}", "use_item", session.room_id, attachment={"item_id": item_id, "item_name": item["name"], "icon": item["icon"]})
                await manager.broadcast({"type": "message", "message": message}, room_id=session.room_id)
                await send_profile_update(session)
            elif message_type == "admin_action":
                try:
                    await admin_action(session, data)
                except Exception:
                    await session.send({"type": "error", "message": "管理操作失败，请稍后再试"})
            elif message_type == "ping":
                await session.send({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        if websocket.application_state.name != "DISCONNECTED":
            raise
    finally:
        await manager.remove(session, websocket)
        await manager.broadcast(users_payload(session.room_id), room_id=session.room_id)
        # 「他不在线了」这件事必须让**全站**都知道：别的房间的人可能正跟他私聊。
        # 只发他自己那个房间的话，另一边会一直显示「在线」，直到自己也刷新（实测过）。
        await push_presence()
        await manager.broadcast({"type": "presence_notice", "action": "leave", "nickname": session.nickname, "level": session.level, "level_title": level_title(session.level), "role": session.role}, room_id=session.room_id)
        await manager.broadcast({"type": "system", "message": f"{session.nickname} 离开了聊天室"}, room_id=session.room_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=int(os.getenv("CHAT_PORT", "9000")),
        reload=False,
        use_colors=False,
        log_level="warning",
        access_log=False,
    )
