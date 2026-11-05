"""Base SQLite de dictee : comptes, clés d'API et réglages de chacun."""

import contextlib
import hashlib
import secrets
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

KEY_PREFIX = "dk_"
# Nombre de caractères de la clé que la page affiche pour la reconnaître.
SHOWN_PREFIX_LENGTH = 10

# Version du schéma, rangée dans `pragma user_version` : une migration s'y repère pour savoir d'où elle part.
# La version 1 porte les comptes, les clés et les réglages ; la version 2 ajoute les tâches de transcription.
SCHEMA_VERSION = 2

# Tables de la version 1, créées à chaque ouverture si elles manquent.
_SCHEMA = """
create table if not exists users (
    id integer primary key,
    sub text not null unique,
    email text not null unique,
    name text not null,
    created_at text not null,
    last_login_at text not null
);
create table if not exists api_keys (
    id integer primary key,
    user_id integer not null references users (id),
    label text not null,
    prefix text not null,
    hash text not null unique,
    created_at text not null,
    last_used_at text,
    revoked_at text
);
create index if not exists api_keys_user_id on api_keys (user_id);
create table if not exists settings (
    user_id integer primary key references users (id),
    glossary text not null default '',
    instructions text not null default '',
    voice text not null default ''
);
"""

# Tables ajoutées par la version 2. Les dates sont des secondes Unix entières ; les colonnes JSON gardent un ordre
# stable. `owner` est le numéro d'un compte, ou `NULL` pour le compte par défaut : il ne référence pas `users`, car
# le compte par défaut n'a pas de ligne. `end` est un mot réservé de SQL, d'où ses guillemets.
_TASKS_SCHEMA = """
create table if not exists tasks (
    id text primary key,
    owner integer,
    model text not null,
    mode text not null,
    language text not null,
    response_format text not null,
    granularities text not null,
    upload_sha256 text not null,
    params_sha256 text not null,
    params text not null,
    status text not null,
    error_code text,
    error_message text,
    duration real not null,
    progress real not null default 0,
    cuts text,
    result text,
    attempts integer not null default 0,
    created_at integer not null,
    started_at integer,
    completed_at integer,
    expires_at integer
);
create index if not exists tasks_identical on tasks (owner, upload_sha256, params_sha256);
create table if not exists task_chunks (
    task_id text not null references tasks (id) on delete cascade,
    idx integer not null,
    start real not null,
    "end" real not null,
    text text not null,
    words text not null,
    primary key (task_id, idx)
);
"""


@dataclass(frozen=True)
class KeyInfo:
    """Ce que la page montre d'une clé : jamais la clé elle-même."""

    id: int
    label: str
    prefix: str
    created_at: str
    last_used_at: str | None
    revoked_at: str | None


