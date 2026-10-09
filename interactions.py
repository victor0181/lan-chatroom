"""房间活动。金币、随机结果和领取记录由服务器处理，活动保存在 SQLite。"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import closing
from typing import Any

import app as chat

# 抽奖奖池权重（合计 100）。
# ★★ 算期望收益时，**道具档必须按「回收价」折算成金币**：道具能拿去 /api/shop/recycle 换成钱，
#   按 0 计就会得出「4.50 < 5，安全」的假结论。真实口径（回收价 = max(1, 商城价 // 2)）：
#     金币档：(0×35 + 5×30 + 10×20 + 20×5) / 100 = 4.50
#     道具档：鲜花 ×2（1/个）= 2，钻石 ×1（25 → 12）= 12 → (2×5 + 12×1) / 100 = 0.22
#     合计 4.72 < 成本 5 → 仍是净消耗，金币不会被无限刷出来。
#   ⚠️ 「钻石 ×1」是唯一回收价上双的大额档：5% 权重就值 0.60，能把 4.50 顶到 5.10。
#      所以它的权重压到 1，省下的 4 个点给「谢谢参与」（35 → 39），权重合计仍是 100。
#   ⚠️ 改这里必须重算「含道具回收价」的 EV，并同步 tests/test_interactions.py 里那条断言。
# 概率文案由 snapshot() 下发给前端渲染，改权重只需改这一处，不用再去同步前端硬写文案。
PRIZES = [
    {"label": "谢谢参与", "coins": 0, "weight": 39},
    {"label": "5 金币", "coins": 5, "weight": 30},
    {"label": "10 金币", "coins": 10, "weight": 20},
    {"label": "20 金币", "coins": 20, "weight": 5},
    {"label": "鲜花 ×2", "item": "basic_flower", "count": 2, "weight": 5},
    {"label": "钻石 ×1", "item": "gift_diamond", "count": 1, "weight": 1},
]
WHEEL_TIMES = 2
# 大喇叭：花金币把一句话滚动播报给全房间。纯消耗、无回报，是金币出口。
# 价格与冷却一起下发到前端（见 snapshot），改这里就能同时改界面上的说明文字。
HORN_COST = 10
HORN_COOLDOWN = 60
HORN_MAX_LENGTH = 60


def _scaled_prize(prize: dict, times: int) -> dict:
    """按倍数放大一档奖品。label 由数值重算，避免显示的数字和实际发放对不上。"""
    scaled = dict(prize)
    if "coins" in prize:
        scaled["coins"] = prize["coins"] * times
        scaled["label"] = "谢谢参与" if not scaled["coins"] else f"{scaled['coins']} 金币"
    else:
        scaled["count"] = prize["count"] * times
        scaled["label"] = f"{prize['label'].split('×')[0].strip()} ×{scaled['count']}"
    return scaled


# 转盘独立奖池：成本 10 金币，奖品金额同步翻倍。
# 含道具回收价的真实期望 = 9.00（金币档翻倍）+ 0.44（道具档翻倍）= 9.44 —— 每次净 -0.56，
# 仍是净消耗（详见上面 PRIZES 那段的口径说明）。
# 与抽奖同池会出现「转盘贵一倍、奖品却一模一样」，所以这里单独派生一份。
# 从 PRIZES 派生而不是手写第二份，权重只有一处来源，不会「改一处漏一处」。
WHEEL_PRIZES = [_scaled_prize(prize, WHEEL_TIMES) for prize in PRIZES]
SONGS = [
    ("小星星", "夜空里一闪一闪的朋友；歌名有三个字"),
    ("两只老虎", "两位动物朋友跑得快；歌名有四个字"),
    ("生日快乐", "吹蜡烛时大家一起唱；歌名有四个字"),
    ("童年", "池塘、榕树、蝉声和放学；歌名有两个字"),
    ("让我们荡起双桨", "小船儿推开波浪；歌名有七个字"),
    ("蜗牛与黄鹂鸟", "背着重壳，慢慢爬上葡萄树；歌名有六个字"),
    ("春天在哪里", "大家在寻找一个季节；歌名有五个字"),
    ("世上只有妈妈好", "唱给妈妈的歌；歌名有七个字"),
]
IDIOMS = set("一心一意 意气风发 发扬光大 大显身手 手到擒来 来日方长 长风破浪 浪子回头 头头是道 道听途说 说一不二 二龙戏珠 珠联璧合 合二为一 一马当先 先见之明 明明白白 白手起家 家喻户晓 晓风残月 月明星稀 稀世之宝 宝刀未老 老生常谈 谈笑风生 生龙活虎 虎头蛇尾 尾大不掉 掉以轻心 心想事成 成千上万 万众一心 心口如一 一鸣惊人 人山人海 海阔天空 空前绝后 后来居上 上善若水 水到渠成 成人之美 美不胜收 收放自如 如鱼得水 水落石出 出生入死 死里逃生 生生不息 息息相关 关门大吉 吉星高照 照本宣科 科班出身 身先士卒 卒岁穷年 年富力强 强词夺理 理直气壮 壮志凌云 云开见日 日新月异 异口同声 声东击西 西窗剪烛 烛照数计 计上心来".split())
TRUTHS = ["你最喜欢的一门课是什么？为什么？", "分享一件最近让你开心的小事。", "如果可以拥有一种超能力，你会选什么？", "你最想去哪个地方旅行？", "向大家推荐一本书或一部电影。"]
DARES = ["用三个表情介绍自己。", "夸一位在线朋友，至少说出一个优点。", "用四句话编一个有趣的小故事。", "在聊天里分享一个冷笑话。", "用一句话给大家送上今天的祝福。"]
# 真心话 / 大冒险：题目公开、全房间都能回答，回答实名公开。
# 每答一题只给 PROMPT_REWARD 金币 —— 少到不值得刷，但足以让人愿意开口
# （不给金币的话，冷场的题就真成废题了，违背「方便参与」这个初衷）。
PROMPT_REWARD = 1
# 真心话 / 大冒险的时长：比抢答题短，因为它是聊天内容不是抢答，拖太久就没人接了。
PROMPT_SECONDS = 120
# 回答长度上限：够说一句完整的话，又不至于让答题板被长文刷屏。
PROMPT_ANSWER_MAX = 60


def init_interactions() -> None:
    with closing(chat.get_db()) as db:
        db.execute("CREATE TABLE IF NOT EXISTS room_activities (id TEXT PRIMARY KEY, room_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, expires_at REAL NOT NULL, payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS room_activity_scores (room_id TEXT NOT NULL, user_id TEXT NOT NULL, nickname TEXT NOT NULL, score INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (room_id, user_id))")
        db.commit()


def store(activity: dict) -> None:
    with closing(chat.get_db()) as db:
        db.execute("INSERT OR REPLACE INTO room_activities VALUES (?, ?, ?, ?, ?, ?)", (activity["id"], activity["room_id"], activity["kind"], activity["status"], activity["expires_at"], json.dumps(activity, ensure_ascii=False)))
        db.commit()


def find(activity_id: str, room_id: str) -> dict:
    with closing(chat.get_db()) as db:
        row = db.execute("SELECT payload FROM room_activities WHERE id=? AND room_id=?", (activity_id, room_id)).fetchone()
    if not row:
        raise ValueError("活动不存在，请刷新活动列表")
    return json.loads(row[0])


def activities(room_id: str) -> list[dict]:
    with closing(chat.get_db()) as db:
        rows = db.execute("SELECT payload FROM room_activities WHERE room_id=? AND (status='open' OR id IN (SELECT id FROM room_activities WHERE room_id=? ORDER BY rowid DESC LIMIT 25)) ORDER BY rowid DESC", (room_id, room_id)).fetchall()
    return [json.loads(row[0]) for row in rows]


def visible(activity: dict) -> dict:
    # ★ 过滤清单是「别人不该看到的服务端记账」：题目答案、红包剩余份数、投票/回答明细、
    #   接龙已用词，以及小游戏「谁已经猜过」（只是防重复猜的名单，没必要下发给前端）。
    result = {key: value for key, value in activity.items() if key not in ("answer", "remaining", "answers", "used", "answered")}
    if activity["status"] != "open":
        result["answer"] = activity.get("answer", "")
    if activity["kind"] == "redpack":
        result["left_count"] = len(activity["remaining"])
        result["claims"] = list(activity["claims"].values())
    if activity["kind"] == "prediction":
        result["votes"] = {option: sum(1 for vote in activity["answers"].values() if vote["choice"] == option) for option in activity["options"]}
    if activity["kind"] in ("truth", "dare"):
        # 真心话 / 大冒险是「题目公开、回答实名公开」—— 别人答了什么就是要给全房间看的，
        # 这是它唯一的乐趣来源。所以这里刻意不像 prediction 那样只发计数，要发 answers 明文。
        # answers 已经由上面的通用过滤剔除了，这里只是换了个键名重新放回去。
        result["replies"] = [dict(reply) for reply in activity["answers"].values()]
        result["reply_count"] = len(result["replies"])
    return result


def snapshot(room_id: str) -> dict:
    # 奖池连同 weight 一起下发：前端据此渲染概率文案，避免两处手写导致「界面在说谎」。
    return {
        "type": "activities",
        "room_id": room_id,
        "activities": [visible(activity) for activity in activities(room_id)],
        "prizes": [dict(prize) for prize in PRIZES],
        "wheel_prizes": [dict(prize) for prize in WHEEL_PRIZES],
        "horn": {"cost": HORN_COST, "cooldown": HORN_COOLDOWN, "max_length": HORN_MAX_LENGTH},
        "prompt": {"reward": PROMPT_REWARD, "seconds": PROMPT_SECONDS, "answer_max": PROMPT_ANSWER_MAX},
    }


def integer(data: dict, key: str, low: int, high: int) -> int:
    value = data.get(key)
    if isinstance(value, bool):
        raise ValueError("请填写有效数字")
    try:
        number = int(str(value))
    except (ValueError, TypeError):
        raise ValueError("请填写有效整数")
    if not low <= number <= high:
        raise ValueError(f"数值必须在 {low}～{high} 之间")
    return number


def credit(user_id: str, amount: int) -> None:
    # 在线用户先更新内存，离线注册用户直接更新数据库，红包过期也能退还。
    session = chat.manager.sessions.get(user_id) or next((s for s in chat.sessions.values() if s.user_id == user_id), None)
    if session:
        session.coins += amount
        chat.persist_session(session)
    elif not user_id.startswith("guest-"):
        with closing(chat.get_db()) as db:
            db.execute("UPDATE users SET coins=coins+? WHERE id=?", (amount, user_id))
            db.commit()


def score(session: Any, amount: int) -> None:
    with closing(chat.get_db()) as db:
        db.execute("INSERT INTO room_activity_scores VALUES (?, ?, ?, ?) ON CONFLICT(room_id,user_id) DO UPDATE SET score=score+excluded.score, nickname=excluded.nickname", (session.room_id, session.user_id, session.nickname, amount))
        db.commit()


def new_activity(session: Any, kind: str, **fields: Any) -> dict:
    return {"id": secrets.token_hex(8), "room_id": session.room_id, "kind": kind, "status": "open", "host_id": session.user_id, "host": session.nickname, "expires_at": time.time() + 120, **fields}


async def announce(session: Any, content: str, activity_kind: str = "") -> None:
    """往聊天区发一条活动提示。

    activity_kind 决定它在界面上怎么显示（判据见 app.ACTIVITY_STREAM_KINDS）：
    流水型会被前端收进「活动消息 N 条」折叠带，通知型照常单独占一行。
    档位由服务端定、随消息下发，前端不自己猜。
    """
    message = chat.save_message(session, content, "activity", activity_kind=activity_kind)
    await chat.manager.broadcast({"type": "message", "message": message}, room_id=session.room_id)


def finish(activity: dict) -> None:
    if activity["status"] != "open":
        return
    if activity["kind"] == "redpack":
        amount = sum(activity["remaining"])
        credit(activity["host_id"], amount)
        activity["refund"] = amount
        activity["remaining"] = []
    elif activity["kind"] == "prediction":
        activity["dice"] = secrets.randbelow(6) + 1
        activity["answer"] = "单数" if activity["dice"] % 2 else "双数"
        winners = [vote for vote in activity["answers"].values() if vote["choice"] == activity["answer"]]
        for vote in winners:
            credit(vote["id"], 10)
        activity["winners"] = [vote["name"] for vote in winners]
    activity["status"] = "finished"
    store(activity)


async def expire_rooms() -> None:
    with closing(chat.get_db()) as db:
        rows = db.execute("SELECT payload FROM room_activities WHERE status='open' AND expires_at<=?", (time.time(),)).fetchall()
    for row in rows:
        activity = json.loads(row[0])
        finish(activity)
        note = f"活动结束：{activity['kind']}"
        if activity["kind"] == "redpack":
            note = f"🧧 {activity['host']} 的红包过期，未领取的 {activity['refund']} 金币已退还"
        elif activity["kind"] == "prediction":
            if activity.get("winners"):
                note = f"🔮 竞猜开奖：骰子 {activity['dice']} 点（{activity['answer']}），猜中者每人获得 10 金币"
            else:
                note = f"🔮 竞猜开奖：骰子 {activity['dice']} 点（{activity['answer']}），本轮无人猜中"
        elif activity["kind"] in ("number", "song"):
            note = f"⏰ 猜谜时间到，答案是：{activity['answer']}"
        elif activity["kind"] in ("truth", "dare"):
            label = "真心话" if activity["kind"] == "truth" else "大冒险"
            # 有人回答才播报，无人回答也说一句 —— 否则玩家不知道题是过期了还是没人管。
            count = len(activity.get("answers") or {})
            note = (f"⏰ 本轮{label}结束，共 {count} 人回答"
                    if count else f"⏰ 本轮{label}结束，没有人回答这道题")
        else:
            note = "⏰ 本轮成语接龙结束"
        await chat.manager.broadcast({"type": "system", "message": note}, room_id=activity["room_id"])
        await chat.manager.broadcast(snapshot(activity["room_id"]), room_id=activity["room_id"])
        for session in list(chat.manager.sessions.values()):
            await chat.send_profile_update(session)


async def watch_rooms() -> None:
    ticks = 0
    while True:
        await expire_rooms()
        ticks += 1
        if ticks >= 60:
            # 每分钟顺带清理一次过期冷却记录和长期闲置的会话，避免长跑后只增不减。
            ticks = 0
            chat.purge_stale()
        await asyncio.sleep(1)


def require_member(session: Any) -> None:
    """游客没有数据库记录，金币花掉就没了，因此不允许参与消耗金币的玩法。"""
    if session.role == "guest":
        raise ValueError("游客不能使用金币功能，请注册账号后再来")


async def handle_action(session: Any, data: dict) -> None:
    try:
        action = data.get("action", "")
        if action == "refresh":
            await session.send(snapshot(session.room_id))
            return
        # 限制单人连续操作，避免手误重复扣费。时间戳在动作真正通过校验后才刷新，
        # 否则参数写错、金币不足这类失败也会白白占用冷却，用户要点两次才成功。
        now = time.time()
        if now - getattr(session, "last_activity", 0) < 0.6:
            raise ValueError("操作太快，请稍等再试")
        result: dict[str, Any] = {}
        note = ""
        # 默认拿动作名当活动类型：绝大多数分支的提示与动作一一对应（抢红包=redpack_claim、
        # 投票=prediction_vote、抽奖=lottery……）。只有 game_answer 一个动作会产出两种
        # 不同性质的提示（接龙流水 / 答对喜报），在分支里单独覆盖。
        note_kind = action
        horn: dict[str, Any] | None = None
        if action in ("lottery", "wheel"):
            require_member(session)
            cost = 5 if action == "lottery" else 10
            # ★ 余额一律走 can_spend_coins / spend_coins（管理员的金币是展示用的 ∞）：
            #   直接比 session.coins 会出现「界面写着 ∞，却提示需要 5 金币才能参加」。
            if not chat.can_spend_coins(session, cost):
                raise ValueError(f"需要 {cost} 金币才能参加")
            prizes = PRIZES if action == "lottery" else WHEEL_PRIZES
            pool = [index for index, prize in enumerate(prizes) for _ in range(prize["weight"])]
            index = secrets.choice(pool)
            prize = prizes[index]
            # 先扣后加：写成 `+= prize.get("coins", 0) - cost` 的话，
            # 「管理员不扣币」那条分支就被绕过去了（管理员余额会被扣成负数）。
            chat.spend_coins(session, cost)
            session.coins += prize.get("coins", 0)
            if "item" in prize:
                session.inventory[prize["item"]] = session.inventory.get(prize["item"], 0) + prize["count"]
            result = {"index": index, "prize": prize["label"], "cost": cost}
            note = f"{'🎰 抽奖' if action == 'lottery' else '🎡 转盘'}：{session.nickname} 消耗 {cost} 金币，抽中「{prize['label']}」"
        elif action == "redpack_create":
            require_member(session)
            total = integer(data, "total", 1, 500)
            count = integer(data, "count", 1, 50)
            if total < count or not chat.can_spend_coins(session, total):
                raise ValueError("金币不足，或红包总额小于份数")
            if any(a["status"] == "open" and a["kind"] == "redpack" and a["host_id"] == session.user_id for a in activities(session.room_id)):
                raise ValueError("你已有未结束的红包")
            pieces, left = [], total
            for slots in range(count, 1, -1):
                value = 1 + secrets.randbelow(min(left - slots + 1, max(1, 2 * left // slots)))
                pieces.append(value)
                left -= value
            pieces.append(left)
            chat.spend_coins(session, total)
            activity = new_activity(session, "redpack", total=total, remaining=pieces, claims={}, count=count)
            store(activity)
            result = {"id": activity["id"]}
            note = f"🧧 {session.nickname} 发出 {total} 金币拼手气红包，共 {count} 份，2 分钟内可领取"
        elif action == "redpack_claim":
            require_member(session)
            activity = find(str(data.get("id", "")), session.room_id)
            if activity["kind"] != "redpack" or activity["status"] != "open" or activity["expires_at"] <= now:
                raise ValueError("红包已领完或过期")
            if session.user_id in activity["claims"]:
                raise ValueError("每人每个红包只能领一次")
            # 随机摘一份，而不是固定 pop() 取末尾：生成时首份受 2*left//slots 上限压制偏小，
            # 按顺序发放会让先抢的人系统性吃亏（实测第 1 位比第 10 位少约 1.1 金币）。
            amount = activity["remaining"].pop(secrets.randbelow(len(activity["remaining"])))
            activity["claims"][session.user_id] = {"id": session.user_id, "name": session.nickname, "amount": amount}
            session.coins += amount
            if not activity["remaining"]:
                activity["status"] = "finished"
            store(activity)
            result = {"amount": amount}
            note = f"🧧 {session.nickname} 从 {activity['host']} 的红包中领取了 {amount} 金币"
        elif action == "prediction_start":
            require_member(session)
            if any(a["status"] == "open" and a["kind"] == "prediction" for a in activities(session.room_id)):
                raise ValueError("本房间已有进行中的竞猜")
            activity = new_activity(session, "prediction", question="开奖骰子的点数是单数还是双数？", options=["单数", "双数"], answers={})
            activity["expires_at"] = now + 60
            store(activity)
            note = "🔮 房间竞猜开始：猜骰子单数或双数，参加花费 5 金币，60 秒后开奖，猜中奖励 10 金币"
        elif action == "prediction_vote":
            require_member(session)
            activity = find(str(data.get("id", "")), session.room_id)
            choice = data.get("choice")
            if activity["kind"] != "prediction" or activity["status"] != "open" or activity["expires_at"] <= now or choice not in activity["options"]:
                raise ValueError("竞猜已结束或选项无效")
            if session.user_id in activity["answers"] or not chat.can_spend_coins(session, 5):
                raise ValueError("每轮只能猜一次，且需要 5 金币")
            chat.spend_coins(session, 5)
            activity["answers"][session.user_id] = {"id": session.user_id, "name": session.nickname, "choice": choice}
            store(activity)
            note = f"🔮 {session.nickname} 已参与竞猜，选择「{choice}」"
        elif action == "game_start":
            kind = data.get("game")
            if kind not in ("number", "song", "idiom"):
                raise ValueError("请选择有效小游戏")
            if any(a["status"] == "open" and a["kind"] in ("number", "song", "idiom") for a in activities(session.room_id)):
                raise ValueError("房间已有小游戏，请等本轮结束")
            if kind == "number":
                activity = new_activity(session, kind, answer=secrets.randbelow(100)+1, hint="猜 1～100 的整数；答错会提示大了或小了", attempts=0, answered=[])
            elif kind == "song":
                answer, hint = secrets.choice(SONGS)
                activity = new_activity(session, kind, answer=answer, hint=hint, attempts=0, answered=[])
            else:
                activity = new_activity(session, kind, word="一心一意", hint="尾字接首字；使用内置成语词库，不支持同音字", used=["一心一意"], last_user="")
            store(activity)
            note = f"🎮 {session.nickname} 发起小游戏：{ {'number':'猜数字', 'song':'猜歌名', 'idiom':'成语接龙'}[kind]}。{activity['hint']}"
        elif action == "game_answer":
            activity = find(str(data.get("id", "")), session.room_id)
            if activity["status"] != "open" or activity["expires_at"] <= now or activity["kind"] not in ("number", "song", "idiom"):
                raise ValueError("小游戏已结束")
            answer = str(data.get("answer", "")).strip()[:40]
            if activity["kind"] == "idiom":
                if answer not in IDIOMS or answer in activity["used"] or answer[0] != activity["word"][-1]:
                    raise ValueError("请接一个词库中的成语，首字要接上前一词尾字，不能重复")
                if activity["last_user"] == session.user_id:
                    raise ValueError("请等其他人接一句再继续")
                activity["word"] = answer
                activity["used"].append(answer)
                activity["last_user"] = session.user_id
                session.coins += 2
                score(session, 2)
                note = f"📜 {session.nickname} 接上「{answer}」，获得 2 金币和 2 房间金币"
                note_kind = "idiom"
            else:
                # ★ 每人每轮只能猜一次 —— 少了这道闸门就是零成本印钞机：
                #   猜数字答错会提示「大了 / 小了」，一个人用二分法 7 次必中，
                #   而这一路既不花金币也不要求注册，答对就是 +10。
                #   （猜歌名同理：总共就 8 首歌，最多 8 次就能试完。）
                #   加上这道闸门后单人「一轮只能猜一次」（中奖概率 1/100），
                #   再叠加「同一房间同时只有一轮 + 本轮 120 秒才过期」，
                #   刷币速率压到每分钟不到 0.01 金币，可以忽略。
                answered = activity.setdefault("answered", [])
                if session.user_id in answered:
                    raise ValueError("每人每轮只能猜一次，等这一轮结束后再来")
                if activity["kind"] == "number":
                    # 先校验输入合法，再记「这轮猜过」：参数写错不该白耗一次机会。
                    number = integer({"answer": answer}, "answer", 1, 100)
                answered.append(session.user_id)
                correct = str(activity["answer"]) == answer
                if not correct:
                    if activity["kind"] == "number":
                        result["hint"] = "大了，再小一点" if number > activity["answer"] else "小了，再大一点"
                    else:
                        result["hint"] = "歌名不对，再想想提示"
                activity["attempts"] += 1
                if correct:
                    activity["status"] = "finished"
                    activity["winner"] = session.nickname
                    session.coins += 10
                    score(session, 10)
                    note = f"🏆 {session.nickname} 答对「{answer}」，获得 10 金币和 10 房间金币"
                    note_kind = "game_win"
                else:
                    note = ""
            store(activity)
        elif action == "prompt_answer":
            activity = find(str(data.get("id", "")), session.room_id)
            if activity["kind"] not in ("truth", "dare") or activity["status"] != "open" or activity["expires_at"] <= now:
                raise ValueError("这道题已经结束了")
            if session.user_id in activity["answers"]:
                raise ValueError("每人只能回答一道题，换题再来")
            # 回答就是聊天内容，必须走违禁词过滤（打码照存）。
            # 与大喇叭相反：付费内容才「违规就拒发」，这里是免费发言，走普通聊天那一套。
            text = str(data.get("text", "")).strip()[:PROMPT_ANSWER_MAX]
            if not text:
                raise ValueError("请先写下你的回答")
            text, violated = chat.moderate_content(text)
            activity["answers"][session.user_id] = {"id": session.user_id, "name": session.nickname, "text": text}
            session.coins += PROMPT_REWARD
            score(session, PROMPT_REWARD)
            store(activity)
            result = {"id": activity["id"], "reward": PROMPT_REWARD}
            flag = "（已打码）" if violated else ""
            note = f"{'💬' if activity['kind'] == 'truth' else '⚡'} {session.nickname} 回答了{activity['host']}的{('真心话' if activity['kind'] == 'truth' else '大冒险')}：{text}{flag}"
        elif action == "horn":
            require_member(session)
            content, violated = chat.moderate_content(str(data.get("content", "")).strip()[:HORN_MAX_LENGTH])
            if not content:
                raise ValueError("请先输入大喇叭内容")
            # 付费内容不做打码照发：含违规词直接退回，学生可以改完再喊，且一分钱不扣。
            if violated:
                raise ValueError("大喇叭内容包含违规词，请修改后再发")
            waited = now - getattr(session, "last_horn", 0)
            if waited < HORN_COOLDOWN:
                raise ValueError(f"大喇叭每 {HORN_COOLDOWN} 秒只能喊一次，请再等 {int(HORN_COOLDOWN - waited) + 1} 秒")
            # 先判断冷却，再判断余额：冷却中的重复操作应明确提示等待时间，
            # 不能因为余额不足而误显示成「需要 10 金币」。
            if not chat.can_spend_coins(session, HORN_COST):
                raise ValueError(f"需要 {HORN_COST} 金币才能使用大喇叭")
            chat.spend_coins(session, HORN_COST)
            session.last_horn = now
            result = {"content": content, "cost": HORN_COST}
            note = f"📣 {session.nickname} 用大喇叭喊：{content}"
            # 单独一条消息类型：前端用置顶滚动横幅展示，而不是只在聊天记录里躺一行。
            horn = {"type": "horn", "sender_id": session.user_id, "nickname": session.nickname, "level": session.level, "level_title": chat.level_title(session.level), "content": content}
        elif action in ("truth", "dare"):
            # 抽出来的题是**房间里的一个活动**，不是抽题者私有的东西 ——
            # 全房间都能看到题目并提交回答（见 prompt_answer）。
            # 同一房间同时只允许一题进行中：否则可以狂抽题、狂自己答，
            # PROMPT_REWARD 就成了一台合法的印钞机（和 prediction_start 同一条闸门）。
            if any(a["status"] == "open" and a["kind"] in ("truth", "dare") for a in activities(session.room_id)):
                raise ValueError("本房间已有一道真心话/大冒险，请等本轮结束")
            prompt = secrets.choice(TRUTHS if action == "truth" else DARES)
            activity = new_activity(session, action, prompt=prompt, answers={})
            activity["expires_at"] = now + PROMPT_SECONDS
            store(activity)
            result = {"id": activity["id"], "prompt": prompt, "reward": PROMPT_REWARD}
            note = f"{'💬 真心话' if action == 'truth' else '⚡ 大冒险'}：{session.nickname} 抽到「{prompt}」，{PROMPT_SECONDS // 60} 分钟内全房间可以回答，回答 {PROMPT_REWARD} 金币"
        else:
            raise ValueError("未知互动操作")
        session.last_activity = now
        chat.persist_session(session)
        # 所有金额变化在第一次 await 之前完成，不给并发领取留下重复入账的窗口。
        # 必须回 self_public：前端收到会直接覆盖 user，少了 inventory 道具栏就会空掉。
        await session.send({"type": "interaction_result", "action": action, "result": result, "user": session.self_public()})
        if note:
            await announce(session, note, note_kind)
        if horn:
            # 大喇叭横幅跨房间发送给所有在线用户，聊天记录仍留在发送者房间。
            await chat.manager.broadcast(horn)
        await chat.send_profile_update(session)
        await chat.manager.broadcast(snapshot(session.room_id), room_id=session.room_id)
    except ValueError as error:
        await session.send({"type": "interaction_error", "action": action, "message": str(error)})
    except Exception:
        # 未预期的异常也必须回一条结果，否则前端会一直停在“正在处理”。
        await session.send({"type": "interaction_error", "action": action, "message": "操作失败，请稍后再试"})


def _rank_number(value: Any) -> float | int:
    """把榜单分数压成「既能排序、又能进 JSON」的数字。

    ★ 线上会话走的是 public()，管理员的 coins 是展示用的字符串 "∞"。
      直接拿去排序会抛 `TypeError: bad operand type for unary -: 'str'` ——
      表现是「只要管理员在线，谁点开财富榜都是 500」。
      这里再兜一次底：以后榜单加别的字段，也不会因为一个字符串把整页打崩。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def leaderboard(room_id: str, kind: str) -> list[dict]:
    with closing(chat.get_db()) as db:
        registered = {str(row["id"]): dict(row) for row in db.execute("SELECT id,nickname,avatar,coins,charm,exp,level FROM users")}
        for session in chat.manager.sessions.values():
            # ★ 在线会话覆盖时 coins 一律取**库里的真值**（session.coins，不是 public() 里的 "∞"）：
            #   财富榜是台账，得能排序、能比大小（口径同控制台成员列表）。
            registered[session.user_id] = {**session.public(), "id": session.user_id, "coins": session.coins}
        if kind == "room":
            scores = {row["user_id"]: row["score"] for row in db.execute("SELECT user_id,score FROM room_activity_scores WHERE room_id=?", (room_id,))}
        elif kind == "active":
            scores = {row[0]: row[1] for row in db.execute("SELECT sender_id,COUNT(*) FROM messages WHERE kind IN ('text','private') GROUP BY sender_id")}
        else:
            field = "charm" if kind == "charm" else "coins"
            scores = {key: _rank_number(value[field]) for key, value in registered.items()}
    return [{"id": key, "nickname": value["nickname"], "avatar": value["avatar"], "level": value["level"], "score": scores.get(key, 0)} for key, value in sorted(registered.items(), key=lambda pair: (-scores.get(pair[0], 0), pair[0]))][:20]
