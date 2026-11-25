"""Relais WebSocket devant le service conversation (speech-to-speech).

Chaque requête doit porter `Authorization: Bearer <CONVERSATION_GATE_KEY>`, sinon 401 sans corps.
Une ouverture WebSocket autorisée est relayée vers le service avec le même chemin et la même
query string. Une seule trame client est retirée : l'événement `input_audio_buffer.clear` que
l'appel vocal d'Open WebUI envoie à chaque coupure du micro, que speech-to-speech ne connaît pas
encore et que le bridge traiterait comme une erreur fatale. Tout le reste passe sans changement.

Les journaux ne contiennent ni en-tête, ni clé, ni chemin, ni contenu de trame.
"""

import asyncio
import hmac
import itertools
import json
import logging
import os
import sys

import aiohttp
from aiohttp import web
from yarl import URL

UPSTREAM = "ws://conversation:8765"
HOST = "0.0.0.0"
PORT = 8080
KEY_VARIABLE = "CONVERSATION_GATE_KEY"
DROPPED_TYPE = "input_audio_buffer.clear"
MAX_MESSAGE_SIZE = 16 * 2**20
# Codes qui ne se mettent jamais dans une trame Close (RFC 6455, 7.4.1).
NOT_SENDABLE = {1005, 1006, 1015}

KEY = web.AppKey("key", str)
UPSTREAM_KEY = web.AppKey("upstream", str)
SESSION = web.AppKey("session", aiohttp.ClientSession)

log = logging.getLogger("gate")
connection_ids = itertools.count(1)


def load_key(environ=None) -> str:
    key = (os.environ if environ is None else environ).get(KEY_VARIABLE, "")
    if not key:
        sys.exit(f"{KEY_VARIABLE} est requise et ne doit pas être vide : le relais refuse de démarrer.")
    return key


def is_authorized(request: web.Request, key: str) -> bool:
    received = request.headers.get("Authorization", "")
    expected = f"Bearer {key}"
    return hmac.compare_digest(
        received.encode("utf-8", "surrogateescape"), expected.encode("utf-8", "surrogateescape")
    )


def is_dropped(text: str) -> bool:
    """Vrai pour l'événement à retirer. Le test de sous-chaîne évite de lire le JSON des trames audio."""
    if DROPPED_TYPE not in text:
        return False
    try:
        event = json.loads(text)
    except ValueError:
        return False
    return isinstance(event, dict) and event.get("type") == DROPPED_TYPE


def close_code(ws, default: int = 1000) -> int:
    code = ws.close_code
    if code is None or code == 1005:
        return default
    return 1011 if code in NOT_SENDABLE else code


async def client_to_upstream(client, upstream, stats: dict) -> None:
    async for msg in client:
        if msg.type == aiohttp.WSMsgType.TEXT:
            if is_dropped(msg.data):
                stats["dropped"] += 1
                continue
            await upstream.send_str(msg.data)
        elif msg.type == aiohttp.WSMsgType.BINARY:
            await upstream.send_bytes(msg.data)


async def upstream_to_client(upstream, client) -> None:
    async for msg in upstream:
        if msg.type == aiohttp.WSMsgType.TEXT:
            await client.send_str(msg.data)
        elif msg.type == aiohttp.WSMsgType.BINARY:
            await client.send_bytes(msg.data)


async def relay(request: web.Request) -> web.StreamResponse:
    key = request.app[KEY]
    if not is_authorized(request, key):
        return web.Response(status=401)
    client = web.WebSocketResponse(max_msg_size=MAX_MESSAGE_SIZE)
    if not client.can_prepare(request).ok:
        return web.Response(status=404)
    await client.prepare(request)

    conn_id = next(connection_ids)
    stats = {"dropped": 0}
    log.info("open id=%d", conn_id)
    code = 1011
    try:
        try:
            upstream = await request.app[SESSION].ws_connect(
                URL(request.app[UPSTREAM_KEY] + request.raw_path, encoded=True),
                max_msg_size=MAX_MESSAGE_SIZE,
            )
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
            await client.close(code=1011)
            return client
        try:
            tasks = [
                asyncio.ensure_future(client_to_upstream(client, upstream, stats)),
                asyncio.ensure_future(upstream_to_client(upstream, client)),
            ]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            if client.closed or client.close_code is not None:
                code = close_code(client)
            else:
                code = close_code(upstream)
            await upstream.close(code=code)
            await client.close(code=code)
        finally:
            await upstream.close()
    finally:
        log.info("close id=%d code=%d dropped_clear=%d", conn_id, code, stats["dropped"])
    return client


async def open_session(app: web.Application):
    async with aiohttp.ClientSession() as session:
        app[SESSION] = session
        yield


def create_app(key: str, upstream: str = UPSTREAM) -> web.Application:
    if not key:
        raise ValueError(f"{KEY_VARIABLE} ne doit pas être vide")
    app = web.Application()
    app[KEY] = key
    app[UPSTREAM_KEY] = upstream.rstrip("/")
    app.cleanup_ctx.append(open_session)
    app.router.add_route("*", "/{tail:.*}", relay)
    return app


def main(environ=None) -> None:
    key = load_key(environ)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    web.run_app(create_app(key), host=HOST, port=PORT, access_log=None, print=None)


if __name__ == "__main__":
    main()
