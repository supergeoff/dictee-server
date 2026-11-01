"""Page de configuration de dictee et connexion par PocketID.

Chacun se connecte avec PocketID (OIDC, code d'autorisation avec PKCE), puis règle sur une seule page ses clés d'API,
son glossaire, ses consignes de reformulation et sa voix. La page n'a pas de JavaScript : chaque action est un
formulaire, et l'écoute d'une voix passe par un élément `<audio>`.

La session est le cookie signé de `SessionMiddleware` ; elle ne garde que `user_id`, le `sub` PocketID du compte (qui
distingue deux comptes ayant porté le même numéro après une remise à zéro de la base) et `csrf`. Chaque `POST` exige une
session et le jeton anti-CSRF, présent dans chaque formulaire. Une clé nouvelle n'existe en clair que dans la réponse
au `POST /keys` : elle n'est ni rangée dans la session, ni journalisée. Les réponses HTML ne sont jamais mises en
cache et portent une politique de sécurité qui n'autorise ni script, ni image, ni ressource d'un autre site.

Le journal ne garde d'une connexion que le numéro du compte, jamais l'email, le nom, un jeton ni un réglage.
"""

import asyncio
import contextlib
import logging
import re
import secrets
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping

from authlib.integrations.starlette_client import OAuth
from jinja2 import Environment, PackageLoader, StrictUndefined
from starlette.datastructures import State
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.templating import Jinja2Templates
from starlette.types import Message

from dictee.audio import OUTPUT_FORMATS, encode_stream
from dictee.auth import Caller
from dictee.errors import NEMO_ERRORS, nemo_failure
from dictee.nemo import TTS_SAMPLE_RATE
from dictee.profile import DEFAULT_INSTRUCTIONS, load_profile
from dictee.settings import Settings
from dictee.speech import LANGUAGE, list_voices, resolve_voice

logger = logging.getLogger("dictee")

PREVIEW_TEXT = "Bonjour, voici la voix qui lira vos textes."
MAX_GLOSSARY_CHARS = 20_000
MAX_GLOSSARY_LINES = 500
MAX_INSTRUCTIONS_CHARS = 20_000
MAX_LABEL_CHARS = 100
# Un formulaire de la page tient dans quelques dizaines de Ko : au-delà, le corps n'est pas lu.
MAX_FORM_BYTES = 256 * 1024
# Plus grand entier que SQLite range : un identifiant de clé au-delà ne peut désigner aucune clé.
MAX_KEY_ID = 2**63 - 1

# Les pages portent une clé en clair après `POST /keys` : le navigateur ne doit rien en garder.
PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; media-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "Cache-Control": "no-store",
}
HOME_LINK = ("/", "Retour à la page de configuration")
LOGIN_LINK = ("/login", "Se connecter")
LOGIN_FAILED = "La connexion avec PocketID a échoué."

# Une seule plage d'octets : `bytes=a-b`, `bytes=a-` ou `bytes=-n`. Les nombres sont bornés, car `int` refuse les très
# longues suites de chiffres. Les espaces suivent le nombre qu'ils accompagnent : deux `\s*` voisins, séparés d'un
# groupe facultatif, se disputeraient une longue suite d'espaces en temps quadratique.
_BYTE_RANGE = re.compile(r"\s*bytes\s*=\s*(?:(\d{1,15})\s*)?-\s*(?:(\d{1,15})\s*)?", re.IGNORECASE)

_templates = Jinja2Templates(
    env=Environment(
        loader=PackageLoader("dictee", "templates"), autoescape=True, undefined=StrictUndefined, auto_reload=False
    )
)


class WebError(Exception):
    """Refus d'une route de la page, rendu comme une courte page HTML en français avec le statut donné."""

    def __init__(self, status: int, message: str, link: tuple[str, str] = HOME_LINK) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.link = link


