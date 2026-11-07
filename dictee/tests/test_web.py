"""Page de configuration : connexion PocketID, clés, réglages de chacun et aperçu des voix."""

import asyncio
import base64
import dataclasses
import html
import json
import logging
import re
import socket
import sqlite3
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx
import itsdangerous
import pytest
from authlib.integrations.starlette_client import OAuthError
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

from dictee.app import create_app
from dictee.db import StoredSettings
from dictee.nemo import TTS_SAMPLE_RATE, NemoOverloaded, NemoUnavailable
from dictee.profile import DEFAULT_INSTRUCTIONS
from dictee.web import MAX_FORM_BYTES, PREVIEW_TEXT, PreviewCache, _mp3_response, build_oauth

USERINFO = {"sub": "sub-1", "email": "a@example.org", "name": "A"}
MP3_MAGIC = (b"ID3", b"\xff\xfb", b"\xff\xf3")
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; media-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
# Chaque route POST de la page, avec de quoi la soumettre : la garde du dernier test de ce fichier les compte.
POSTS = [
    ("/keys", {"label": "Android"}),
    ("/keys/1/revoke", {}),
    ("/settings", {"glossary": "Nemotron"}),
    ("/settings/reset-instructions", {}),
    ("/logout", {}),
]
POST_ROUTES = {"/keys", "/keys/{key_id:int}/revoke", "/settings", "/settings/reset-instructions", "/logout"}


def sign_in(client, userinfo=USERINFO, *, endpoint=None):
    """Fait revenir PocketID : seuls l'échange du code et le point `userinfo` sont remplacés, le reste est réel.

    `userinfo` est ce que le jeton d'identité porte ; `endpoint`, ce que répond le point `userinfo` de PocketID
    (rien par défaut), ou l'exception qu'il lève.
    """
    pocketid = client.app.state.oauth.pocketid
    pocketid.authorize_access_token = AsyncMock(return_value={"access_token": "token", "userinfo": userinfo})
    if isinstance(endpoint, Exception):
        pocketid.userinfo = AsyncMock(side_effect=endpoint)
    else:
        pocketid.userinfo = AsyncMock(return_value={} if endpoint is None else endpoint)
    return client.get("/auth/callback", params={"code": "c", "state": "s"}, follow_redirects=False)


