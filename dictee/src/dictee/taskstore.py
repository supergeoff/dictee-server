"""Registre des tâches de transcription et de leurs morceaux, dans la base SQLite de dictee.

Toutes les dates sont des secondes Unix entières. Les colonnes JSON (glossaire, points de coupe, résultat, mots) sont
écrites avec des clés triées, donc d'une forme stable. Une tâche finie (réussie, en échec ou annulée) garde sa première
fin : ni `start`, ni `complete`, ni `fail`, ni `cancel` ne la modifient, et elle reste lisible jusqu'à `expires_at`.
"""

import contextlib
import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum

from dictee.db import Database
from dictee.modes import Mode
from dictee.profile import GlossaryEntry
from dictee.types import Segment, Transcript, Word

# Alphabet de Crockford : les chiffres et les lettres sans I, L, O ni U. L'ordre des caractères est celui des valeurs,
# donc deux identifiants se trient dans l'ordre de leur horodatage.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ID_PREFIX = "trn_"
_TIME_BITS = 48
_RANDOM_BITS = 80
_ID_DIGITS = 26


class TaskStatus(StrEnum):
    """Statuts d'une tâche, ceux du mode `background` d'OpenAI."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})

# Condition SQL des tâches qui ne sont pas finies, écrite d'après les statuts pour ne pas les répéter à la main.
_UNFINISHED = f"status in ('{TaskStatus.QUEUED}', '{TaskStatus.IN_PROGRESS}')"


def _dump(value: object) -> str:
    """JSON compact aux clés triées : la même valeur donne toujours le même texte."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _glossary_json(glossary: Iterable[GlossaryEntry]) -> list[dict]:
    return [{"term": entry.term, "heard_as": list(entry.heard_as)} for entry in glossary]


def _words_json(words: Iterable[Word]) -> list[dict]:
    return [{"text": word.text, "start": word.start, "end": word.end, "speaker": word.speaker} for word in words]


def _words(items: list[dict]) -> tuple[Word, ...]:
    return tuple(Word(item["text"], item["start"], item["end"], item["speaker"]) for item in items)


def _segments_json(segments: Iterable[Segment]) -> list[dict]:
    return [
        {"id": segment.id, "start": segment.start, "end": segment.end, "speaker": segment.speaker, "text": segment.text}
        for segment in segments
    ]


def _segments(items: list[dict]) -> tuple[Segment, ...]:
    return tuple(Segment(item["id"], item["start"], item["end"], item["speaker"], item["text"]) for item in items)


def new_task_id(now_ms: int | None = None) -> str:
    """Identifiant d'une tâche : `trn_` puis 26 caractères de Crockford.

    Les 48 premiers bits sont l'horodatage en millisecondes (l'heure courante par défaut) et les 80 suivants sont
    aléatoires : les identifiants se trient dans l'ordre de création, et deux tâches nées à la même milliseconde ne se
    confondent pas. Un horodatage qui ne tient pas sur 48 bits lève `ValueError`.
    """
    if now_ms is None:
        now_ms = time.time_ns() // 1_000_000
    if not 0 <= now_ms < 1 << _TIME_BITS:
        raise ValueError(f"the timestamp must fit in {_TIME_BITS} bits, got {now_ms}")
    value = (now_ms << _RANDOM_BITS) | secrets.randbits(_RANDOM_BITS)
    digits = (_CROCKFORD[(value >> (5 * position)) & 31] for position in reversed(range(_ID_DIGITS)))
    return _ID_PREFIX + "".join(digits)


@dataclass(frozen=True)
class TaskParams:
    """Réglages figés à la création d'une tâche : une reprise travaille avec eux, pas avec ceux du compte à ce moment.

    `language` est au format NeMo (`fr-FR`). `instructions` n'est rempli que pour `Mode.CLEAN`.
    """

    language: str
    mode: Mode
    glossary: tuple[GlossaryEntry, ...]
    boost: float
    instructions: str | None

    def digest(self) -> str:
        """SHA-256 hexadécimal du JSON trié des cinq champs : deux envois qui le partagent se traitent de même."""
        fields = {
            "language": self.language,
            "mode": self.mode.value,
            "glossary": _glossary_json(self.glossary),
            "boost": float(self.boost),
            "instructions": self.instructions,
        }
        return hashlib.sha256(_dump(fields).encode()).hexdigest()