class PreviewCache:
    """Mp3 de l'aperçu de chaque voix, gardés après la première synthèse réussie.

    Un échec n'est pas gardé. Une voix n'est synthétisée qu'une fois à la fois : les requêtes qui arrivent ensemble
    (Safari en envoie plusieurs par lecture) attendent la première au lieu de relancer chacune une synthèse.
    """

    def __init__(self) -> None:
        self._mp3: dict[str, bytes] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, voice: str, synthesize: Callable[[str], Awaitable[bytes]]) -> bytes:
        """Mp3 de `voice` ; `synthesize(voice)` le fabrique si la voix n'est pas encore gardée."""
        if voice not in self._mp3:
            async with self._locks.setdefault(voice, asyncio.Lock()):
                if voice not in self._mp3:
                    self._mp3[voice] = await synthesize(voice)
        return self._mp3[voice]


def _page(request: Request, name: str, context: dict, status: int = 200) -> Response:
    return _templates.TemplateResponse(request, name, context, status_code=status, headers=PAGE_HEADERS)


def _message(request: Request, status: int, message: str, link: tuple[str, str]) -> Response:
    context = {"message": message, "link_url": link[0], "link_text": link[1]}
    return _page(request, "message.html", context, status)


async def handle_web_error(request: Request, exc: WebError) -> Response:
    return _message(request, exc.status, exc.message, exc.link)


def build_oauth(settings: Settings) -> OAuth:
    """Registre OAuth de l'app, avec le client `pocketid` ; les adresses de PocketID viennent de ses métadonnées."""
    oauth = OAuth()
    oauth.register(
        name="pocketid",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        server_metadata_url=f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
    )
    return oauth


def _session_user(request: Request) -> int | None:
    """Compte de la session, ou `None` sans session.

    La session porte le numéro du compte et le `sub` PocketID avec lequel il s'est connecté. Si le compte n'existe plus
    (base remise à zéro, compte supprimé), ou si le numéro appartient désormais à un autre `sub` (un nouveau compte a
    repris le numéro), ou si la session ne dit pas son `sub` (cookie d'avant cette règle), elle est vidée et traitée
    comme absente : aucune route ne travaille pour un compte fantôme ni pour celui d'un autre.
    """
    user_id = request.session.get("user_id")
    if type(user_id) is not int:
        return None
    sub = request.session.get("sub")
    if type(sub) is not str or request.app.state.db.user_sub(user_id) != sub:
        request.session.clear()
        return None
    return user_id


def _clean(text: str) -> str:
    """Texte d'un formulaire : sauts de ligne `\\n` et sans espace de bord, comme on le compare et le range."""
    return text.replace("\r\n", "\n").strip()


def _too_big() -> WebError:
    return WebError(413, "Le formulaire est trop volumineux.")


def _bounded(request: Request) -> Request:
    """Requête de même corps que `request`, dont la lecture lève 413 dès que `MAX_FORM_BYTES` octets sont dépassés.

    `Content-Length` ne dit rien d'un corps envoyé par morceaux : seule la lecture sait combien il en vient.
    """
    received = 0

    async def receive() -> Message:
        nonlocal received
        message = await request.receive()
        received += len(message.get("body", b""))
        if received > MAX_FORM_BYTES:
            raise _too_big()
        return message

    return Request(request.scope, receive)


async def _read_post(request: Request) -> tuple[int, dict[str, str]]:
    """Compte de la session et champs textuels d'un `POST` ; 403 sans session ou sans jeton anti-CSRF valide.

    La session se vérifie avant de lire le corps : sans session, le corps n'est pas lu. Un corps de plus de
    `MAX_FORM_BYTES` octets est refusé (413), sur la foi de `Content-Length` avant d'en lire un octet, ou pendant la
    lecture s'il n'annonce pas sa taille.
    """
    user_id = _session_user(request)
    expected = request.session.get("csrf")
    if user_id is None or not isinstance(expected, str):
        raise WebError(403, "Votre session a expiré. Connectez-vous de nouveau.", LOGIN_LINK)
    try:
        declared = int(request.headers.get("content-length", "0"))
    except ValueError:
        declared = 0  # taille illisible : la lecture bornée y pourvoit
    if declared > MAX_FORM_BYTES:
        raise _too_big()
    try:
        async with _bounded(request).form() as form:
            fields = {name: value for name, value in form.items() if isinstance(value, str)}
    except HTTPException:
        raise WebError(400, "Le formulaire n'a pas pu être lu.") from None
    if not secrets.compare_digest(fields.get("csrf", "").encode(), expected.encode()):
        raise WebError(403, "Le formulaire a expiré. Rechargez la page et recommencez.")
    return user_id, fields