@pytest.fixture
def pocketid(monkeypatch):
    """Faux PocketID sur le port local : il sert ses métadonnées par HTTP et note les chemins demandés.

    Un vrai serveur plutôt qu'un faux transport : Authlib choisit lui-même son client HTTP (httpx2 ou httpx).
    """
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    asked = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            asked.append(self.path)
            base = f"http://127.0.0.1:{self.server.server_port}"
            body = json.dumps(
                {
                    "issuer": base,
                    "authorization_endpoint": f"{base}/authorize",
                    "token_endpoint": f"{base}/token",
                    "jwks_uri": f"{base}/jwks",
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", asked=asked)
    server.shutdown()
    server.server_close()
    thread.join()


def free_port() -> int:
    """Un port local où rien n'écoute."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def csrf_of(page: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def session_payload(client) -> dict:
    """Contenu du cookie de session : un objet JSON signé, dont la signature suit la première partie."""
    return json.loads(base64.b64decode(client.cookies["session"].split(".")[0]))


def user_rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("select id, sub, name from users order by id").fetchall()
    finally:
        conn.close()


def delete_account(db_path, user_id):
    """Efface le compte comme le ferait une base remise à zéro, sans toucher au cookie de session."""
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute("delete from users where id = ?", (user_id,))
    finally:
        conn.close()


class LoggedIn:
    """Un compte connecté : `client` garde le cookie de session, `post` ajoute le jeton anti-CSRF au formulaire."""

    def __init__(self, client, user_id):
        self.client = client
        self.user_id = user_id
        self.csrf = csrf_of(client.get("/").text)

    def post(self, path, data=None, **kwargs):
        return self.client.post(path, data={"csrf": self.csrf, **(data or {})}, **kwargs)


@pytest.fixture
def logged_in(transcriptions_client, db):
    assert sign_in(transcriptions_client).status_code == 303
    return LoggedIn(transcriptions_client, db.user_by_email("a@example.org"))


# --- Connexion ---


def test_page_redirects_to_login_without_a_session(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"


def test_build_oauth_registers_pocketid(settings):
    pocketid = build_oauth(settings).pocketid
    assert pocketid.client_id == "dictee" and pocketid.client_secret == "test-oidc-secret"
    assert pocketid.client_kwargs == {"scope": "openid email profile", "code_challenge_method": "S256"}
    assert create_app(settings).state.oauth.pocketid is not None


@pytest.mark.parametrize("slash", ["", "/"])
def test_login_redirects_to_pocketid_with_pkce(settings, pocketid, slash):
    # Une barre oblique finale dans PUBLIC_URL ou OIDC_ISSUER ne double pas celle de l'adresse construite.
    settings = dataclasses.replace(settings, public_url="https://dictee.test" + slash, oidc_issuer=pocketid.url + slash)
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        r = client.get("/login", follow_redirects=False)
    assert r.status_code == 302
    assert pocketid.asked == ["/.well-known/openid-configuration"]
    target = urlsplit(r.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == f"{pocketid.url}/authorize"
    query = {key: values[0] for key, values in parse_qs(target.query).items()}
    assert query["client_id"] == "dictee" and query["response_type"] == "code"
    assert query["scope"] == "openid email profile"
    assert query["redirect_uri"] == "https://dictee.test/auth/callback"
    assert query["code_challenge_method"] == "S256" and query["code_challenge"] and query["state"]


def test_login_says_so_when_pocketid_is_unreachable(settings, caplog):
    settings = dataclasses.replace(settings, oidc_issuer=f"http://127.0.0.1:{free_port()}")
    with TestClient(create_app(settings), base_url="https://testserver") as client, caplog.at_level(logging.INFO):
        r = client.get("/login", follow_redirects=False)
    assert r.status_code == 502 and r.headers["content-type"].startswith("text/html")
    assert "connexion" in r.text and 'href="/login"' in r.text
    assert "login unavailable class=" in caplog.text


def test_callback_creates_the_account_then_updates_its_email(transcriptions_client, db):
    assert db.user_by_email("a@example.org") is None
    r = sign_in(transcriptions_client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    user_id = db.user_by_email("a@example.org")
    assert user_id is not None

    sign_in(transcriptions_client, {**USERINFO, "email": "A2@Example.org", "name": "A2"})
    assert db.user_by_email("a2@example.org") == user_id
    assert db.user_by_email("a@example.org") is None


def test_callback_starts_a_fresh_session(settings, pocketid):
    settings = dataclasses.replace(settings, oidc_issuer=pocketid.url)
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        client.get("/login", follow_redirects=False)  # laisse l'état de l'échange OAuth dans la session
        assert any(key.startswith("_state_pocketid") for key in session_payload(client))

        sign_in(client)
        first = session_payload(client)
        assert set(first) == {"user_id", "sub", "csrf"}
        assert first["sub"] == USERINFO["sub"]
        sign_in(client)
        assert session_payload(client)["csrf"] != first["csrf"]


def test_callback_refuses_an_email_taken_by_another_account(transcriptions_client, db, db_path, caplog):
    owner = db.upsert_user("sub-owner", "a@example.org", "Owner")
    with caplog.at_level(logging.INFO, logger="dictee"):
        r = sign_in(transcriptions_client, {"sub": "sub-2", "email": "A@Example.org", "name": "Intruder"})
    assert r.status_code == 409 and r.headers["content-type"].startswith("text/html")
    assert "autre compte" in r.text and "administrateur" in r.text
    assert "set-cookie" not in r.headers
    assert transcriptions_client.get("/", follow_redirects=False).status_code == 302
    assert user_rows(db_path) == [(owner, "sub-owner", "Owner")]
    assert "example.org" not in caplog.text.lower() and "Intruder" not in caplog.text


@pytest.mark.parametrize(
    "userinfo",
    [
        None,
        {},
        {"email": "a@example.org"},
        {"sub": "sub-1"},
        {"sub": "sub-1", "email": "   "},
        {"sub": "", "email": "a@example.org"},
        {"sub": 12, "email": "a@example.org"},
    ],
)
def test_callback_refuses_an_answer_without_sub_or_email(transcriptions_client, db_path, userinfo):
    r = sign_in(transcriptions_client, userinfo)
    assert r.status_code == 400 and r.headers["content-type"].startswith("text/html")
    assert "set-cookie" not in r.headers
    assert user_rows(db_path) == []
    transcriptions_client.app.state.oauth.pocketid.userinfo.assert_awaited_once()  # PocketID a été interrogé aussi


@pytest.mark.parametrize("claims", [None, {}, {"sub": "sub-1"}, {"sub": "sub-1", "email": " "}])
def test_callback_asks_the_userinfo_endpoint_when_the_identity_token_is_incomplete(transcriptions_client, db, claims):
    r = sign_in(transcriptions_client, claims, endpoint=USERINFO)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert db.user_by_email("a@example.org") is not None
    userinfo = transcriptions_client.app.state.oauth.pocketid.userinfo
    userinfo.assert_awaited_once()
    assert userinfo.await_args.kwargs["token"]["access_token"] == "token"


def test_callback_keeps_the_identity_token_when_it_is_complete(transcriptions_client, db):
    assert sign_in(transcriptions_client, endpoint={**USERINFO, "email": "other@example.org"}).status_code == 303
    transcriptions_client.app.state.oauth.pocketid.userinfo.assert_not_awaited()
    assert db.user_by_email("a@example.org") is not None and db.user_by_email("other@example.org") is None


def test_callback_refuses_a_userinfo_answer_about_another_subject(transcriptions_client, db_path):
    # Le `sub` de `userinfo` doit être celui du jeton d'identité, sinon ses valeurs ne servent pas (OIDC Core 5.3.2).
    r = sign_in(transcriptions_client, {"sub": "sub-1"}, endpoint={**USERINFO, "sub": "sub-2"})
    assert r.status_code == 400 and 'href="/login"' in r.text
    assert "set-cookie" not in r.headers
    assert user_rows(db_path) == []


def test_callback_refuses_when_the_userinfo_endpoint_fails(transcriptions_client, db_path, caplog):
    with caplog.at_level(logging.INFO, logger="dictee"):
        r = sign_in(transcriptions_client, None, endpoint=ValueError("secret-detail"))
    assert r.status_code == 400 and 'href="/login"' in r.text
    assert "ValueError" in caplog.text and "secret-detail" not in r.text + caplog.text
    assert user_rows(db_path) == []


@pytest.mark.parametrize(
    "error", [OAuthError(description="secret-detail"), httpx.ConnectError("secret-detail"), ValueError("secret-detail")]
)
def test_callback_oauth_error_is_a_400_with_a_login_link(transcriptions_client, caplog, error):
    transcriptions_client.app.state.oauth.pocketid.authorize_access_token = AsyncMock(side_effect=error)
    with caplog.at_level(logging.INFO, logger="dictee"):
        r = transcriptions_client.get("/auth/callback", params={"code": "c", "state": "s"}, follow_redirects=False)
    assert r.status_code == 400 and 'href="/login"' in r.text
    assert "set-cookie" not in r.headers
    assert type(error).__name__ in caplog.text
    assert "secret-detail" not in r.text + caplog.text


def test_callback_refuses_a_state_that_login_never_issued(transcriptions_client, caplog):
    # Authlib réel, sans remplacement : l'état est comparé à celui que `/login` a rangé dans la session.
    with caplog.at_level(logging.INFO, logger="dictee"):
        r = transcriptions_client.get("/auth/callback", params={"code": "c", "state": "forged"}, follow_redirects=False)
    assert r.status_code == 400 and 'href="/login"' in r.text
    assert "MismatchingStateError" in caplog.text


def test_login_logs_the_account_number_only(transcriptions_client, db, caplog):
    with caplog.at_level(logging.INFO, logger="dictee"):
        sign_in(transcriptions_client)
    user_id = db.user_by_email("a@example.org")
    assert f"user={user_id}" in caplog.text
    assert "example.org" not in caplog.text and "sub-1" not in caplog.text


def test_session_cookie_is_secure(transcriptions_client):
    r = sign_in(transcriptions_client)
    cookie = r.headers["set-cookie"].lower()
    attributes = {part.strip().split("=")[0] for part in cookie.split(";")}
    assert cookie.startswith("session=")
    assert {"secure", "httponly"} <= attributes and "samesite=lax" in cookie


def test_logout_clears_the_session(logged_in):
    r = logged_in.post("/logout")
    assert r.status_code == 200 and "déconnecté" in r.text and 'href="/login"' in r.text
    assert logged_in.client.get("/", follow_redirects=False).status_code == 302
    assert logged_in.post("/keys", {"label": "x"}).status_code == 403


def test_repeated_logins_keep_one_pending_state_in_the_session(settings, pocketid):
    # Authlib range un état par tentative de connexion et efface les précédents : le cookie ne grossit pas.
    settings = dataclasses.replace(settings, oidc_issuer=pocketid.url)
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        for _ in range(30):
            assert client.get("/login", follow_redirects=False).status_code == 302
        pending = [key for key in session_payload(client) if key.startswith("_state_pocketid_")]
        assert len(pending) == 1
        assert len(client.cookies["session"]) < 4096  # au-delà, le navigateur refuse le cookie


# --- Session dont le compte n'existe plus ---


def test_a_page_asked_with_a_session_whose_account_is_gone_goes_to_login(logged_in, db_path):
    delete_account(db_path, logged_in.user_id)
    r = logged_in.client.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"
    assert logged_in.client.cookies.get("session") is None  # la session est vidée, pas seulement refusée


@pytest.mark.parametrize(("path", "data"), POSTS)
def test_a_post_with_a_session_whose_account_is_gone_is_a_403_page(logged_in, db_path, path, data):
    delete_account(db_path, logged_in.user_id)
    r = logged_in.post(path, data)
    assert r.status_code == 403 and r.headers["content-type"].startswith("text/html")
    assert 'href="/login"' in r.text
    assert logged_in.client.cookies.get("session") is None
    assert user_rows(db_path) == []  # rien n'a recréé le compte ni rangé quoi que ce soit pour lui


def reset_database_and_sign_up(db, db_path, logged_in):
    """Base remise à zéro, puis un autre compte (autre `sub`) qui reprend le numéro de l'ancien."""
    delete_account(db_path, logged_in.user_id)
    newcomer = db.upsert_user("sub-2", "b@example.org", "B")
    assert newcomer == logged_in.user_id
    return newcomer


def test_a_page_asked_with_a_session_whose_account_number_now_belongs_to_another_sub_goes_to_login(
    logged_in, db, db_path
):
    reset_database_and_sign_up(db, db_path, logged_in)
    r = logged_in.client.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"
    assert logged_in.client.cookies.get("session") is None  # la session est vidée, pas seulement refusée


@pytest.mark.parametrize(("path", "data"), POSTS)
def test_a_post_with_a_session_whose_account_number_now_belongs_to_another_sub_is_a_403_page(
    logged_in, db, db_path, path, data
):
    newcomer = reset_database_and_sign_up(db, db_path, logged_in)
    r = logged_in.post(path, data)
    assert r.status_code == 403 and r.headers["content-type"].startswith("text/html")
    assert 'href="/login"' in r.text
    assert logged_in.client.cookies.get("session") is None
    # Rien n'a été rangé au nom du nouveau compte.
    assert db.list_keys(newcomer) == []
    assert db.get_settings(newcomer) == StoredSettings("", "", "")


def test_the_preview_with_a_session_whose_account_number_now_belongs_to_another_sub_is_a_401_page(
    logged_in, db, db_path, fake_nemo
):
    reset_database_and_sign_up(db, db_path, logged_in)
    r = logged_in.client.get("/voice/preview", params={"voice": "Leo"})
    assert r.status_code == 401 and r.headers["content-type"].startswith("text/html")
    assert logged_in.client.cookies.get("session") is None
    assert fake_nemo.speak_calls == []


def test_a_session_that_names_no_sub_is_refused(settings, logged_in):
    # Cookie signé comme le fait SessionMiddleware, d'avant que la session garde le `sub` : il ne vaut plus rien.
    payload = {"user_id": logged_in.user_id, "csrf": logged_in.csrf}
    data = base64.b64encode(json.dumps(payload).encode("utf-8"))
    cookie = itsdangerous.TimestampSigner(settings.session_secret).sign(data).decode("utf-8")
    logged_in.client.cookies.clear()
    logged_in.client.cookies.set("session", cookie, domain="testserver.local")  # celui où le client range le sien
    r = logged_in.client.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"
    assert logged_in.client.cookies.get("session") is None


def test_the_preview_with_a_session_whose_account_is_gone_is_a_401_page(logged_in, fake_nemo, db_path):
    delete_account(db_path, logged_in.user_id)
    r = logged_in.client.get("/voice/preview", params={"voice": "Leo"})
    assert r.status_code == 401 and r.headers["content-type"].startswith("text/html")
    assert logged_in.client.cookies.get("session") is None
    assert fake_nemo.speak_calls == []


# --- La page ---


def test_page_shows_the_four_blocks_and_the_default_instructions(logged_in):
    page = html.unescape(logged_in.client.get("/").text)
    for title in ("Clés", "Glossaire", "Consignes de reformulation", "Voix"):
        assert title in page
    assert DEFAULT_INSTRUCTIONS in page
    assert "https://dictee.test/v1" in page


def test_page_shows_the_saved_settings_and_the_chosen_voice(logged_in):
    assert re.search(r'value="Leo" checked', logged_in.client.get("/").text)  # la voix par défaut avant tout choix
    logged_in.post(
        "/settings",
        {
            "glossary": "Nemotron (souvent entendu : némo tronc)",
            "instructions": "Corrige la ponctuation.",
            "voice": "Sofia",
        },
    )
    page = html.unescape(logged_in.client.get("/").text)
    assert "Nemotron (souvent entendu : némo tronc)" in page and "Corrige la ponctuation." in page
    assert DEFAULT_INSTRUCTIONS not in page
    assert re.search(r'value="Sofia" checked', page) and not re.search(r'value="Leo" checked', page)
    assert '/voice/preview?voice=Sofia"' in page


def test_page_tells_when_a_change_was_saved(logged_in):
    r = logged_in.post("/settings", {"glossary": "Nemotron"}, follow_redirects=False)
    assert r.headers["location"] == "/?saved=1"
    assert "Modifications enregistrées" in logged_in.client.get("/?saved=1").text
    assert "Modifications enregistrées" not in logged_in.client.get("/").text


def test_page_still_renders_when_nemo_is_down(logged_in, fake_nemo):
    logged_in.client.app.state.voices = None
    fake_nemo.error = NemoUnavailable("down")
    r = logged_in.client.get("/")
    assert r.status_code == 200
    assert "indisponible" in r.text and "<audio" not in r.text
    assert DEFAULT_INSTRUCTIONS in html.unescape(r.text)


def test_page_escapes_what_users_write(logged_in):
    logged_in.post("/settings", {"glossary": "</textarea><script>alert(1)</script>"})
    logged_in.post("/keys", {"label": "<b>x</b>"})
    page = logged_in.client.get("/").text
    assert "<script>" not in page and "<b>x</b>" not in page
    assert "&lt;/textarea&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in page and "&lt;b&gt;x&lt;/b&gt;" in page


def test_html_responses_are_not_cached_and_carry_a_content_security_policy(logged_in):
    key_page = logged_in.post("/keys", {"label": "Android"})  # contient une clé en clair
    pages = [
        logged_in.client.get("/"),
        key_page,
        logged_in.client.post("/keys", data={"label": "x"}),  # refus : pas de jeton
        logged_in.post("/logout"),
    ]
    assert [page.status_code for page in pages] == [200, 200, 403, 200]
    for page in pages:
        assert page.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert page.headers["cache-control"] == "no-store"


# --- Habillage ---

TOKENS = (
    "--background", "--foreground", "--card", "--muted", "--muted-foreground", "--border", "--input",
    "--primary", "--primary-foreground", "--accent", "--accent-foreground", "--success", "--success-surface",
    "--warning", "--warning-surface", "--destructive", "--destructive-surface",
)  # fmt: skip


def test_design_system_palette_is_declared_for_light_and_dark(logged_in):
    for page in (logged_in.client.get("/").text, logged_in.post("/logout").text):
        for token in TOKENS:
            assert page.count(f"{token}:") == 2, token  # une déclaration en clair, une en sombre
        assert "color-scheme: light dark" in page and "prefers-color-scheme: dark" in page


def luminance(color: str) -> float:
    """Luminance relative (WCAG) d'une couleur #rrggbb."""
    channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    red, green, blue = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first: str, second: str) -> float:
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_design_system_muted_text_reaches_aa_contrast_in_both_themes(logged_in):
    page = logged_in.client.get("/").text
    for theme, name in enumerate(("clair", "sombre")):  # la palette claire est déclarée avant la sombre
        colors = {
            token: re.findall(rf"{token}:\s*(#[0-9a-f]{{6}})\b", page)[theme]
            for token in ("--background", "--muted", "--muted-foreground")
        }
        for surface in ("--background", "--muted"):  # le texte discret se pose sur le fond ou sur la pastille
            ratio = contrast(colors["--muted-foreground"], colors[surface])
            assert ratio >= 4.5, f"thème {name}, sur {surface} : contraste {ratio:.2f}"


def test_design_system_code_chip_has_a_visible_outline(logged_in):
    # Le fond --muted ne se distingue pas du fond de page en mode clair : un filet dessine la pastille.
    page = logged_in.client.get("/").text
    assert any("var(--border)" in rule for rule in css_rules(page, "code"))


def test_design_system_pages_load_nothing_from_outside(logged_in):
    pages = [
        logged_in.client.get("/").text,
        logged_in.post("/keys", {"label": "x"}).text,
        logged_in.post("/logout").text,
    ]
    for page in pages:
        assert "<script" not in page
        assert not re.search(r"<(iframe|object|embed)\b", page)
        # L'icône est un <link> et un <img> : ils ne pointent que vers un chemin de dictee, jamais vers un autre site.
        for url in re.findall(r'<(?:link|img)\b[^>]*\b(?:href|src)="([^"]*)"', page):
            assert url.startswith("/") and not url.startswith("//"), url
        assert "@import" not in page and "url(" not in page


# --- Icône ---


def test_the_icon_is_served_without_a_session_and_can_be_cached(client):
    icon = client.get("/icon.svg")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")
    assert icon.text.lstrip().startswith("<svg")
    assert "max-age=" in icon.headers["cache-control"] and "no-store" not in icon.headers["cache-control"]


def test_the_touch_icon_is_a_180_pixel_png_served_without_a_session(client):
    icon = client.get("/apple-touch-icon.png")
    assert icon.status_code == 200
    assert icon.headers["content-type"] == "image/png"
    assert icon.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", icon.content[16:24]) == (180, 180)  # largeur et hauteur de l'en-tête IHDR
    assert "max-age=" in icon.headers["cache-control"] and "no-store" not in icon.headers["cache-control"]


def test_pages_declare_the_icon_and_show_it_before_the_title(logged_in):
    # La page de configuration et la page de message partagent le gabarit : les deux portent la marque.
    for page in (logged_in.client.get("/").text, logged_in.post("/logout").text):
        assert '<link rel="icon" href="/icon.svg" type="image/svg+xml">' in page
        assert '<link rel="apple-touch-icon" href="/apple-touch-icon.png">' in page
        assert re.search(r'<h1[^>]*>\s*<img src="/icon\.svg" alt="" width="\d+" height="\d+">\s*Dictée\s*</h1>', page)


def test_design_system_icon_has_an_outline_in_dark_mode(logged_in):
    # Son carré (#0a0a0a) se confond avec le fond sombre de la page : sans liseré, il ne reste qu'un D blanc isolé.
    page = logged_in.client.get("/").text
    dark_rule = re.search(r"@media \(prefers-color-scheme: dark\) \{\s*\.brand img \{([^}]*)\}", page)
    assert dark_rule is not None and "drop-shadow(" in dark_rule.group(1)


def test_design_system_brand_title_is_uppercase_like_the_app_header(logged_in):
    # Le titre reprend celui de l'en-tête de l'app Android : capitales espacées, en gras, à 16 px.
    rule = " ".join(css_rules(logged_in.client.get("/").text, ".brand"))
    for declaration in ("text-transform: uppercase", "letter-spacing: 0.06em", "font-size: 16px", "font-weight: 700"):
        assert declaration in rule, declaration


BUTTON_CLASSES = {
    "Se déconnecter": "button button-ghost",
    "Créer une clé": "button button-primary",
    "Révoquer": "button button-danger button-small",
    "Enregistrer le glossaire": "button button-primary",
    "Enregistrer les consignes": "button button-primary",
    "Revenir aux consignes par défaut": "button button-secondary",
    "Enregistrer la voix": "button button-primary",
}


def button_classes(page: str, label: str) -> str | None:
    found = re.search(rf'<button[^>]*class="([^"]+)"[^>]*>\s*{re.escape(label)}\s*</button>', page)
    return found.group(1) if found else None


def test_design_system_buttons_carry_the_broker_classes(logged_in):
    page = logged_in.post("/keys", {"label": "Android"}).text  # une clé existe, donc « Révoquer » est affiché
    for label, classes in BUTTON_CLASSES.items():
        assert button_classes(page, label) == classes, label


def test_design_system_message_pages_link_with_a_primary_button(logged_in):
    refused = logged_in.client.post("/keys", data={"label": "x"})  # 403 : pas de jeton
    logged_out = logged_in.post("/logout").text
    for page, label in ((logged_out, "Se connecter"), (refused.text, "Retour à la page de configuration")):
        assert re.search(rf'<a [^>]*class="button button-primary"[^>]*>\s*{label}\s*</a>', page), label


def test_design_system_buttons_are_border_box(logged_in):
    # Sans cela, un lien .button (content-box par défaut) est plus haut qu'un <button> (border-box par défaut).
    rule = re.search(r"(?m)^\s*\.button\s*\{([^}]*)\}", logged_in.client.get("/").text)  # ni .button-* ni .button:*
    assert rule is not None and "box-sizing: border-box" in rule.group(1)


def test_design_system_notices_use_their_variants(logged_in, fake_nemo):
    def has_success_variant(page: str) -> bool:
        return re.search(r'class="[^"]*notice-success', page) is not None  # la feuille de style le nomme toujours

    saved = logged_in.client.get("/?saved=1").text
    assert re.search(r'class="notice notice-success"[^>]*>\s*Modifications enregistrées', saved)
    created = logged_in.post("/keys", {"label": "Android"}).text
    assert 'class="notice" role="alert"' in created and not has_success_variant(created)
    logged_in.client.app.state.voices = None
    fake_nemo.error = NemoUnavailable("down")
    down = logged_in.client.get("/").text
    assert re.search(r'class="notice"[^>]*>\s*La liste des voix', down) and not has_success_variant(down)


def test_design_system_key_table_scrolls_on_its_own_and_marks_revoked_keys(logged_in):
    page = logged_in.post("/keys", {"label": "x" * 100}).text  # 100 caractères : la limite des libellés
    assert re.search(r'<div class="table-scroll">\s*<table class="data-table">', page)
    key_id = re.search(r'action="/keys/(\d+)/revoke"', page).group(1)
    page = logged_in.post(f"/keys/{key_id}/revoke").text
    assert re.search(r'class="status-badge status-retired"[^>]*>\s*révoquée le', page)
    assert "Révoquer" not in page


def css_rules(page: str, selector: str) -> list[str]:
    """Corps des règles de la feuille de style dont la liste de sélecteurs contient exactement ce sélecteur."""
    style = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
    style = re.sub(r"/\*.*?\*/", "", style, flags=re.S)  # un commentaire avant une règle ne s'ajoute pas à son sélecteur
    return [
        body
        for selectors, body in re.findall(r"([^{}]+)\{([^}]*)\}", style)
        if selector in [part.strip() for part in selectors.split(",")]
    ]


def test_design_system_key_table_rows_stay_on_one_line(logged_in):
    page = logged_in.client.get("/").text
    assert any("white-space: nowrap" in rule for rule in css_rules(page, ".data-table td"))
    assert any("overflow-x: auto" in rule for rule in css_rules(page, ".table-scroll"))
    # Le libellé fait exception : il se replie et casse une longue suite sans espace, dans une largeur bornée,
    # et garde un plancher pour ne pas s'écraser sur la largeur de son en-tête.
    label = " ".join(css_rules(page, ".data-table td:first-child"))
    assert "white-space: normal" in label and "overflow-wrap: anywhere" in label
    assert "max-width" in label and "min-width" in label
    # Une clé du tableau reste d'un bloc ; celle de l'encart, hors tableau, garde le repli n'importe où.
    assert any("overflow-wrap: normal" in rule for rule in css_rules(page, ".data-table code"))
    assert any("overflow-wrap: anywhere" in rule for rule in css_rules(page, "code"))


# --- Garde anti-CSRF ---


@pytest.mark.parametrize(("path", "data"), POSTS)
def test_forms_require_the_csrf_token(logged_in, db, path, data):
    assert logged_in.client.post(path, data=data).status_code == 403
    assert logged_in.client.post(path, data={**data, "csrf": "wrong"}).status_code == 403
    assert db.list_keys(logged_in.user_id) == []
    assert db.get_settings(logged_in.user_id) == StoredSettings("", "", "")
    assert logged_in.client.get("/", follow_redirects=False).status_code == 200  # la session est restée ouverte


@pytest.mark.parametrize(("path", "data"), POSTS)
def test_forms_require_a_session(transcriptions_client, db, path, data):
    r = transcriptions_client.post(path, data={**data, "csrf": "anything"})
    assert r.status_code == 403 and r.headers["content-type"].startswith("text/html")


def test_every_post_route_of_the_page_is_covered_by_the_csrf_tests(client):
    # Une route WebSocket n'a pas de méthode : seules les routes HTTP de la page comptent.
    page_routes = [
        route for route in client.app.routes if isinstance(route, Route) and not route.path.startswith("/v1/")
    ]
    assert {route.path for route in page_routes if "POST" in route.methods} == POST_ROUTES


# --- Clés ---


def test_new_key_is_shown_once(logged_in):
    page = logged_in.post("/keys", {"label": "Android"}).text
    key = re.search(r"dk_[\w-]{40,}", page).group(0)
    assert "Android" in page
    assert key not in logged_in.client.get("/").text
    assert key not in json.dumps(session_payload(logged_in.client))
    assert logged_in.client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).status_code == 200
    assert key[:10] in logged_in.client.get("/").text  # la page garde de quoi la reconnaître


