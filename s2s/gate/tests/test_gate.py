import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

GATE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(GATE_DIR))

import gate  # noqa: E402

KEY = "cle-de-test-0123456789"
AUTH = {"Authorization": f"Bearer {KEY}"}
CLEAR = json.dumps({"type": "input_audio_buffer.clear"})
APPEND = json.dumps({"type": "input_audio_buffer.append", "audio": "AAAA"})


class FakeUpstream:
    """Serveur WebSocket de remplacement : renvoie chaque trame telle quelle et note ce qu'il reçoit."""

    def __init__(self):
        self.received: list[tuple[str, object]] = []
        self.path_qs: list[str] = []
        self.close_code: int | None = None
        self.closed = asyncio.Event()

    async def handler(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.path_qs.append(request.path_qs)
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                self.received.append(("text", msg.data))
                if msg.data == "ferme-moi":
                    await ws.close(code=4001)
                    break
                if msg.data == "emets-clear":
                    await ws.send_str(CLEAR)
                    continue
                await ws.send_str(msg.data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                self.received.append(("binary", msg.data))
                await ws.send_bytes(msg.data)
        self.close_code = ws.close_code
        self.closed.set()
        return ws


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def start(app: web.Application) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


@asynccontextmanager
async def stack(key: str = KEY, upstream_up: bool = True):
    fake = FakeUpstream()
    fake_app = web.Application()
    fake_app.router.add_route("*", "/{tail:.*}", fake.handler)
    fake_runner, fake_port = await start(fake_app)
    if not upstream_up:
        await fake_runner.cleanup()
    gate_runner, gate_port = await start(gate.create_app(key, upstream=f"ws://127.0.0.1:{fake_port}"))
    try:
        yield f"127.0.0.1:{gate_port}", fake
    finally:
        await gate_runner.cleanup()
        if upstream_up:
            await fake_runner.cleanup()


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


def test_request_without_key_gets_401_with_empty_body():
    async def go():
        async with stack() as (addr, _), aiohttp.ClientSession() as http:
            async with http.get(f"http://{addr}/v1/realtime") as resp:
                assert resp.status == 401
                assert await resp.read() == b""
            with pytest.raises(aiohttp.WSServerHandshakeError) as err:
                await http.ws_connect(f"http://{addr}/v1/realtime")
            assert err.value.status == 401

    run(go())


@pytest.mark.parametrize(
    "header",
    [
        f"Bearer {KEY}x",
        f"Bearer {KEY[:-1]}",
        "Bearer autre",
        f"bearer {KEY}",
        KEY,
        "Bearer ",
        f"Basic {KEY}",
    ],
)
def test_wrong_key_gets_401(header):
    async def go():
        async with stack() as (addr, fake), aiohttp.ClientSession() as http:
            headers = {"Authorization": header}
            async with http.get(f"http://{addr}/", headers=headers) as resp:
                assert resp.status == 401
                assert await resp.read() == b""
            with pytest.raises(aiohttp.WSServerHandshakeError) as err:
                await http.ws_connect(f"http://{addr}/v1/realtime", headers=headers)
            assert err.value.status == 401
            assert fake.path_qs == []

    run(go())


def test_startup_is_refused_without_key():
    for env in ({}, {"CONVERSATION_GATE_KEY": ""}):
        with pytest.raises(SystemExit) as err:
            gate.load_key(env)
        assert err.value.code not in (0, None)
    assert gate.load_key({"CONVERSATION_GATE_KEY": KEY}) == KEY
    with pytest.raises(ValueError):
        gate.create_app("")


def test_process_exits_with_an_error_without_key():
    env = {k: v for k, v in os.environ.items() if k != "CONVERSATION_GATE_KEY"}
    for extra in ({}, {"CONVERSATION_GATE_KEY": ""}):
        proc = subprocess.run(
            [sys.executable, str(GATE_DIR / "gate.py")],
            env={**env, **extra},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode != 0
        assert "CONVERSATION_GATE_KEY" in proc.stderr


def test_text_and_binary_frames_go_both_ways_unchanged_and_in_order():
    frames = [
        ("text", "premier"),
        ("binary", b"\x00\x01\xff\xfe"),
        ("text", json.dumps({"type": "session.update", "session": {"a": [1, 2]}}, indent=3)),
        ("binary", b""),
        ("text", "é 日本語"),
        ("binary", bytes(range(256)) * 40),
    ]

    async def go():
        async with stack() as (addr, fake), aiohttp.ClientSession() as http:
            async with http.ws_connect(f"http://{addr}/v1/realtime", headers=AUTH) as ws:
                for kind, data in frames:
                    await (ws.send_str(data) if kind == "text" else ws.send_bytes(data))
                echoes = []
                for _ in frames:
                    msg = await ws.receive()
                    kind = "text" if msg.type == aiohttp.WSMsgType.TEXT else "binary"
                    echoes.append((kind, msg.data))
            assert fake.received == frames
            assert echoes == frames

    run(go())


def test_clear_event_is_dropped_and_the_next_append_is_forwarded():
    other = json.dumps({"type": "conversation.item.create", "item": {"text": "input_audio_buffer.clear"}})
    spaced = '{ "type" :  "input_audio_buffer.clear" , "event_id": "x" }'

    async def go():
        async with stack() as (addr, fake), aiohttp.ClientSession() as http:
            async with http.ws_connect(f"http://{addr}/v1/realtime", headers=AUTH) as ws:
                await ws.send_str(CLEAR)
                await ws.send_str(spaced)
                await ws.send_str(APPEND)
                await ws.send_str(other)
                await ws.send_str("pas du json input_audio_buffer.clear")
                await ws.send_str("[\"input_audio_buffer.clear\"]")
                got = [(await ws.receive()).data for _ in range(4)]
            assert fake.received == [
                ("text", APPEND),
                ("text", other),
                ("text", "pas du json input_audio_buffer.clear"),
                ("text", "[\"input_audio_buffer.clear\"]"),
            ]
            assert got == [APPEND, other, "pas du json input_audio_buffer.clear", "[\"input_audio_buffer.clear\"]"]

    run(go())


def test_clear_event_from_the_server_is_forwarded_to_the_client():
    async def go():
        async with stack() as (addr, _), aiohttp.ClientSession() as http:
            async with http.ws_connect(f"http://{addr}/v1/realtime", headers=AUTH) as ws:
                await ws.send_str("emets-clear")
                msg = await ws.receive()
                assert (msg.type, msg.data) == (aiohttp.WSMsgType.TEXT, CLEAR)

    run(go())


def test_path_and_query_are_preserved():
    async def go():
        async with stack() as (addr, fake), aiohttp.ClientSession() as http:
            for target in ("/v1/realtime?model=medium&x=a%20b", "/autre/chemin", "/"):
                async with http.ws_connect(f"http://{addr}{target}", headers=AUTH) as ws:
                    await ws.send_str("ping")
                    await ws.receive()
            assert fake.path_qs == ["/v1/realtime?model=medium&x=a%20b", "/autre/chemin", "/"]

    run(go())


def test_upstream_down_closes_the_client_with_1011():
    async def go():
        async with stack(upstream_up=False) as (addr, _), aiohttp.ClientSession() as http:
            async with http.ws_connect(f"http://{addr}/v1/realtime", headers=AUTH) as ws:
                msg = await ws.receive()
                assert msg.type == aiohttp.WSMsgType.CLOSE
                assert msg.data == 1011

    run(go())


def test_authorized_request_that_is_not_a_websocket_gets_404():
    async def go():
        async with stack() as (addr, fake), aiohttp.ClientSession() as http:
            async with http.get(f"http://{addr}/v1/realtime", headers=AUTH) as resp:
                assert resp.status == 404
            async with http.post(f"http://{addr}/", headers=AUTH, data=b"x") as resp:
                assert resp.status == 404
            assert fake.path_qs == []

    run(go())


def test_client_close_code_reaches_the_upstream():
    async def go():
        async with stack() as (addr, fake), aiohttp.ClientSession() as http:
            async with http.ws_connect(f"http://{addr}/v1/realtime", headers=AUTH) as ws:
                await ws.send_str("ping")
                await ws.receive()
                await ws.close(code=4002)
            await asyncio.wait_for(fake.closed.wait(), 5)
            assert fake.close_code == 4002

    run(go())


def test_upstream_close_code_reaches_the_client():
    async def go():
        async with stack() as (addr, _), aiohttp.ClientSession() as http:
            async with http.ws_connect(f"http://{addr}/v1/realtime", headers=AUTH) as ws:
                await ws.send_str("ferme-moi")
                msg = await ws.receive()
                assert msg.type == aiohttp.WSMsgType.CLOSE
                assert msg.data == 4001

    run(go())


def test_logs_one_line_per_open_and_close_with_the_dropped_count_and_no_secret(caplog):
    secret_frame = "contenu-de-trame-secret"

    async def go():
        async with stack() as (addr, fake), aiohttp.ClientSession() as http:
            async with http.ws_connect(f"http://{addr}/v1/realtime?model=medium", headers=AUTH) as ws:
                await ws.send_str(CLEAR)
                await ws.send_str(CLEAR)
                await ws.send_str(secret_frame)
                await ws.receive()
            await asyncio.wait_for(fake.closed.wait(), 5)
            await asyncio.sleep(0.2)

    with caplog.at_level(logging.DEBUG, logger="gate"):
        run(go())
    lines = [r.getMessage() for r in caplog.records if r.name == "gate"]
    assert len([m for m in lines if m.startswith("open ")]) == 1
    closes = [m for m in lines if m.startswith("close ")]
    assert len(closes) == 1
    assert "dropped_clear=2" in closes[0]
    text = "\n".join(lines)
    for forbidden in (KEY, secret_frame, "Bearer", "model=medium", "input_audio_buffer"):
        assert forbidden not in text
