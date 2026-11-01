"""Page de configuration de dictee et connexion par PocketID.

Chacun se connecte avec PocketID (OIDC, code d'autorisation avec PKCE), puis règle sur une seule page ses clés d'API,
son glossaire, ses consignes de reformulation et sa voix. La page n'a pas de JavaScript : chaque action est un
formulaire, et l'écoute d'une voix passe par un élément `<audio>`.

La session est le cookie signé de `SessionMiddleware` ; elle ne garde que `user_id` et `csrf`. Chaque `POST` exige une
session et le jeton anti-CSRF, présent dans chaque formulaire. Une clé nouvelle n'existe en clair que dans la réponse
au `POST /keys` : elle n'est ni rangée dans la session, ni journalisée. Les réponses HTML ne sont jamais mises en
cache et portent une politique de sécurité qui n'autorise ni script, ni image, ni ressource d'un autre site.

Le journal ne garde d'une connexion que le numéro du compte, jamais l'email, le nom, un jeton ni un réglage.
"""

import contextlib
import logging
import secrets
import sqlite3
from collections.abc import AsyncIterator, Mapping

from authlib.integrations.starlette_client import OAuth
from jinja2 import Environment, PackageLoader, StrictUndefined
from starlette.datastructures import State
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.templating import Jinja2Templates

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
    user_id = request.session.get("user_id")
    return user_id if type(user_id) is int else None


def _clean(text: str) -> str:
    """Texte d'un formulaire : sauts de ligne `\\n` et sans espace de bord, comme on le compare et le range."""
    return text.replace("\r\n", "\n").strip()


async def _read_post(request: Request) -> tuple[int, dict[str, str]]:
    """Compte de la session et champs textuels d'un `POST` ; 403 sans session ou sans jeton anti-CSRF valide.

    La session se vérifie avant de lire le corps : sans session, le corps n'est pas lu.
    """
    user_id = _session_user(request)
    expected = request.session.get("csrf")
    if user_id is None or not isinstance(expected, str):
        raise WebError(403, "Votre session a expiré. Connectez-vous de nouveau.", LOGIN_LINK)
    try:
        async with request.form() as form:
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


async def callback(request: Request) -> Response:
    """Retour de PocketID : ouvre une session pour le compte, créé au premier passage.

    Un email déjà pris par un autre compte refuse la connexion : rattacher le compte existant au nouvel identifiant
    laisserait quiconque met l'email d'un autre dans PocketID prendre sa place.
    """
    state = request.app.state
    try:
        token = await state.oauth.pocketid.authorize_access_token(request)
    except Exception as error:
        # Authlib, joserfc et httpx lèvent chacun leurs classes (état, jeton, signature, réseau) : toutes refusent la
        # connexion, et seule la classe est journalisée, car le message peut citer le code ou le jeton.
        logger.warning("login refused class=%s", type(error).__name__)
        raise WebError(400, "La connexion avec PocketID a échoué.", LOGIN_LINK) from None
    claims = token.get("userinfo")
    claims = claims if isinstance(claims, Mapping) else {}
    sub, email, name = claims.get("sub"), claims.get("email"), claims.get("name")
    if not (isinstance(sub, str) and sub.strip() and isinstance(email, str) and email.strip()):
        raise WebError(400, "PocketID n'a pas transmis l'identifiant ou l'email du compte.", LOGIN_LINK)
    try:
        user_id = state.db.upsert_user(sub, email, name if isinstance(name, str) and name.strip() else email)
    except sqlite3.IntegrityError:
        message = "Un autre compte utilise déjà cet email. Adressez-vous à l'administrateur."
        raise WebError(409, message, LOGIN_LINK) from None
    # Session vidée avant d'être remplie : un cookie déposé avant la connexion ne devient pas celui du compte.
    request.session.clear()
    request.session["user_id"] = user_id
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

    Tout est validé avant d'enregistrer quoi que ce soit : un champ refusé n'en laisse aucun autre enregistré.
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


async def voice_preview(request: Request) -> Response:
    """Aperçu d'une voix : `PREVIEW_TEXT` en français, en mp3. Le texte est fixe et n'est pas journalisé."""
    if _session_user(request) is None:
        raise WebError(401, "Connectez-vous pour écouter les voix.", LOGIN_LINK)
    state = request.app.state
    try:
        voice = await _known_voice(state, request.query_params.get("voice", ""))
        pcm = await state.nemo.speak(PREVIEW_TEXT, voice=voice, language=LANGUAGE)
    except NEMO_ERRORS as error:
        raise nemo_failure(error) from None
    return Response(await _mp3(pcm), media_type=OUTPUT_FORMATS["mp3"])