@pytest.mark.parametrize(
    ("label", "status", "stored"),
    [
        ("", 400, None),
        ("   ", 400, None),
        ("x" * 101, 400, None),
        (" Android ", 200, "Android"),
        ("x" * 100, 200, "x" * 100),
    ],
)
def test_key_label_is_bounded(logged_in, db, label, status, stored):
    assert logged_in.post("/keys", {"label": label}).status_code == status
    assert [key.label for key in db.list_keys(logged_in.user_id)] == ([] if stored is None else [stored])


def test_revoked_key_stops_working(logged_in, db):
    key = re.search(r"dk_[\w-]{40,}", logged_in.post("/keys", {"label": "Android"}).text).group(0)
    spare = db.create_key(logged_in.user_id, "spare")  # une clé qui reste, pour voir que seule la bonne est révoquée
    key_id = db.list_keys(logged_in.user_id)[0].id

    def models_status(token):
        return logged_in.client.get("/v1/models", headers={"Authorization": f"Bearer {token}"}).status_code

    assert models_status(key) == 200
    r = logged_in.post(f"/keys/{key_id}/revoke", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?saved=1"
    assert models_status(key) == 401 and models_status(spare) == 200
    page = logged_in.client.get("/").text
    assert "révoquée" in page and f"/keys/{key_id}/revoke" not in page


def test_cannot_revoke_someone_elses_key(logged_in, db):
    other = db.upsert_user("sub-2", "b@example.org", "B")
    key = db.create_key(other, "theirs")
    key_id = db.list_keys(other)[0].id
    assert logged_in.post(f"/keys/{key_id}/revoke").status_code == 404
    assert db.key_owner(key) == other


@pytest.mark.parametrize("key_id", ["999", str(2**70)])
def test_revoking_an_unknown_key_is_a_404_page(logged_in, key_id):
    r = logged_in.post(f"/keys/{key_id}/revoke")
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")


# --- Réglages ---


def test_settings_are_saved_and_default_instructions_stay_empty(logged_in, db):
    data = {"glossary": "Nemotron", "instructions": f"  {DEFAULT_INSTRUCTIONS}  \r\n", "voice": "Leo"}
    r = logged_in.post("/settings", data, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?saved=1"
    assert db.get_settings(logged_in.user_id) == StoredSettings("Nemotron", "", "Leo")


def test_settings_normalize_line_endings_and_edges(logged_in, db):
    glossary = "  Nemotron\r\nSortformer (souvent entendu : sort former)\r\n\r\n"
    logged_in.post("/settings", {"glossary": glossary, "instructions": "\r\nSois bref.\r\nSans fioriture.  "})
    stored = db.get_settings(logged_in.user_id)
    assert stored.glossary == "Nemotron\nSortformer (souvent entendu : sort former)"
    assert stored.instructions == "Sois bref.\nSans fioriture."


def test_settings_accept_any_subset_of_the_fields(logged_in, db):
    logged_in.post("/settings", {"glossary": "Nemotron", "instructions": "Sois bref.", "voice": "Leo"})
    logged_in.post("/settings", {"voice": "aria"})  # sans distinction de casse, rangée sous le nom de NeMo
    assert db.get_settings(logged_in.user_id) == StoredSettings("Nemotron", "Sois bref.", "Aria")
    logged_in.post("/settings", {"glossary": ""})
    assert db.get_settings(logged_in.user_id) == StoredSettings("", "Sois bref.", "Aria")


@pytest.mark.parametrize(
    "data",
    [
        {"glossary": "x" * 20001},
        {"glossary": "\n".join(["Nemotron"] * 501)},
        {"instructions": "x" * 20001},
        {"glossary": "Nemotron", "voice": "alloy"},
        {"glossary": "Nemotron", "voice": ""},
    ],
)
def test_settings_out_of_bounds_are_refused_and_nothing_is_saved(logged_in, db, data):
    r = logged_in.post("/settings", data)
    assert r.status_code == 400 and r.headers["content-type"].startswith("text/html")
    assert db.get_settings(logged_in.user_id) == StoredSettings("", "", "")


@pytest.mark.parametrize(
    "data", [{"glossary": "x" * 20000}, {"glossary": "\n".join(["Nemotron"] * 500)}, {"instructions": "x" * 20000}]
)
def test_settings_at_the_bounds_are_accepted(logged_in, data):
    assert logged_in.post("/settings", data, follow_redirects=False).status_code == 303


def test_a_multipart_form_without_boundary_is_a_400_page(logged_in, db):
    r = logged_in.client.post("/settings", content=b"x", headers={"Content-Type": "multipart/form-data"})
    assert r.status_code == 400 and r.headers["content-type"].startswith("text/html")
    assert db.get_settings(logged_in.user_id) == StoredSettings("", "", "")


def test_a_form_that_declares_more_than_the_limit_is_refused_before_it_is_read(logged_in, db, monkeypatch):
    def refuse_to_read(self, *args, **kwargs):
        raise AssertionError("the body must not be read")

    monkeypatch.setattr(Request, "form", refuse_to_read)
    fields = {"csrf": logged_in.csrf, "glossary": "x" * MAX_FORM_BYTES}
    r = logged_in.client.post("/settings", data=fields)
    assert r.status_code == 413 and r.headers["content-type"].startswith("text/html")
    assert db.get_settings(logged_in.user_id) == StoredSettings("", "", "")


def test_a_form_of_exactly_the_limit_is_still_read(logged_in, db):
    prefix = f"csrf={logged_in.csrf}&padding="
    body = prefix + "x" * (MAX_FORM_BYTES - len(prefix))
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    r = logged_in.client.post("/settings", content=body, headers=headers)
    assert r.request.headers["content-length"] == str(MAX_FORM_BYTES)
    assert r.status_code == 400 and "Aucune modification" in r.text  # lu jusqu'au bout, sans champ connu : pas un 413


def test_a_chunked_form_is_cut_when_it_passes_the_limit(logged_in, db):
    def body():
        yield f"csrf={logged_in.csrf}&glossary=".encode()
        for _ in range(MAX_FORM_BYTES // 1024 + 1):
            yield b"x" * 1024

    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    r = logged_in.client.post("/settings", content=body(), headers=headers)
    assert "content-length" not in r.request.headers  # aucune taille annoncée : seule la lecture peut la borner
    assert r.status_code == 413 and r.headers["content-type"].startswith("text/html")
    assert db.get_settings(logged_in.user_id) == StoredSettings("", "", "")


def test_a_form_without_session_is_not_read_even_when_it_is_small(transcriptions_client, monkeypatch):
    monkeypatch.setattr(Request, "form", lambda self, *args, **kwargs: pytest.fail("the body must not be read"))
    assert transcriptions_client.post("/settings", data={"csrf": "x"}).status_code == 403


def test_a_settings_form_without_any_known_field_is_a_400_page(logged_in, db):
    for data in ({}, {"unknown": "x"}):
        r = logged_in.post("/settings", data)
        assert r.status_code == 400 and r.headers["content-type"].startswith("text/html")
        assert "enregistrées" not in r.text
    assert db.get_settings(logged_in.user_id) == StoredSettings("", "", "")


def test_unknown_voice_is_refused(logged_in, db):
    assert logged_in.post("/settings", {"voice": "alloy"}).status_code == 400
    assert db.get_settings(logged_in.user_id).voice == ""


def test_settings_when_nemo_is_down_save_nothing_that_needs_it(logged_in, db, fake_nemo):
    logged_in.client.app.state.voices = None
    fake_nemo.error = NemoUnavailable("down")
    r = logged_in.post("/settings", {"glossary": "Nemotron", "voice": "Leo"})
    assert r.status_code == 502 and "voix" in r.text
    assert db.get_settings(logged_in.user_id) == StoredSettings("", "", "")
    # Sans voix dans le formulaire, NeMo n'est pas consulté.
    assert logged_in.post("/settings", {"glossary": "Nemotron"}, follow_redirects=False).status_code == 303
    assert db.get_settings(logged_in.user_id).glossary == "Nemotron"


def test_reset_instructions(logged_in, db):
    logged_in.post("/settings", {"glossary": "Nemotron", "instructions": "Sois bref.", "voice": "Leo"})
    r = logged_in.post("/settings/reset-instructions", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?saved=1"
    assert db.get_settings(logged_in.user_id) == StoredSettings("Nemotron", "", "Leo")
    assert DEFAULT_INSTRUCTIONS in html.unescape(logged_in.client.get("/").text)


# --- Aperçu d'une voix ---


def test_voice_preview_returns_mp3(logged_in, fake_nemo, caplog):
    fake_nemo.pcm = bytes(2 * TTS_SAMPLE_RATE)
    with caplog.at_level(logging.INFO, logger="dictee"):
        r = logged_in.client.get("/voice/preview", params={"voice": "Leo"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    assert r.content.startswith(MP3_MAGIC)
    assert fake_nemo.speak_calls[-1] == {"text": PREVIEW_TEXT, "voice": "Leo", "language": "fr-FR"}
    assert PREVIEW_TEXT not in caplog.text


def test_voice_preview_accepts_any_casing_and_speaks_with_the_nemo_name(logged_in, fake_nemo):
    fake_nemo.pcm = bytes(2 * TTS_SAMPLE_RATE)
    assert logged_in.client.get("/voice/preview", params={"voice": "aRIA"}).status_code == 200
    assert fake_nemo.speak_calls[-1]["voice"] == "Aria"


def test_voice_preview_is_synthesized_once_per_voice(logged_in, fake_nemo):
    fake_nemo.pcm = bytes(2 * TTS_SAMPLE_RATE)
    first = logged_in.client.get("/voice/preview", params={"voice": "Leo"})
    second = logged_in.client.get("/voice/preview", params={"voice": "leo"})
    assert first.content and first.content == second.content
    assert len(fake_nemo.speak_calls) == 1
    logged_in.client.get("/voice/preview", params={"voice": "Aria"})
    assert [call["voice"] for call in fake_nemo.speak_calls] == ["Leo", "Aria"]


def test_voice_preview_does_not_keep_a_failure(logged_in, fake_nemo):
    fake_nemo.pcm = bytes(2 * TTS_SAMPLE_RATE)
    fake_nemo.error = NemoUnavailable("down")
    assert logged_in.client.get("/voice/preview", params={"voice": "Leo"}).status_code == 502
    fake_nemo.error = None
    assert logged_in.client.get("/voice/preview", params={"voice": "Leo"}).status_code == 200
    fake_nemo.error = NemoUnavailable("down")  # l'aperçu réussi est gardé : NeMo n'est plus sollicité
    assert logged_in.client.get("/voice/preview", params={"voice": "Leo"}).status_code == 200
    assert len(fake_nemo.speak_calls) == 2


@pytest.mark.anyio
async def test_preview_cache_synthesizes_once_when_requests_overlap():
    cache = PreviewCache()
    calls = []

    async def synthesize(voice):
        calls.append(voice)
        await anyio.sleep(0.01)
        return b"mp3 of " + voice.encode()

    answers = await asyncio.gather(cache.get("Leo", synthesize), cache.get("Leo", synthesize))
    assert answers == [b"mp3 of Leo", b"mp3 of Leo"]
    assert calls == ["Leo"]


@pytest.fixture
def preview_mp3(logged_in, fake_nemo):
    """L'aperçu de Leo en entier, et de quoi le redemander avec un en-tête `Range`."""
    fake_nemo.pcm = bytes(2 * TTS_SAMPLE_RATE)

    def get(range_header=None):
        headers = {} if range_header is None else {"Range": range_header}
        return logged_in.client.get("/voice/preview", params={"voice": "Leo"}, headers=headers)

    whole = get()
    assert whole.status_code == 200 and len(whole.content) > 1000
    return SimpleNamespace(get=get, content=whole.content, size=len(whole.content), whole=whole)


def test_voice_preview_says_it_serves_byte_ranges(preview_mp3):
    assert preview_mp3.whole.headers["accept-ranges"] == "bytes"
    assert preview_mp3.whole.headers["content-length"] == str(preview_mp3.size)


@pytest.mark.parametrize(
    ("header", "span"),
    [
        ("bytes=0-1", lambda size: (0, 1)),
        ("bytes=0-0", lambda size: (0, 0)),
        ("bytes=2-5", lambda size: (2, 5)),
        ("bytes=3-", lambda size: (3, size - 1)),
        ("bytes=-4", lambda size: (size - 4, size - 1)),
        ("bytes=0-99999999", lambda size: (0, size - 1)),
        ("Bytes = 1 - 2", lambda size: (1, 2)),
    ],
)
def test_voice_preview_serves_a_single_byte_range_as_206(preview_mp3, header, span):
    start, end = span(preview_mp3.size)
    r = preview_mp3.get(header)
    assert r.status_code == 206 and r.headers["content-type"] == "audio/mpeg"
    assert r.content == preview_mp3.content[start : end + 1]
    assert r.headers["content-range"] == f"bytes {start}-{end}/{preview_mp3.size}"
    assert r.headers["content-length"] == str(end - start + 1) and r.headers["accept-ranges"] == "bytes"


@pytest.mark.parametrize("header", ["bytes=99999999-", "bytes=-0"])
def test_voice_preview_refuses_a_range_past_the_end_with_416(preview_mp3, header):
    r = preview_mp3.get(header)
    assert r.status_code == 416 and r.headers["content-range"] == f"bytes */{preview_mp3.size}"
    assert r.headers["accept-ranges"] == "bytes"


@pytest.mark.parametrize(
    "header",
    ["bytes=", "bytes=-", "bytes=abc", "items=0-1", "0-1", "bytes=5-2", "bytes=0-1,3-4", "bytes=" + "9" * 5000 + "-"],
)
def test_voice_preview_sends_the_whole_file_for_a_range_it_does_not_follow(preview_mp3, header):
    r = preview_mp3.get(header)
    assert r.status_code == 200 and r.content == preview_mp3.content and "content-range" not in r.headers


@pytest.mark.parametrize(
    "header",
    [
        pytest.param("bytes=" + " " * 16_000, id="spaces-after-the-equal-sign"),
        pytest.param("bytes=1" + " " * 16_000 + "x", id="spaces-after-the-first-number"),
        pytest.param("bytes=-" + " " * 16_000 + "x", id="spaces-after-the-dash"),
        pytest.param(" " * 16_000 + "x", id="spaces-before-everything"),
    ],
)
def test_a_range_header_made_of_spaces_is_read_in_linear_time(header):
    # Une expression régulière qui laisse deux `\s*` voisins se disputer les espaces y passe un temps quadratique.
    started = time.perf_counter()
    response = _mp3_response(b"mp3", header)
    assert time.perf_counter() - started < 0.05
    assert response.status_code == 200 and response.body == b"mp3"


def test_voice_preview_requires_a_session(client):
    r = client.get("/voice/preview", params={"voice": "Leo"})
    assert r.status_code == 401 and r.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("params", [{"voice": "alloy"}, {"voice": ""}, {}])
def test_voice_preview_refuses_an_unknown_voice_without_calling_nemo(logged_in, fake_nemo, params):
    assert logged_in.client.get("/voice/preview", params=params).status_code == 400
    assert fake_nemo.speak_calls == []


@pytest.mark.parametrize(("error", "status"), [(NemoOverloaded("busy"), 429), (NemoUnavailable("down"), 502)])
def test_voice_preview_reports_a_nemo_failure(logged_in, fake_nemo, error, status):
    fake_nemo.error = error
    assert logged_in.client.get("/voice/preview", params={"voice": "Leo"}).status_code == status
    logged_in.client.app.state.voices = None  # la lecture des voix échoue de même
    assert logged_in.client.get("/voice/preview", params={"voice": "Leo"}).status_code == status


# --- Garde ---


def test_v1_routes_do_not_depend_on_the_session(transcriptions_client, service_headers):
    r = transcriptions_client.get("/v1/models", headers=service_headers)
    assert r.status_code == 200 and "set-cookie" not in r.headers


def test_a_session_cookie_alone_does_not_open_v1(logged_in):
    assert "session" in logged_in.client.cookies
    r = logged_in.client.get("/v1/models")  # sans en-tête Authorization
    assert r.status_code == 401