def _public_url(settings: Settings) -> str:
    return settings.public_url.rstrip("/")


async def _known_voice(state: State, requested: str) -> str:
    """Voix de NeMo que `requested` désigne, sans tenir compte de la casse, écrite comme NeMo l'écrit.

    Lève `WebError` 400 si NeMo ne la propose pas ; un échec de NeMo à la lecture des voix remonte tel quel.
    """
    # Sans voix de repli, `resolve_voice` rend une chaîne vide pour un nom que NeMo ne propose pas.
    voice = resolve_voice(requested.strip(), "", await list_voices(state))
    if not voice:
        raise WebError(400, "Cette voix n'existe pas.")
    return voice


def _checked_glossary(text: str) -> str:
    glossary = _clean(text)
    if len(glossary) > MAX_GLOSSARY_CHARS or len(glossary.splitlines()) > MAX_GLOSSARY_LINES:
        message = f"Le glossaire est limité à {MAX_GLOSSARY_CHARS} caractères et à {MAX_GLOSSARY_LINES} lignes."
        raise WebError(400, message)
    return glossary


def _checked_instructions(text: str) -> str:
    """Consignes à ranger ; celles qui égalent les consignes par défaut sont rangées vides."""
    instructions = _clean(text)
    if len(instructions) > MAX_INSTRUCTIONS_CHARS:
        raise WebError(400, f"Les consignes sont limitées à {MAX_INSTRUCTIONS_CHARS} caractères.")
    return "" if instructions == DEFAULT_INSTRUCTIONS else instructions


async def _render_index(request: Request, user_id: int, *, new_key: str | None = None) -> Response:
    state = request.app.state
    profile = load_profile(state.db, state.settings, Caller(user_id))
    try:
        voices = await list_voices(state)
    except NEMO_ERRORS as error:
        # La page reste utile sans la liste des voix : clés, glossaire et consignes ne dépendent pas de NeMo.
        logger.warning("voices unavailable class=%s", type(error).__name__)
        voices = ()
    context = {
        "csrf": request.session["csrf"],
        "api_url": f"{_public_url(state.settings)}/v1",
        "keys": state.db.list_keys(user_id),
        "new_key": new_key,
        "glossary": state.db.get_settings(user_id).glossary,
        "instructions": profile.instructions,
        "voices": voices,
        "current_voice": resolve_voice(profile.voice, "", voices),
        "saved": request.query_params.get("saved") == "1",
    }
    return _page(request, "index.html", context)


async def index(request: Request) -> Response:
    user_id = _session_user(request)
    if user_id is None:
        return RedirectResponse("/login", status_code=302)
    return await _render_index(request, user_id)


async def login(request: Request) -> Response:
    state = request.app.state
    try:
        return await state.oauth.pocketid.authorize_redirect(request, f"{_public_url(state.settings)}/auth/callback")
    except Exception as error:
        # Lire les métadonnées de PocketID peut échouer par le réseau, par un statut ou par un corps illisible, et
        # les classes d'erreur du réseau dépendent du client HTTP qu'Authlib charge (httpx2 ou, à défaut, httpx).
        logger.warning("login unavailable class=%s", type(error).__name__)
        raise WebError(502, "Le service de connexion ne répond pas. Réessayez dans un instant.", LOGIN_LINK) from None


def _login_failed(error: Exception) -> WebError:
    """Refus d'une connexion dont l'échange avec PocketID a échoué ; seule la classe de l'exception est journalisée.

    Authlib, joserfc et httpx lèvent chacun leurs classes (état, jeton, signature, réseau) : toutes refusent la
    connexion, et le message de l'exception peut citer le code ou le jeton.
    """
    logger.warning("login refused class=%s", type(error).__name__)
    return WebError(400, LOGIN_FAILED, LOGIN_LINK)