def _params_json(params: TaskParams) -> str:
    """JSON de la colonne `params` : le glossaire (termes et déformations), la force et les consignes.

    La langue et le mode ont chacun leur colonne.
    """
    return _dump(
        {"glossary": _glossary_json(params.glossary), "boost": float(params.boost), "instructions": params.instructions}
    )


def _params(language: str, mode: str, stored: str) -> TaskParams:
    """Reforme les réglages d'après les colonnes `language`, `mode` et `params`."""
    fields = json.loads(stored)
    glossary = tuple(GlossaryEntry(item["term"], tuple(item["heard_as"])) for item in fields["glossary"])
    return TaskParams(language, Mode(mode), glossary, fields["boost"], fields["instructions"])


@dataclass(frozen=True)
class TaskResult:
    """Résultat d'une tâche sous sa forme neutre : le format de réponse se choisit à la lecture.

    `cleaned` est le texte reformulé de `dictee-clean`, et `decision` la raison qui a donné ce texte ou gardé le brut.
    """

    text: str
    words: tuple[Word, ...]
    segments: tuple[Segment, ...] | None
    duration: float
    language: str
    cleaned: str | None = None
    decision: str | None = None

    def to_json(self) -> str:
        """Forme enregistrée dans la colonne `result` : du JSON aux clés triées, donc stable."""
        return _dump(
            {
                "text": self.text,
                "words": _words_json(self.words),
                "segments": None if self.segments is None else _segments_json(self.segments),
                "duration": self.duration,
                "language": self.language,
                "cleaned": self.cleaned,
                "decision": self.decision,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> "TaskResult":
        """Relit un résultat écrit par `to_json`."""
        fields = json.loads(raw)
        segments = fields["segments"]
        return cls(
            text=fields["text"],
            words=_words(fields["words"]),
            segments=None if segments is None else _segments(segments),
            duration=fields["duration"],
            language=fields["language"],
            cleaned=fields["cleaned"],
            decision=fields["decision"],
        )

    def transcript(self) -> Transcript:
        """La transcription à rendre : le texte reformulé quand il existe, sinon le texte brut."""
        text = self.text if self.cleaned is None else self.cleaned
        return Transcript(text, self.words, self.duration, self.language, self.segments)


@dataclass(frozen=True)
class ChunkRecord:
    """Un morceau d'un fichier entier : sa tranche d'audio (en secondes), son texte et ses mots, déjà décalés."""

    idx: int
    start: float
    end: float
    text: str
    words: tuple[Word, ...]


@dataclass(frozen=True)
class TaskRecord:
    """Une tâche telle que la base la garde. `owner` vaut `None` pour le compte par défaut."""

    id: str
    owner: int | None
    model: str
    params: TaskParams
    response_format: str
    granularities: frozenset[str]
    upload_sha256: str
    status: TaskStatus
    error_code: str | None
    error_message: str | None
    duration: float
    progress: float
    cuts: tuple[float, ...] | None
    result: TaskResult | None
    attempts: int
    created_at: int
    started_at: int | None
    completed_at: int | None
    expires_at: int | None


def _record(row: sqlite3.Row) -> TaskRecord:
    cuts, result = row["cuts"], row["result"]
    return TaskRecord(
        id=row["id"],
        owner=row["owner"],
        model=row["model"],
        params=_params(row["language"], row["mode"], row["params"]),
        response_format=row["response_format"],
        granularities=frozenset(json.loads(row["granularities"])),
        upload_sha256=row["upload_sha256"],
        status=TaskStatus(row["status"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
        duration=row["duration"],
        progress=row["progress"],
        cuts=None if cuts is None else tuple(float(cut) for cut in json.loads(cuts)),
        result=None if result is None else TaskResult.from_json(result),
        attempts=row["attempts"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        expires_at=row["expires_at"],
    )


def _expiry(now: int, retention_s: float) -> int:
    """Date d'effacement d'une tâche finie à `now`, en secondes entières."""
    return int(now + retention_s)


class TaskStore:
    """Registre des tâches et de leurs morceaux. Chaque méthode est une transaction de la base."""

    def __init__(self, db: Database) -> None:
        self._db = db

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Connexion de la base dont les lignes se lisent par nom de colonne."""
        with self._db.connect() as conn:
            conn.row_factory = sqlite3.Row
            yield conn

    @staticmethod
    def _fetch(conn: sqlite3.Connection, task_id: str) -> TaskRecord | None:
        row = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
        return None if row is None else _record(row)

    @staticmethod
    def _found(record: TaskRecord | None, task_id: str) -> TaskRecord:
        if record is None:
            raise KeyError(task_id)
        return record

    def _finish(
        self, task_id: str, status: TaskStatus, now: int, retention_s: float, **columns: object
    ) -> TaskRecord | None:
        """Pose la fin d'une tâche qui n'est pas finie, et rend la tâche, ou `None` si elle n'existe pas.

        Une tâche déjà finie n'est pas touchée : sa première fin reste la bonne, même si le traitement qui la
        concernait se termine après une annulation. Les noms de `columns` viennent du code, jamais de l'appelant.
        """
        columns |= {"status": status.value, "completed_at": now, "expires_at": _expiry(now, retention_s)}
        assignments = ", ".join(f"{name} = ?" for name in columns)
        with self._connect() as conn:
            conn.execute(f"update tasks set {assignments} where id = ? and {_UNFINISHED}", (*columns.values(), task_id))
            return self._fetch(conn, task_id)

    def create(
        self,
        *,
        task_id: str,
        owner: int | None,
        model: str,
        params: TaskParams,
        response_format: str,
        granularities: Iterable[str],
        upload_sha256: str,
        duration: float,
        now: int,
    ) -> TaskRecord:
        """Enregistre une tâche en file, sans progrès, et la rend."""
        with self._connect() as conn:
            conn.execute(
                "insert into tasks (id, owner, model, mode, language, response_format, granularities, upload_sha256, "
                "params_sha256, params, status, duration, created_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    owner,
                    model,
                    params.mode.value,
                    params.language,
                    response_format,
                    _dump(sorted(set(granularities))),
                    upload_sha256,
                    params.digest(),
                    _params_json(params),
                    TaskStatus.QUEUED.value,
                    duration,
                    now,
                ),
            )
            return self._found(self._fetch(conn, task_id), task_id)

    def get(self, task_id: str) -> TaskRecord | None:
        with self._connect() as conn:
            return self._fetch(conn, task_id)

    def get_visible(self, task_id: str, owner: int | None, now: int) -> TaskRecord | None:
        """La tâche de ce propriétaire ; `None` pour un autre propriétaire, une tâche inconnue ou effacée à `now`.

        `owner is ?` compare aussi `NULL`, le compte par défaut.
        """
        with self._connect() as conn:
            row = conn.execute(
                "select * from tasks where id = ? and owner is ? and (expires_at is null or expires_at > ?)",
                (task_id, owner, now),
            ).fetchone()
        return None if row is None else _record(row)

    def find_identical(self, owner: int | None, upload_sha256: str, params_digest: str, now: int) -> TaskRecord | None:
        """La plus récente des tâches du propriétaire qu'un même envoi peut rejoindre, ou `None`.

        Une tâche se rejoint en file ou en cours, ou terminée avec succès tant qu'elle n'est pas effacée à `now`.
        Une tâche en échec ou annulée ne se rejoint jamais.
        """
        with self._connect() as conn:
            row = conn.execute(
                "select * from tasks where owner is ? and upload_sha256 = ? and params_sha256 = ? "
                f"and ({_UNFINISHED} or (status = ? and expires_at > ?)) "
                "order by created_at desc, id desc limit 1",
                (owner, upload_sha256, params_digest, TaskStatus.COMPLETED.value, now),
            ).fetchone()
        return None if row is None else _record(row)

    def start(self, task_id: str, now: int) -> TaskRecord:
        """Passe la tâche en cours et compte ce démarrage ; `started_at` garde la date du premier.

        Une tâche déjà finie reste telle quelle. Lève `KeyError` pour une tâche inconnue.
        """
        with self._connect() as conn:
            conn.execute(
                f"update tasks set status = ?, attempts = attempts + 1, started_at = coalesce(started_at, ?) "
                f"where id = ? and {_UNFINISHED}",
                (TaskStatus.IN_PROGRESS.value, now, task_id),
            )
            return self._found(self._fetch(conn, task_id), task_id)

    def set_cuts(self, task_id: str, cuts: Iterable[float]) -> None:
        """Garde les points de coupe : une reprise retrouve ainsi les mêmes morceaux.

        Un audio sans point de coupe se garde en tuple vide, à ne pas confondre avec `None` (points pas encore posés).
        """
        with self._connect() as conn:
            conn.execute("update tasks set cuts = ? where id = ?", (_dump(list(cuts)), task_id))

    def save_chunk(self, task_id: str, chunk: ChunkRecord) -> None:
        """Garde un morceau et remplace celui du même rang."""
        with self._connect() as conn:
            conn.execute(
                'insert into task_chunks (task_id, idx, start, "end", text, words) values (?, ?, ?, ?, ?, ?) '
                'on conflict (task_id, idx) do update set start = excluded.start, "end" = excluded."end", '
                "text = excluded.text, words = excluded.words",
                (task_id, chunk.idx, chunk.start, chunk.end, chunk.text, _dump(_words_json(chunk.words))),
            )

    def chunks(self, task_id: str) -> list[ChunkRecord]:
        """Morceaux gardés d'une tâche, par rang."""
        with self._connect() as conn:
            rows = conn.execute(
                'select idx, start, "end", text, words from task_chunks where task_id = ? order by idx', (task_id,)
            ).fetchall()
        return [
            ChunkRecord(row["idx"], row["start"], row["end"], row["text"], _words(json.loads(row["words"])))
            for row in rows
        ]

    def set_progress(self, task_id: str, progress: float) -> None:
        """Écrit l'avancement d'une tâche qui n'est pas finie ; celui d'une tâche finie ne bouge plus."""
        with self._connect() as conn:
            conn.execute(f"update tasks set progress = ? where id = ? and {_UNFINISHED}", (progress, task_id))

    def complete(self, task_id: str, result: TaskResult, now: int, retention_s: float) -> TaskRecord:
        """Termine la tâche avec son résultat (avancement à 1), gardé jusqu'à `now + retention_s`.

        Lève `KeyError` pour une tâche inconnue.
        """
        record = self._finish(task_id, TaskStatus.COMPLETED, now, retention_s, progress=1.0, result=result.to_json())
        return self._found(record, task_id)

    def fail(self, task_id: str, code: str, message: str, now: int, retention_s: float) -> TaskRecord:
        """Termine la tâche en échec avec son code et son message, gardés jusqu'à `now + retention_s`.

        Lève `KeyError` pour une tâche inconnue.
        """
        record = self._finish(task_id, TaskStatus.FAILED, now, retention_s, error_code=code, error_message=message)
        return self._found(record, task_id)

    def cancel(self, task_id: str, now: int, retention_s: float) -> TaskRecord | None:
        """Annule la tâche, sans effet sur une tâche finie ; `None` pour un identifiant inconnu."""
        return self._finish(task_id, TaskStatus.CANCELLED, now, retention_s)

    def unfinished(self) -> list[TaskRecord]:
        """Tâches en file ou en cours, de la plus ancienne à la plus récente, puis par identifiant."""
        with self._connect() as conn:
            rows = conn.execute(f"select * from tasks where {_UNFINISHED} order by created_at, id").fetchall()
        return [_record(row) for row in rows]

    def delete_expired(self, now: int) -> list[str]:
        """Efface les tâches dont `expires_at` est atteint, avec leurs morceaux, et rend leurs identifiants triés."""
        with self._connect() as conn:
            rows = conn.execute(
                "delete from tasks where expires_at is not null and expires_at <= ? returning id", (now,)
            ).fetchall()
        return sorted(row["id"] for row in rows)

    def known_ids(self) -> set[str]:
        """Identifiants de toutes les tâches en base, quel que soit leur statut."""
        with self._connect() as conn:
            return {row["id"] for row in conn.execute("select id from tasks")}