@dataclass(frozen=True)
class StoredSettings:
    """Réglages d'un compte ; une chaîne vide renvoie à la valeur par défaut."""

    glossary: str
    instructions: str
    voice: str


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _now() -> str:
    """Date courante en ISO 8601 UTC."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _normalize_email(email: str) -> str:
    return email.strip().lower()


class Database:
    """Accès à la base SQLite.

    Chaque opération ouvre sa propre connexion et la ferme : l'app tourne sur
    plusieurs threads (l'événement et le pool de Starlette) et aucune connexion
    n'a besoin d'être partagée. Les emails sont rangés en minuscules, les dates
    des comptes en ISO 8601 UTC. Les tâches de transcription, qui vivent dans
    la même base, sont gérées par `TaskStore` à travers `connect()`.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
            # Une base neuve ou de la version 1 (ou créée avant le versionnement) gagne les tables des tâches ;
            # une version plus récente n'est pas touchée.
            if conn.execute("pragma user_version").fetchone()[0] < SCHEMA_VERSION:
                conn.executescript(_TASKS_SCHEMA)
                conn.execute(f"pragma user_version = {SCHEMA_VERSION}")

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Ouvre une connexion, valide la transaction à la sortie (ou l'annule sur erreur), puis la ferme.

        Les clés étrangères sont actives : effacer une tâche efface ses morceaux.
        """
        conn = sqlite3.connect(self._path)
        try:
            conn.execute("pragma foreign_keys = on")
            with conn:
                yield conn
        finally:
            conn.close()

    def upsert_user(self, sub: str, email: str, name: str) -> int:
        """Crée le compte au premier passage d'un `sub` ; ensuite met à jour son email et son nom."""
        now = _now()
        with self.connect() as conn:
            row = conn.execute(
                "insert into users (sub, email, name, created_at, last_login_at) values (?, ?, ?, ?, ?) "
                "on conflict (sub) do update set "
                "email = excluded.email, name = excluded.name, last_login_at = excluded.last_login_at "
                "returning id",
                (sub, _normalize_email(email), name, now, now),
            ).fetchone()
        return row[0]

    def user_by_email(self, email: str) -> int | None:
        with self.connect() as conn:
            row = conn.execute("select id from users where email = ?", (_normalize_email(email),)).fetchone()
        return None if row is None else row[0]

    def user_sub(self, user_id: int) -> str | None:
        """`sub` PocketID du compte ; `None` si le compte n'existe plus.

        Une session cite un numéro de compte et son `sub` : si la base a été remise à zéro, un autre compte peut avoir
        repris ce numéro, et seul le `sub` dit que la session n'est plus la sienne.
        """
        with self.connect() as conn:
            row = conn.execute("select sub from users where id = ?", (user_id,)).fetchone()
        return None if row is None else row[0]

    def create_key(self, user_id: int, label: str) -> str:
        """Crée une clé et la renvoie en clair : c'est la seule fois, la base n'en garde que le hachage."""
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        with self.connect() as conn:
            conn.execute(
                "insert into api_keys (user_id, label, prefix, hash, created_at) values (?, ?, ?, ?, ?)",
                (user_id, label, key[:SHOWN_PREFIX_LENGTH], hash_key(key), _now()),
            )
        return key

    def key_owner(self, key: str) -> int | None:
        """Propriétaire d'une clé et mise à jour de son dernier usage ; `None` si la clé est inconnue ou révoquée."""
        with self.connect() as conn:
            row = conn.execute(
                "update api_keys set last_used_at = ? where hash = ? and revoked_at is null returning user_id",
                (_now(), hash_key(key)),
            ).fetchone()
        return None if row is None else row[0]

    def list_keys(self, user_id: int) -> list[KeyInfo]:
        """Clés d'un compte, révoquées comprises, de la plus ancienne à la plus récente."""
        with self.connect() as conn:
            rows = conn.execute(
                "select id, label, prefix, created_at, last_used_at, revoked_at "
                "from api_keys where user_id = ? order by id",
                (user_id,),
            ).fetchall()
        return [KeyInfo(*row) for row in rows]

    def revoke_key(self, user_id: int, key_id: int) -> bool:
        """Révoque une clé de ce compte ; `False` si elle n'existe pas, appartient à un autre ou est déjà révoquée."""
        with self.connect() as conn:
            cursor = conn.execute(
                "update api_keys set revoked_at = ? where id = ? and user_id = ? and revoked_at is null",
                (_now(), key_id, user_id),
            )
        return cursor.rowcount > 0

    def get_settings(self, user_id: int) -> StoredSettings:
        with self.connect() as conn:
            row = conn.execute(
                "select glossary, instructions, voice from settings where user_id = ?", (user_id,)
            ).fetchone()
        return StoredSettings("", "", "") if row is None else StoredSettings(*row)

    def save_settings(
        self,
        user_id: int,
        *,
        glossary: str | None = None,
        instructions: str | None = None,
        voice: str | None = None,
    ) -> None:
        """Enregistre les réglages donnés ; ceux qui valent `None` gardent leur valeur."""
        with self.connect() as conn:
            conn.execute("insert into settings (user_id) values (?) on conflict (user_id) do nothing", (user_id,))
            conn.execute(
                "update settings set glossary = coalesce(?, glossary), "
                "instructions = coalesce(?, instructions), voice = coalesce(?, voice) where user_id = ?",
                (glossary, instructions, voice, user_id),
            )
