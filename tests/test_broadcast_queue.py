"""广播背压测试：一个卡住的客户端不能拖住别人，也不能无限吃内存。

这两条是「每条连接一个发送队列」这个改动的核心契约。
用假 socket 就能精确复现（不需要真网络、不需要起服务）：
``send_json`` 永远不返回 == 学生机网线被拔 / 浏览器标签被冻住。

注意两点实现细节，否则测试本身会变成噪音：
1. ``IsolatedAsyncioTestCase`` 强制 ``debug=True``，事件循环慢一个量级
   （实测发 50 条要 0.3 秒，不是 0.002 秒），所以不能拿「睡固定时长」当同步手段，
   一律用 ``wait_until`` 轮询到达。
2. 收尾必须等发送协程真的结束（``manager.drain()``）。留下未收尾的任务，
   unittest 关事件循环时会卡死。
"""
import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as chat


class FakeSocket:
    """block=True 时 send_json 永不返回 —— 模拟「只连不读」的客户端。"""

    def __init__(self, block: bool = False) -> None:
        self.events: list[dict] = []
        self.block = block
        self.closed: int | None = None
        self.close_reason = ""

    async def send_json(self, data: dict) -> None:
        if self.block:
            await asyncio.Event().wait()
        self.events.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = code
        self.close_reason = reason


class BroadcastQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = chat.ConnectionManager()

    async def asyncTearDown(self):
        # 把发送协程和「断开慢客户端」的兜底任务等干净，一个都不留给 asyncio 去清理。
        await self.manager.drain()

    async def wait_until(self, condition, timeout: float = 15.0) -> bool:
        """轮询等待条件成立。debug 模式下事件循环很慢，固定 sleep 不可靠。"""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if condition():
                return True
            await asyncio.sleep(0.01)
        return False

    async def make_client(self, user_id: str, nickname: str, block: bool = False):
        socket = FakeSocket(block=block)
        session = chat.Session(f"token-{user_id}", user_id, nickname, "user")
        await self.manager.add(session, socket)
        return session, socket

    async def test_a_stuck_client_does_not_block_the_room(self):
        """一个 send_json 永远不返回的客户端，不该拖慢同房间其他人。"""
        _, fast_socket = await self.make_client("1", "快的")
        await self.make_client("2", "卡住的", block=True)

        start = time.perf_counter()
        for index in range(50):
            await self.manager.broadcast({"type": "message", "n": index})
        elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 0.5, f"广播被卡住的客户端拖住了，用了 {elapsed:.3f}s")
        delivered = await self.wait_until(lambda: len(fast_socket.events) == 50)
        self.assertTrue(delivered, f"正常客户端只收到 {len(fast_socket.events)}/50 条")

    async def test_queue_overflow_disconnects_the_stuck_client(self):
        """积压到上限 → 摘出连接表并断开，而不是无限吃内存。

        注意这条必须**由积压触发**：发送超时那条路（5 秒后断开）也会给出 4008，
        所以这里把等待上限压到 2 秒、并且核对断开原因，免得测试蹭到超时那条路还假装通过。
        """
        session, socket = await self.make_client("2", "卡住的", block=True)

        for index in range(chat.BROADCAST_QUEUE_SIZE + 10):
            await self.manager.broadcast({"type": "message", "n": index})

        dropped = await self.wait_until(
            lambda: self.manager.sessions.get("2") is None, timeout=2.0
        )
        self.assertTrue(dropped, "积压超限的客户端还挂在连接表里")
        self.assertEqual(socket.closed, 4008, "没有用「积压过多」的错误码断开")
        self.assertIn("积压", socket.close_reason, f"是被别的路径断开的：{socket.close_reason!r}")
        self.assertIsNone(session.outbox, "断开之后还留着发送队列")

    async def test_normal_client_is_not_mistakenly_dropped(self):
        """对照组：跟得上的客户端，在同样消息量下必须全部收到、且不被误踢。"""
        total = chat.BROADCAST_QUEUE_SIZE + 50
        _, socket = await self.make_client("3", "正常的")

        # 一条一条来（每条之间让出一次），贴近真实的推送节奏：
        # 真实场景下 broadcast 不是紧密连发的，发送协程有的是机会跟上。
        for index in range(total):
            await self.manager.broadcast({"type": "message", "n": index})
            await asyncio.sleep(0.002)

        delivered = await self.wait_until(lambda: len(socket.events) == total)
        self.assertIsNotNone(self.manager.sessions.get("3"), "正常客户端被误踢了")
        self.assertIsNone(socket.closed, "正常客户端被误断开")
        self.assertTrue(delivered, f"正常客户端只收到 {len(socket.events)}/{total} 条")

    async def test_broadcast_does_not_touch_the_network(self):
        """广播本身必须是「同步塞队列」：卡住的客户端不能让它产生等待。"""
        await self.make_client("4", "卡住的", block=True)
        await asyncio.sleep(0.05)

        start = time.perf_counter()
        await self.manager.broadcast({"type": "message"})
        elapsed = time.perf_counter() - start
        # 真发网络时，对端不读会让这一句挂到超时（5 秒）；塞队列则是微秒级。
        self.assertLess(elapsed, 0.05, f"broadcast 仍在等网络，用了 {elapsed:.3f}s")


if __name__ == "__main__":
    unittest.main()