def _identity(claims: object) -> tuple[str, str, str] | None:
    """`(sub, email, nom)` d'un `userinfo`, ou `None` s'il manque le `sub` ou l'email ; l'email tient lieu de nom."""
    if not isinstance(claims, Mapping):
        return None
    sub, email, name = claims.get("sub"), claims.get("email"), claims.get("name")
    if not (isinstance(sub, str) and sub.strip() and isinstance(email, str) and email.strip()):
        return None
    return sub, email, name if isinstance(name, str) and name.strip() else email


async def callback(request: Request) -> Response:
    """Retour de PocketID : ouvre une session pour le compte, créé au premier passage.

    Le jeton d'identité dit qui se connecte ; s'il ne porte pas le `sub` ou l'email, le point `userinfo` de PocketID
    les donne. Un email déjà pris par un autre compte refuse la connexion : rattacher le compte existant au nouvel
    identifiant laisserait quiconque met l'email d'un autre dans PocketID prendre sa place.
    """
    state = request.app.state
    pocketid = state.oauth.pocketid
    try:
        token = await pocketid.authorize_access_token(request)
    except Exception as error:
        raise _login_failed(error) from None
    claims = token.get("userinfo")
    identity = _identity(claims)
    if identity is None:
        try:
            identity = _identity(await pocketid.userinfo(token=token))
        except Exception as error:
            raise _login_failed(error) from None
        # Les valeurs de `userinfo` ne valent que pour le `sub` du jeton d'identité (OpenID Connect Core, 5.3.2).
        known = claims.get("sub") if isinstance(claims, Mapping) else None
        if identity is not None and isinstance(known, str) and known != identity[0]:
            logger.warning("login refused reason=userinfo_subject_mismatch")
            raise WebError(400, LOGIN_FAILED, LOGIN_LINK)
    if identity is None:
        raise WebError(400, "PocketID n'a pas transmis l'identifiant ou l'email du compte.", LOGIN_LINK)
    sub, email, name = identity
    try:
        user_id = state.db.upsert_user(sub, email, name)
    except sqlite3.IntegrityError:
        message = "Un autre compte utilise déjà cet email. Adressez-vous à l'administrateur."
        raise WebError(409, message, LOGIN_LINK) from None
    # Session vidée avant d'être remplie : un cookie déposé avant la connexion ne devient pas celui du compte.
    request.session.clear()
    request.session["user_id"] = user_id
    request.session["sub"] = sub
    request.session["csrf"] = secrets.token_urlsafe(32)
    logger.info("login user=%d", user_id)
    return RedirectResponse("/", status_code=303)


async def logout(request: Request) -> Response:
    """Ferme la session de dictee ; celle de PocketID reste ouverte, d'où la page plutôt qu'un retour à la connexion."""
    await _read_post(request)
    request.session.clear()
    return _message(request, 200, "Vous êtes déconnecté de dictee.", LOGIN_LINK)


async def create_key(request: Request) -> Response:
    """Crée une clé et rend la page qui la montre : c'est la seule fois qu'elle apparaît en clair."""
    user_id, fields = await _read_post(request)
    label = _clean(fields.get("label", ""))
    if not 1 <= len(label) <= MAX_LABEL_CHARS:
        raise WebError(400, f"Le libellé de la clé compte de 1 à {MAX_LABEL_CHARS} caractères.")
    key = request.app.state.db.create_key(user_id, label)
    return await _render_index(request, user_id, new_key=key)


async def revoke_key(request: Request) -> Response:
    user_id, _ = await _read_post(request)
    key_id = request.path_params["key_id"]
    if key_id > MAX_KEY_ID or not request.app.state.db.revoke_key(user_id, key_id):
        raise WebError(404, "Cette clé n'existe pas ou est déjà révoquée.")
    return RedirectResponse("/?saved=1", status_code=303)


async def update_settings(request: Request) -> Response:
    """Enregistre les champs reçus, parmi `glossary`, `instructions` et `voice` ; un champ absent garde sa valeur.

    Tout est validé avant d'enregistrer quoi que ce soit : un champ refusé n'en laisse aucun autre enregistré. Un
    formulaire qui n'en porte aucun est refusé (400) plutôt que d'annoncer un enregistrement qui n'a pas eu lieu.
    """
    user_id, fields = await _read_post(request)
    changes: dict[str, str] = {}
    if "glossary" in fields:
        changes["glossary"] = _checked_glossary(fields["glossary"])
    if "instructions" in fields:
        changes["instructions"] = _checked_instructions(fields["instructions"])
    if "voice" in fields:
        try:
            changes["voice"] = await _known_voice(request.app.state, fields["voice"])
        except NEMO_ERRORS as error:
            logger.warning("voices unavailable class=%s", type(error).__name__)
            message = "Le moteur vocal ne répond pas, la liste des voix est indisponible. Rien n'a été enregistré."
            raise WebError(502, message) from None
    if not changes:
        raise WebError(400, "Aucune modification : le formulaire ne contient ni glossaire, ni consignes, ni voix.")
    request.app.state.db.save_settings(user_id, **changes)
    return RedirectResponse("/?saved=1", status_code=303)


async def reset_instructions(request: Request) -> Response:
    user_id, _ = await _read_post(request)
    request.app.state.db.save_settings(user_id, instructions="")
    return RedirectResponse("/?saved=1", status_code=303)


async def _mp3(pcm: bytes) -> bytes:
    async def chunks() -> AsyncIterator[bytes]:
        yield pcm

    async with contextlib.aclosing(
        encode_stream(chunks(), fmt="mp3", speed=1.0, input_rate=TTS_SAMPLE_RATE)
    ) as encoded:
        return b"".join([chunk async for chunk in encoded])


def _mp3_response(mp3: bytes, range_header: str | None) -> Response:
    """Réponse portant `mp3`, qui suit une requête `Range` d'une seule plage, comme Safari l'exige d'un `<audio>`.

    Une plage satisfaisable donne 206 avec `Content-Range`, une plage qui commence après la fin donne 416. Un en-tête
    absent, d'une autre unité, mal formé ou de plusieurs plages est ignoré, ce que la RFC 9110 permet : le fichier
    entier est rendu.
    """
    headers = {"Accept-Ranges": "bytes"}
    media_type = OUTPUT_FORMATS["mp3"]
    size = len(mp3)
    match = _BYTE_RANGE.fullmatch(range_header or "")
    first, last = (None if group is None else int(group) for group in match.groups()) if match else (None, None)
    reversed_span = first is not None and last is not None and last < first
    if (first is None and last is None) or reversed_span:
        return Response(mp3, media_type=media_type, headers=headers)
    if first is None:  # `bytes=-n` : les n derniers octets, aucun si n vaut 0
        start, end = (max(size - last, 0) if last else size), size - 1
    else:
        start, end = first, size - 1 if last is None else min(last, size - 1)
    if start >= size:
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return Response(mp3[start : end + 1], status_code=206, media_type=media_type, headers=headers)


async def voice_preview(request: Request) -> Response:
    """Aperçu d'une voix : `PREVIEW_TEXT` en français, en mp3. Le texte est fixe et n'est pas journalisé.

    L'aperçu de chaque voix est synthétisé une fois, puis gardé (`PreviewCache`) : relire un aperçu ou en lire les
    plages ne sollicite plus NeMo.
    """
    if _session_user(request) is None:
        raise WebError(401, "Connectez-vous pour écouter les voix.", LOGIN_LINK)
    state = request.app.state

    async def synthesize(voice: str) -> bytes:
        pcm = await state.nemo.speak(PREVIEW_TEXT, voice=voice, language=LANGUAGE)
        return await _mp3(pcm)

    try:
        voice = await _known_voice(state, request.query_params.get("voice", ""))
        mp3 = await state.previews.get(voice, synthesize)
    except NEMO_ERRORS as error:
        raise nemo_failure(error) from None
    return _mp3_response(mp3, request.headers.get("range"))
