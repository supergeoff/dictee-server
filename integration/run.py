# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx==0.28.1", "openai==3.42.0", "websockets==17.4"]
# ///
"""Test d'intégration de bout en bout de dictee, sur la stack complète et les vrais modèles (spec, section 8).

Prérequis : `make integration`, qui démarre la stack (`compose.yaml` et `compose.integration.yaml`), attend `/health`
puis lance ce script. Usage : uv run --script integration/run.py [--only a,b,c]

`--only` rejoue seulement les contrôles nommés, avec ceux dont ils dépendent, sur une stack déjà démarrée : pratique
pour refaire un contrôle long (`--only hangup`) sans relancer tout le reste. `--help` donne la liste des noms.

dictee répond sur http://127.0.0.1:18000. Le script l'appelle avec la clé de service (`OPENWEBUI_SERVICE_KEY` de
l'environnement, sinon du `.env`, jamais affichée) et sans en-tête d'email : les valeurs par défaut s'appliquent. Chaque contrôle affiche `OK`
ou `ÉCHEC` avec sa durée, et le script s'arrête au premier échec. Les lignes `INFO` sont des mesures, sans verdict.
Les contrôles rapides passent d'abord, les contrôles longs à la fin, l'un après l'autre. Le dernier, `restart`,
redémarre le service `dictee` avec `compose restart` et attend qu'il soit de nouveau sain.

Chaque envoi qui doit créer sa propre tâche porte une marque inaudible et unique (voir `mark`) : dictee rejoindrait
sinon la tâche d'un envoi identique, même terminée, et un second lancement du script ne mesurerait plus rien. Un
contrôle annule à sa sortie les tâches qu'il a lancées et qui ne sont pas finies, et vérifie que leurs dossiers ont
disparu de `/data/tasks`.

Le script ne lit du `.env` que la clé de service et les réglages de la file des tâches longues, par
`nemo_rt.env_value`, et n'affiche aucune valeur de ce fichier. Les audios produits vont dans `.integration/`, ignoré
par git.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import queue
import re
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import openai

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_nemo  # noqa: E402
import nemo_rt  # noqa: E402

ROOT = nemo_rt.ROOT
WORK = ROOT / ".integration"
HOST, PORT = "127.0.0.1", 18000
BASE_URL = f"http://{HOST}:{PORT}"
TRANSCRIPTIONS = "/v1/audio/transcriptions"

# Fréquence de tous les formats de sortie de la synthèse de dictee (`dictee.audio`).
SPEECH_RATE = 24000
MIN_WORDS_FOUND = 0.6
SPEED_TOLERANCE = 0.10
# Une transcription est longue au-delà de LONG_AUDIO_SECONDS (600 s par défaut) : 11 minutes la dépassent.
LONG_SECONDS = 11 * 60
# Un audio court qui dure assez pour qu'on raccroche pendant sa tâche : quatre morceaux d'environ 30 s.
MEDIUM_SECONDS = 2 * 60
# Avance de la transcription longue sur la requête qui la suit, en secondes.
LONG_HEADSTART = 5.0
# Raccroché en plein flux : attente maximale du premier delta après l'envoi de l'audio, puis fenêtre de relevé du CPU
# de nemo après le raccroché.
FIRST_DELTA_TIMEOUT = 30.0
HANGUP_WINDOW = 15.0
# Une requête synchrone coupée l'est après CUT_AFTER secondes, avant que sa tâche ait fini.
CUT_AFTER = 5.0
# Intervalle de lecture d'une tâche par `GET`, et attente maximale de la fin d'une tâche, en secondes.
POLL_INTERVAL = 0.5
TASK_TIMEOUT = 900.0
# Attente maximale d'une ligne de journal, puis de la santé de dictee après `compose restart`, en secondes.
LOG_TIMEOUT = 15.0
HEALTHY_TIMEOUT = 150.0
# Dictée en direct : attente maximale de `input_audio_buffer.cleared` après `clear`, puis du texte final après `commit`,
# en secondes, et durée de l'audio envoyé avant `clear`.
CLEARED_TIMEOUT = 5.0
COMPLETED_TIMEOUT = 60.0
CLEARED_AUDIO_SECONDS = 1.0
LIVE_FRAME_SECONDS = 0.5
# Débit minimal, en octets par seconde, de l'envoi d'une requête à dictee : au-dessous, l'envoi échoue.
SEND_MIN_RATE = 1024**2
# CPU de nemo (en pourcents d'un cœur) au-dessus duquel il travaille, et au-dessous duquel il est au repos.
BUSY_CPU = 50.0
IDLE_CPU = 10.0
NEMO_MEMORY_LIMIT = 3 * 1024**3
DICTEE_MEMORY_LIMIT = 512 * 1024**2
# Version de NeMo-Speech.cpp dans compose.yaml : un tag explicite x.y.z (AGENTS.md), sans la répéter ici.
EXPLICIT_VERSION = r"\d+\.\d+\.\d+"
# Règles de compose.yaml : service -> (restart, limite de mémoire en octets).
COMPOSE_RULES = {
    "models": ("no", 512 * 1024**2),
    "nemo": ("unless-stopped", NEMO_MEMORY_LIMIT),
    "dictee": ("unless-stopped", DICTEE_MEMORY_LIMIT),
}
# Début de chaque format de synthèse (dictee/tests/test_audio.py, tâche 5). Le `pcm` n'a pas de signature.
SIGNATURES = {
    "mp3": (b"ID3", b"\xff\xfb", b"\xff\xf3"),
    "opus": (b"OggS",),
    "aac": (b"\xff\xf1", b"\xff\xf9"),
    "flac": (b"fLaC",),
}
DELTA = b"transcript.text.delta"
TERMINAL = frozenset({"completed", "failed", "cancelled"})
# Dossier des WAV des tâches dans le conteneur dictee : à côté de la base, `/data/dictee.db` par défaut.
TASKS_DIR = "/data/tasks"
LIVE_DELTA = "conversation.item.input_audio_transcription.delta"
LIVE_COMPLETED = "conversation.item.input_audio_transcription.completed"


class Failure(Exception):
    """Un contrôle qui échoue ; le message dit pourquoi."""


def expect(condition: object, message: str) -> None:
    if not condition:
        raise Failure(message)


def info(text: str) -> None:
    print(f"INFO {text}", flush=True)


def note(text: str) -> None:
    print(f"   {text}", flush=True)


def words(text: str) -> list[str]:
    """Les mots d'un texte, en minuscules et sans ponctuation."""
    return re.findall(r"\w+", text.lower())


def found_ratio(expected: list[str], text: str) -> tuple[int, float]:
    """Le nombre de mots de `expected` retrouvés dans `text`, chacun autant de fois qu'il y est attendu, et leur part."""
    found = sum((Counter(expected) & Counter(words(text))).values())
    return found, found / len(expected)


def silence(seconds: float, rate: int = SPEECH_RATE) -> bytes:
    return bytes(int(rate * seconds) * 2)


def mark(pcm: bytes) -> bytes:
    """`pcm` suivi d'une marque inaudible et unique : un dixième de seconde de silence, puis 64 échantillons à 0 ou 1.

    dictee rejoint la tâche d'un envoi aux mêmes octets, en cours ou réussie depuis moins de 24 heures : un envoi qui
    doit créer sa propre tâche porte une marque neuve, tirée au hasard, d'un contrôle à l'autre comme d'un lancement
    du script à l'autre.
    """
    bits = int.from_bytes(secrets.token_bytes(8), "little")
    return pcm + silence(0.1) + b"".join(struct.pack("<h", (bits >> index) & 1) for index in range(64))


def repeat_pcm(pcm: bytes, rate: int, seconds: float, gap: float = 1.0) -> bytes:
    """Répète `pcm`, séparé par `gap` seconde de silence, jusqu'à durer au moins `seconds`.

    `check_nemo.repeat_to` est figé à 16 kHz, alors que la synthèse de dictee rend du 24 kHz.
    """
    unit = pcm + silence(gap, rate)
    return unit * repeats(pcm, rate, seconds, gap)


def repeats(pcm: bytes, rate: int, seconds: float, gap: float = 1.0) -> int:
    """Le nombre de répétitions de `pcm` que `repeat_pcm` met bout à bout."""
    return math.ceil(seconds * rate * 2 / (len(pcm) + len(silence(gap, rate))))


def duration(pcm: bytes, rate: int = SPEECH_RATE) -> float:
    return len(pcm) / (rate * 2)


def setting(name: str, default: float) -> float:
    """Un réglage de dictee lu par `nemo_rt.env_value`, ou sa valeur par défaut quand le `.env` ne le fixe pas.

    La valeur n'est jamais affichée.
    """
    try:
        return float(nemo_rt.env_value(name))
    except RuntimeError:
        return default


@dataclass
class Result:
    """Réponse d'une transcription : statut, texte (vide hors 200), durée, horloge de fin."""

    status: int
    text: str
    seconds: float
    ended_at: float
    body: str = ""


@dataclass
class Speech:
    """Réponse de synthèse : octets, délai du premier octet, durée totale."""

    data: bytes
    first_byte_s: float
    total_s: float


class Sampler:
    """Relève la mémoire d'un conteneur toutes les 2 s avec `check_nemo.sample_memory`, en tâche de fond."""

    def __init__(self, container: str) -> None:
        self.samples: list[tuple[float, float]] = []
        self._stop = threading.Event()
        self.origin = time.monotonic()
        self._thread = threading.Thread(
            target=check_nemo.sample_memory, args=(container, self._stop, self.samples), daemon=True
        )
        self._thread.start()

    def peak(self, since: float | None = None) -> float | None:
        """Le pic en octets, depuis l'horloge monotone `since` (le début du script par défaut) ; None sans relevé."""
        offset = 0.0 if since is None else since - self.origin
        sizes = [size for at, size in list(self.samples) if at >= offset]
        return max(sizes) if sizes else None

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)


def cpu_percent(container: str) -> float:
    """Le CPU d'un conteneur en pourcents d'un cœur, d'après `docker stats` (une à deux secondes de mesure)."""
    out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", container], capture_output=True, text=True
    )
    expect(out.returncode == 0 and out.stdout.strip(), "docker stats ne rend pas le CPU")
    return float(out.stdout.strip().rstrip("%"))


def wait_idle(container: str, timeout: float = 90.0) -> float:
    """Attend que le CPU du conteneur retombe sous IDLE_CPU deux relevés de suite ; rend les secondes attendues.

    Échoue si le CPU ne retombe pas en `timeout` secondes : un contrôle qui part sur un nemo encore occupé mesurerait
    autre chose que ce qu'il dit.
    """
    started = time.monotonic()
    calm = 0
    last = 0.0
    while time.monotonic() - started < timeout:
        last = cpu_percent(container)
        calm = calm + 1 if last < IDLE_CPU else 0
        if calm == 2:
            return time.monotonic() - started
    raise Failure(f"nemo ne retombe pas sous {IDLE_CPU:.0f} % de CPU en {timeout:.0f} s (dernier relevé {last:.0f} %)")


def multipart(fields: dict[str, str], filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "dictee-integration-" + secrets.token_hex(8)
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields.items()
    )
    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode()
    return body + content + f"\r\n--{boundary}--\r\n".encode(), boundary


class RawRequest:
    """Une transcription sur un socket brut : on voit les octets arriver et on raccroche à volonté.

    Le constructeur envoie toute la requête (en-têtes, champs `fields` et audio). `started` est l'horloge du début de
    l'envoi, `sent_at` celle de la fin de l'envoi.
    """

    def __init__(self, fields: dict[str, str], wav: bytes, key: str) -> None:
        body, boundary = multipart(fields, "audio.wav", wav)
        head = (
            f"POST {TRANSCRIPTIONS} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nAuthorization: Bearer {key}\r\n"
            f"Content-Type: multipart/form-data; boundary={boundary}\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        self.buffer = b""
        self.sock = socket.create_connection((HOST, PORT), timeout=10)
        self.started = time.monotonic()
        # `sendall` a pour délai la durée totale de l'envoi : proportionné à la taille, pour qu'un envoi lent échoue.
        send_timeout = 10 + (len(head) + len(body)) / SEND_MIN_RATE
        self.sock.settimeout(send_timeout)
        try:
            self.sock.sendall(head + body)
        except (BrokenPipeError, ConnectionResetError):  # dictee a déjà répondu et fermé : la réponse est à lire
            pass
        except TimeoutError:
            self.sock.close()
            raise Failure(
                f"envoi de {len(body) / 1024**2:.0f} Mo non terminé en {send_timeout:.0f} s "
                f"(débit minimal {SEND_MIN_RATE / 1024**2:.0f} Mo/s)"
            ) from None
        self.sent_at = time.monotonic()

    def read_until(self, marker: bytes, timeout: float) -> bool:
        """Lit jusqu'à voir `marker` dans la réponse ; faux si `timeout` secondes passent ou si dictee ferme avant."""
        deadline = time.monotonic() + timeout
        while marker not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.sock.settimeout(remaining)
            try:
                data = self.sock.recv(65536)
            except (TimeoutError, OSError):
                return False
            if not data:
                return False
            self.buffer += data
        return True

    @property
    def status(self) -> int | None:
        match = re.match(rb"HTTP/1\.[01] (\d{3})", self.buffer)
        return int(match.group(1)) if match else None

    def close(self) -> None:
        self.sock.close()


@dataclass
class State:
    """Ce que les contrôles partagent : les clients, les conteneurs, l'audio de test et les mesures au repos.

    `dialogue` porte la marque du lancement : les contrôles qui l'envoient tel quel partagent ses tâches, et ceux qui
    veulent la leur prennent `fresh_dialogue`, `medium_wav` ou `long_wav`, marqués à chaque appel.
    """

    key: str
    http: httpx.Client
    ai: openai.OpenAI
    nemo: str
    dictee: str
    nemo_sampler: Sampler | None = None
    dictee_sampler: Sampler | None = None
    phrases: list[bytes] = field(default_factory=list)
    dialogue_pcm: bytes = b""
    dialogue: bytes = b""
    rest_text: str = ""
    rest_seconds: float = 0.0
    _long: bytes = field(default=b"", repr=False)

    def upload(self) -> tuple[str, bytes, str]:
        return ("dialogue.wav", self.dialogue, "audio/wav")

    @property
    def dialogue_words(self) -> list[str]:
        return words(check_nemo.PHRASE_1[0]) + words(check_nemo.PHRASE_2[0])

    def fresh_dialogue(self) -> bytes:
        """Le dialogue avec une marque neuve : son envoi crée une tâche à lui."""
        return nemo_rt.wav_file(mark(self.dialogue_pcm), SPEECH_RATE)

    def medium_wav(self) -> bytes:
        """Le dialogue répété sur MEDIUM_SECONDS, sous le seuil des audios longs, avec une marque neuve."""
        return nemo_rt.wav_file(mark(repeat_pcm(self.dialogue_pcm, SPEECH_RATE, MEDIUM_SECONDS)), SPEECH_RATE)

    @property
    def medium_words(self) -> list[str]:
        return self.dialogue_words * repeats(self.dialogue_pcm, SPEECH_RATE, MEDIUM_SECONDS)

    def long_wav(self) -> bytes:
        """Le dialogue répété sur LONG_SECONDS, assez long pour passer par la place des transcriptions longues.

        Chaque appel rend une marque neuve : deux audios longs ne rejoignent jamais la même tâche.
        """
        if not self._long:
            self._long = repeat_pcm(self.dialogue_pcm, SPEECH_RATE, LONG_SECONDS)
        return nemo_rt.wav_file(mark(self._long), SPEECH_RATE)

    def speak(self, text: str, voice: str, fmt: str, speed: float | None = None) -> Speech:
        body: dict[str, object] = {"input": text, "voice": voice, "response_format": fmt}
        if speed is not None:
            body["speed"] = speed
        started = time.monotonic()
        first: float | None = None
        chunks = []
        with self.http.stream("POST", "/v1/audio/speech", json=body) as response:
            if response.status_code != 200:
                response.read()
                raise Failure(f"synthèse {fmt} : HTTP {response.status_code} {response.text[:200]}")
            for chunk in response.iter_bytes():
                if first is None:
                    first = time.monotonic() - started
                chunks.append(chunk)
        expect(first is not None, f"synthèse {fmt} : réponse vide")
        return Speech(b"".join(chunks), first or 0.0, time.monotonic() - started)

    def transcribe(self, model: str, response_format: str = "json", wav: bytes | None = None) -> httpx.Response:
        data = {"model": model, "response_format": response_format}
        return self.http.post(
            TRANSCRIPTIONS, data=data, files={"file": ("audio.wav", wav or self.dialogue, "audio/wav")}
        )

    def transcribe_result(self, wav: bytes, model: str = "dictee") -> Result:
        started = time.monotonic()
        response = self.transcribe(model, wav=wav)
        ended = time.monotonic()
        if response.status_code != 200:
            return Result(response.status_code, "", ended - started, ended, response.text[:200])
        return Result(200, response.json().get("text", ""), ended - started, ended)

    def background(self, wav: bytes, model: str = "dictee", response_format: str = "json") -> httpx.Response:
        """Lance une tâche avec `background=true` ; la réponse porte l'objet de la tâche, ou l'erreur d'un refus."""
        data = {"model": model, "response_format": response_format, "background": "true"}
        return self.http.post(TRANSCRIPTIONS, data=data, files={"file": ("audio.wav", wav, "audio/wav")})

    def created_task(self, wav: bytes, model: str = "dictee", response_format: str = "json") -> dict:
        """L'objet d'une tâche lancée en arrière-plan, qui doit être acceptée."""
        response = self.background(wav, model, response_format)
        expect(response.status_code == 200, f"background=true : HTTP {response.status_code} {response.text[:200]}")
        task = response.json()
        expect(
            task.get("object") == "audio.transcription.task" and str(task.get("id", "")).startswith("trn_"),
            f"background=true ne rend pas un objet tâche : {sorted(task)}",
        )
        return task

    def task(self, task_id: str) -> dict:
        """L'objet de la tâche, lu par `GET /v1/audio/transcriptions/{id}`."""
        response = self.http.get(f"{TRANSCRIPTIONS}/{task_id}", timeout=30)
        expect(response.status_code == 200, f"GET {task_id} : HTTP {response.status_code} {response.text[:200]}")
        return response.json()

    def wait_task(self, task_id: str, timeout: float = TASK_TIMEOUT) -> tuple[dict, list[float]]:
        """Lit la tâche toutes les POLL_INTERVAL secondes jusqu'à sa fin ; rend son objet et les avancements lus."""
        deadline = time.monotonic() + timeout
        progress: list[float] = []
        while True:
            task = self.task(task_id)
            progress.append(task["progress"])
            if task["status"] in TERMINAL:
                return task, progress
            expect(
                time.monotonic() < deadline,
                f"la tâche {task_id} n'a pas fini en {timeout:.0f} s ({task['status']}, avancement {task['progress']})",
            )
            time.sleep(POLL_INTERVAL)

    def cancel_unfinished(self, task_ids: list[str]) -> None:
        """Annule celles des tâches qui ne sont pas finies : le ménage d'un contrôle, quelle que soit son issue."""
        for task_id in task_ids:
            try:
                if self.task(task_id)["status"] not in TERMINAL:
                    self.http.post(f"{TRANSCRIPTIONS}/{task_id}/cancel", timeout=30)
            except (Failure, httpx.HTTPError):
                note(f"ménage : la tâche {task_id} n'a pas pu être relue ni annulée")

    def dictee_lines(self) -> list[str]:
        """Les lignes du journal `dictee` du conteneur, dans l'ordre, sans celles d'uvicorn.

        Un `compose restart` garde le même conteneur : son journal garde aussi les lignes d'avant le redémarrage.
        """
        out = subprocess.run(["docker", "logs", self.dictee], capture_output=True, text=True)
        expect(out.returncode == 0, "docker logs ne rend pas le journal de dictee")
        return [line for line in (out.stdout + out.stderr).splitlines() if " INFO dictee " in line]

    def task_line(self, task_id: str, after: int = 0) -> str:
        """La ligne de fin de la tâche, que le runner écrit juste après sa fin ; attendue LOG_TIMEOUT secondes au plus.

        Une tâche n'en a qu'une : le contrôle échoue s'il en trouve plusieurs.
        """
        deadline = time.monotonic() + LOG_TIMEOUT
        while True:
            lines = task_lines(self.dictee_lines()[after:], task_id)
            expect(len(lines) <= 1, f"{len(lines)} lignes de fin pour la tâche {task_id} au lieu d'une")
            if lines:
                return lines[0]
            expect(time.monotonic() < deadline, f"aucune ligne de fin pour la tâche {task_id} en {LOG_TIMEOUT:.0f} s")
            time.sleep(POLL_INTERVAL)

    def task_folders(self) -> set[str]:
        """Les dossiers de tâche présents dans le conteneur dictee (aucun quand le dossier des tâches n'existe pas)."""
        out = subprocess.run(["docker", "exec", self.dictee, "ls", "-1", TASKS_DIR], capture_output=True, text=True)
        return set(out.stdout.split()) if out.returncode == 0 else set()

    def expect_folders_gone(self, task_ids: list[str]) -> None:
        """Vérifie que les dossiers de ces tâches finies ont disparu, attendus LOG_TIMEOUT secondes au plus."""
        deadline = time.monotonic() + LOG_TIMEOUT
        while left := self.task_folders() & set(task_ids):
            expect(time.monotonic() < deadline, f"dossiers de tâche encore présents : {sorted(left)}")
            time.sleep(POLL_INTERVAL)


def field_of(line: str, name: str) -> str | None:
    """La valeur du champ `name=` d'une ligne de journal, ou None."""
    match = re.search(rf"(?:^|\s){name}=(\S+)", line)
    return match.group(1) if match else None


def task_lines(lines: list[str], task_id: str) -> list[str]:
    """Les lignes de fin de la tâche `task_id`, celles que le runner écrit (`task=<id> user=…`)."""
    return [line for line in lines if f" task={task_id} user=" in line]


def request_lines(lines: list[str]) -> list[str]:
    """Les lignes des requêtes de transcription, une par requête, qui finissent par la tâche servie."""
    return [line for line in lines if "route=transcriptions" in line]


def declared_volumes() -> list[str]:
    """Les volumes nommés de `compose.yaml`, dans l'ordre du fichier (`config` rend les clés triées)."""
    names: list[str] = []
    inside = False
    for line in (ROOT / "compose.yaml").read_text().splitlines():
        if re.match(r"^[^\s#]", line):
            inside = line.startswith("volumes:")
        elif inside and (match := re.match(r"^  ([\w.-]+):", line)):
            names.append(match.group(1))
    return names


def memory_limit(service: dict) -> int | None:
    limit = service.get("deploy", {}).get("resources", {}).get("limits", {}).get("memory", service.get("mem_limit"))
    return None if limit is None else int(limit)


def check_compose(state: State) -> str:
    command = [*check_nemo.compose_base(), "-f", "compose.yaml", "config", "--no-env-resolution", "--format", "json"]
    done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    expect(done.returncode == 0, f"`config` a échoué : {done.stderr.strip()[:300]}")
    config = json.loads(done.stdout)
    services = config.get("services", {})
    expect(set(services) == set(COMPOSE_RULES), f"services {sorted(services)} au lieu de {sorted(COMPOSE_RULES)}")
    problems: list[str] = []
    for name, (restart, memory) in COMPOSE_RULES.items():
        service = services[name]
        if service.get("restart") != restart:
            problems.append(f"{name} : restart {service.get('restart')!r} au lieu de {restart!r}")
        entries = service.get("env_file", [])
        env_files = [Path(entry["path"] if isinstance(entry, dict) else entry).name for entry in entries]
        if env_files != [".env"]:
            problems.append(f"{name} : env_file {env_files} au lieu de ['.env']")
        if "ports" in service:
            problems.append(f"{name} : publie des ports")
        if memory_limit(service) != memory:
            problems.append(f"{name} : limite de mémoire {memory_limit(service)} octets au lieu de {memory}")
    versions: dict[str, object] = {}
    vague = []
    for name in ("models", "nemo"):
        versions[name] = services[name].get("build", {}).get("args", {}).get("NEMO_VERSION")
        if not isinstance(versions[name], str) or not re.fullmatch(EXPLICIT_VERSION, versions[name]):
            vague.append(name)
            problems.append(f"{name} : NEMO_VERSION {versions[name]!r} n'est pas une version explicite (x.y.z)")
    if not vague and len(set(versions.values())) > 1:
        problems.append(f"NEMO_VERSION différente entre models et nemo : {versions}")
    info("NEMO_VERSION lue dans compose.yaml : " + ", ".join(f"{name} {version}" for name, version in versions.items()))
    volumes = declared_volumes()
    if volumes != sorted(volumes):
        problems.append(f"volumes {volumes} pas dans l'ordre alphabétique")
    if set(volumes) != set(config.get("volumes", {})):
        problems.append(f"volumes lus dans le fichier {volumes} différents de ceux de `config`")
    expect(not problems, " ; ".join(problems))
    return (
        f"{len(services)} services, restart, env_file, ports, mémoire, même version explicite de NeMo et volumes "
        "conformes"
    )


def check_health(state: State) -> str:
    response = state.http.get("/health", timeout=10)
    expect(response.status_code == 200, f"HTTP {response.status_code}")
    return "200"


def check_synthesis(state: State) -> str:
    """Synthétise les deux phrases de la tâche 1 en wav, puis assemble le dialogue avec une seconde de silence.

    Le dialogue porte la marque de ce lancement : ses transcriptions ne rejoignent pas celles d'un lancement précédent.
    """
    parts: list[str] = []
    for index, (text, voice) in enumerate((check_nemo.PHRASE_1, check_nemo.PHRASE_2), start=1):
        speech = state.speak(text, voice, "wav")
        expect(speech.data[:4] == b"RIFF", f"phrase {index} ({voice}) : la réponse ne commence pas par RIFF")
        try:
            rate, pcm = nemo_rt.parse_wav(speech.data)
        except ValueError as error:
            raise Failure(f"phrase {index} ({voice}) : WAV illisible, {error}") from None
        expect(rate == SPEECH_RATE, f"phrase {index} ({voice}) : {rate} Hz au lieu de {SPEECH_RATE} Hz")
        expect(duration(pcm) > 1.0, f"phrase {index} ({voice}) : {duration(pcm):.1f} s d'audio seulement")
        state.phrases.append(pcm)
        parts.append(f"{voice} {duration(pcm):.1f} s d'audio, RIFF à {rate} Hz")
        if index == 1:
            info(
                f"première phrase : premier octet après {speech.first_byte_s:.1f} s, réponse entière en "
                f"{speech.total_s:.1f} s (critère de la spec : moins de 2 s sur le VPS, ici une indication)"
            )
    state.dialogue_pcm = state.phrases[0] + silence(1.0) + state.phrases[1]
    state.dialogue = state.fresh_dialogue()
    (WORK / "dictee-dialogue.wav").write_bytes(state.dialogue)
    return ", ".join(parts) + f", dialogue de {duration(state.dialogue_pcm):.1f} s"


def check_transcription(state: State) -> str:
    started = time.monotonic()
    result = state.ai.audio.transcriptions.create(model="dictee", file=state.upload(), response_format="json")
    state.rest_seconds = time.monotonic() - started
    state.rest_text = result.text
    note(f"transcription : {result.text}")
    expected = state.dialogue_words
    found, ratio = found_ratio(expected, result.text)
    detail = f"{found} mots attendus sur {len(expected)} retrouvés ({ratio:.0%}, minimum {MIN_WORDS_FOUND:.0%})"
    expect(ratio >= MIN_WORDS_FOUND, detail)
    return f"{detail}, {state.rest_seconds:.1f} s"


def check_diarize(state: State) -> str:
    """Les personnes d'un dialogue, données par la session temps réel de NeMo, dans trois formats.

    Les trois requêtes envoient le même audio en mode réunion : la première crée la tâche, les deux autres la
    rejoignent et reçoivent son résultat dans leur format. Le journal le confirme : une seule ligne de fin de tâche, en
    mode `diarize` et sans morceaux (`chunks=-`), car une réunion passe en une seule session de NeMo, qui marque les
    personnes pendant qu'elle transcrit.
    """
    wav = state.fresh_dialogue()
    before = len(state.dictee_lines())
    response = state.transcribe("dictee-diarize", "diarized_json", wav)
    expect(response.status_code == 200, f"diarized_json : HTTP {response.status_code} {response.text[:200]}")
    segments = response.json().get("segments", [])
    speakers = {segment["speaker"] for segment in segments}
    expect(speakers == {"A", "B"}, f"locuteurs {sorted(speakers)} sur {len(segments)} segments au lieu de ['A', 'B']")
    vtt = state.transcribe("dictee-diarize", "vtt", wav)
    expect(vtt.status_code == 200, f"vtt : HTTP {vtt.status_code} {vtt.text[:200]}")
    expect("<v A>" in vtt.text and "<v B>" in vtt.text, "le VTT ne porte pas les balises <v A> et <v B>")
    srt = state.transcribe("dictee-diarize", "srt", wav)
    expect(srt.status_code == 200, f"srt : HTTP {srt.status_code} {srt.text[:200]}")
    expect(srt.text.startswith("1\n"), f"le SRT commence par {srt.text[:10]!r} au lieu de '1\\n'")
    requests = request_lines(state.dictee_lines()[before:])
    served = [field_of(line, "task") for line in requests]
    expect(
        len(served) == 3 and len(set(served)) == 1 and served[0] != "-",
        f"les trois requêtes ont servi les tâches {served} au lieu d'une seule",
    )
    line = state.task_line(served[0], before)
    expect(
        field_of(line, "mode") == "diarize" and field_of(line, "chunks") == "-",
        f"la tâche porte mode={field_of(line, 'mode')} chunks={field_of(line, 'chunks')} au lieu de "
        "mode=diarize chunks=- (une seule session de NeMo)",
    )
    return (
        f"{len(segments)} segments, locuteurs A et B, VTT avec <v A> et <v B>, SRT qui commence par 1 ; une seule "
        "tâche pour les trois formats, en une session de NeMo (chunks=-)"
    )


@dataclass
class Streamed:
    """Ce qu'un flux `stream=true` a rendu : les deltas, le texte de `done` et les délais depuis la requête."""

    deltas: list[str]
    done: str | None
    first_delta: float | None
    seconds: float

    @property
    def joined(self) -> str:
        return "".join(self.deltas)


def stream_events(state: State, upload: tuple[str, bytes, str]) -> Streamed:
    """Lit un flux `stream=true` jusqu'au bout avec le SDK d'OpenAI, en notant le délai du premier delta."""
    started = time.monotonic()
    stream = state.ai.audio.transcriptions.create(model="dictee", file=upload, response_format="json", stream=True)
    deltas: list[str] = []
    done: str | None = None
    first: float | None = None
    for event in stream:
        if event.type == "transcript.text.delta":
            if first is None:
                first = time.monotonic() - started
            deltas.append(event.delta)
        elif event.type == "transcript.text.done":
            done = event.text
    return Streamed(deltas, done, first, time.monotonic() - started)


def check_stream(state: State) -> str:
    streamed = stream_events(state, state.upload())
    expect(streamed.done is not None, "aucun événement transcript.text.done")
    expect(streamed.first_delta is not None, "aucun événement transcript.text.delta")
    note(f"deltas : {streamed.deltas}")
    # NeMo émet un delta tous les 1,12 s d'audio : le dialogue en donne plusieurs si dictee les relaie.
    expect(len(streamed.deltas) > 1, f"{len(streamed.deltas)} delta seulement : dictee ne relaie pas ceux de NeMo")
    expect(
        streamed.joined == streamed.done,
        f"les deltas mis bout à bout {streamed.joined!r} diffèrent du texte final {streamed.done!r}",
    )
    return (
        f"{len(streamed.deltas)} deltas dont la concaténation égale le texte de transcript.text.done ; premier delta "
        f"après {streamed.first_delta:.1f} s, fin après {streamed.seconds:.1f} s "
        f"({state.rest_seconds:.1f} s sans flux)"
    )


def check_clean(state: State) -> str:
    result = state.transcribe_result(state.dialogue, "dictee-clean")
    expect(result.status == 200, f"HTTP {result.status} {result.body}")
    expect(result.text == state.rest_text, f"{result.text!r} au lieu du texte de `dictee` {state.rest_text!r}")
    return f"200 et le même texte que dictee, en {result.seconds:.1f} s"


def check_formats(state: State) -> str:
    text, voice = check_nemo.PHRASE_2
    for fmt, magics in SIGNATURES.items():
        data = state.speak(text, voice, fmt).data
        expect(data.startswith(magics), f"{fmt} : commence par {data[:4]!r}, attendu l'un de {magics}")
    # Deux phrases pour la comparaison des vitesses : la durée de Magpie varie d'une synthèse à l'autre (de 3 à 7 %
    # sur une phrase, mesuré), et la comparaison met deux synthèses face à face.
    both = f"{check_nemo.PHRASE_1[0]} {text}"
    normal = state.speak(both, voice, "pcm").data
    fast = state.speak(both, voice, "pcm", speed=2.0).data
    expect(len(normal) > 0 and len(normal) % 2 == 0, f"pcm : {len(normal)} octets")
    half = len(normal) / 2
    gap = abs(len(fast) - half) / half
    detail = (
        f"à vitesse 2 le pcm fait {len(fast)} octets pour {half:.0f} attendus "
        f"({gap:.1%} d'écart, tolérance {SPEED_TOLERANCE:.0%})"
    )
    expect(gap <= SPEED_TOLERANCE, detail)
    return f"signatures mp3, opus, aac et flac conformes, pcm de {len(normal)} octets ; {detail}"


def check_background(state: State) -> str:
    """Une tâche lancée avec `background=true`, lue par `GET` jusqu'à `completed`, puis son `output` en SRT.

    La réponse arrive tout de suite avec l'objet de la tâche, encore en file ou en cours. Une fois la tâche finie,
    `output` est la réponse dans le format demandé à la création (une chaîne pour `srt`), l'avancement vaut 1, le
    résultat expire `TASK_RETENTION_HOURS` après la fin, et le dossier de son audio a disparu.
    """
    created = state.created_task(state.fresh_dialogue(), response_format="srt")
    task_id = created["id"]
    try:
        expect(
            created["status"] in ("queued", "in_progress") and created["output"] is None,
            f"la réponse de background=true porte le statut {created['status']} et un output {created['output']!r}",
        )
        task, progress = state.wait_task(task_id)
    finally:
        state.cancel_unfinished([task_id])
    expect(task["status"] == "completed", f"la tâche finit en {task['status']} : {task['error']}")
    output = task["output"]
    expect(isinstance(output, str) and output.startswith("1\n"), f"output en SRT attendu, reçu {str(output)[:40]!r}")
    expected = state.dialogue_words
    found, ratio = found_ratio(expected, output)
    detail = f"{found} mots attendus sur {len(expected)} retrouvés ({ratio:.0%}, minimum {MIN_WORDS_FOUND:.0%})"
    expect(ratio >= MIN_WORDS_FOUND, detail)
    expect(task["progress"] == 1, f"avancement final {task['progress']} au lieu de 1")
    retention = setting("TASK_RETENTION_HOURS", 24) * 3600
    kept = task["expires_at"] - task["completed_at"]
    expect(kept == retention, f"le résultat expire {kept} s après la fin au lieu de {retention:.0f} s")
    state.expect_folders_gone([task_id])
    return (
        f"réponse immédiate en {created['status']}, {len(progress)} lectures par GET jusqu'à completed, output en SRT "
        f"({detail}), résultat gardé {kept / 3600:.0f} h, dossier de l'audio effacé"
    )


class LiveEvents:
    """Les événements d'une connexion de dictée en direct, lus en tâche de fond pour les attendre avec un délai.

    Le client d'OpenAI n'a pas de délai de lecture : un fil lit les messages bruts et les range dans une file, avec
    l'instant de leur arrivée. `seen` garde tous les événements reçus, dans l'ordre.
    """

    def __init__(self, connection) -> None:
        self._queue: queue.Queue[tuple[float, dict | None]] = queue.Queue()
        self.seen: list[tuple[float, dict]] = []
        self._thread = threading.Thread(target=self._read, args=(connection,), daemon=True)
        self._thread.start()

    def _read(self, connection) -> None:
        try:
            while True:
                event = json.loads(connection.recv_bytes())  # l'instant se lit après l'arrivée, pas avant l'attente
                self._queue.put((time.monotonic(), event))
        except Exception:  # la connexion est fermée : la fin se signale par None
            self._queue.put((time.monotonic(), None))

    def wait(self, kind: str, timeout: float) -> tuple[float, dict] | None:
        """Attend un événement de type `kind` ; rend son instant et son contenu, ou None après `timeout` secondes.

        Un événement `error` ou la fermeture de la connexion font échouer le contrôle.
        """
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                at, event = self._queue.get(timeout=remaining)
            except queue.Empty:
                return None
            if event is None:
                raise Failure(f"connexion fermée avant {kind} ; reçus : {self.kinds()}")
            self.seen.append((at, event))
            if event.get("type") == "error":
                raise Failure(f"événement error avant {kind} : {event.get('error')}")
            if event.get("type") == kind:
                return at, event
        return None

    def kinds(self, since: float = 0.0) -> list[str]:
        return [event.get("type", "?") for at, event in self.seen if at >= since]


def check_live(state: State) -> str:
    """Une dictée en direct avec le client Python d'OpenAI, sur la première phrase Magpie à 24 kHz.

    La session se règle sans `turn_detection`, puis reçoit une seconde d'audio que le client vide par
    `input_audio_buffer.clear` : dictee relaie le `clear` à NeMo et n'envoie `input_audio_buffer.cleared` qu'à sa
    réponse, attendue CLEARED_TIMEOUT secondes au plus. La phrase entière part ensuite par trames, puis un commit ; le
    texte de `…transcription.completed` doit contenir au moins MIN_WORDS_FOUND de ses mots.
    """
    text, _ = check_nemo.PHRASE_1
    pcm = state.phrases[0]
    session = {
        "type": "transcription",
        "audio": {"input": {"format": {"type": "audio/pcm", "rate": SPEECH_RATE}, "turn_detection": None}},
    }
    with state.ai.realtime.connect(extra_query={"intent": "transcription"}) as connection:
        events = LiveEvents(connection)
        expect(events.wait("session.created", 10), f"pas de session.created ; reçus : {events.kinds()}")
        connection.session.update(session=session)
        expect(events.wait("session.updated", 10), f"pas de session.updated ; reçus : {events.kinds()}")

        head = pcm[: int(SPEECH_RATE * CLEARED_AUDIO_SECONDS) * 2]
        connection.input_audio_buffer.append(audio=base64.b64encode(head).decode())
        sent_clear = time.monotonic()
        connection.input_audio_buffer.clear()
        cleared = events.wait("input_audio_buffer.cleared", CLEARED_TIMEOUT)
        expect(
            cleared,
            f"pas de input_audio_buffer.cleared {CLEARED_TIMEOUT:.0f} s après clear ; reçus depuis clear : "
            f"{events.kinds(sent_clear)}",
        )
        clear_ms = (cleared[0] - sent_clear) * 1000
        note(
            f"clear : cleared {clear_ms:.0f} ms après l'envoi, événements reçus depuis clear {events.kinds(sent_clear)}"
        )

        frame = int(SPEECH_RATE * LIVE_FRAME_SECONDS) * 2
        sent_audio = time.monotonic()
        for start in range(0, len(pcm), frame):
            connection.input_audio_buffer.append(audio=base64.b64encode(pcm[start : start + frame]).decode())
        connection.input_audio_buffer.commit()
        completed = events.wait(LIVE_COMPLETED, COMPLETED_TIMEOUT)
        expect(completed, f"pas de {LIVE_COMPLETED} en {COMPLETED_TIMEOUT:.0f} s ; reçus : {events.kinds(sent_audio)}")
    transcript = completed[1].get("transcript", "")
    note(f"dictée en direct : {transcript}")
    deltas = events.kinds(sent_audio).count(LIVE_DELTA)
    expected = words(text)
    found, ratio = found_ratio(expected, transcript)
    detail = f"{found} mots attendus sur {len(expected)} retrouvés ({ratio:.0%}, minimum {MIN_WORDS_FOUND:.0%})"
    expect(ratio >= MIN_WORDS_FOUND, detail)
    return (
        f"clear confirmé par cleared en {clear_ms:.0f} ms ; {deltas} deltas puis le texte final {completed[0] - sent_audio:.1f} s "
        f"après le début de l'audio, {detail}"
    )


def check_concurrency(state: State) -> str:
    long_wav = state.long_wav()
    short_seconds = duration(state.dialogue_pcm)
    note(f"audio long : {LONG_SECONDS / 60:.0f} min, audio court : {short_seconds:.1f} s")
    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        long_future = pool.submit(state.transcribe_result, long_wav)
        time.sleep(LONG_HEADSTART)
        short = state.transcribe_result(state.fresh_dialogue())
        long_still_running = not long_future.done()
        long = long_future.result()
    expect(short.status == 200 and short.text, f"la courte : HTTP {short.status} {short.body}")
    expect(long.status == 200 and long.text, f"la longue : HTTP {long.status} {long.body}")
    expect(long_still_running, "la longue était déjà finie quand la courte a répondu")
    expect(short.ended_at < long.ended_at, "la courte a répondu après la longue")
    peak = state.nemo_sampler.peak(began) if state.nemo_sampler else None
    if peak is None:
        raise Failure("aucun relevé de mémoire de nemo pendant la concurrence")
    memory = f"pic de nemo {peak / 1024**3:.2f} Gio (limite {NEMO_MEMORY_LIMIT / 1024**3:.0f} Gio)"
    expect(peak < NEMO_MEMORY_LIMIT, memory)
    return (
        f"la courte répond en {short.seconds:.1f} s, avant la longue qui finit à {long.ended_at - began:.1f} s ; "
        + memory
    )


def hang_up_sync(state: State, wav: bytes) -> tuple[str, RawRequest, int]:
    """Envoie une transcription synchrone, raccroche après CUT_AFTER secondes et rend la tâche qu'elle a servie.

    La réponse ne doit pas être arrivée avant la coupure : sinon le contrôle ne raccroche pas en plein travail. La
    tâche se lit dans la ligne de la requête, que dictee écrit quand il voit le client parti (statut 499). Rend aussi
    la requête et le nombre de lignes du journal avant l'envoi.
    """
    before = len(state.dictee_lines())
    request = RawRequest({"model": "dictee", "response_format": "json"}, wav, state.key)
    try:
        answered = request.read_until(b"\r\n\r\n", CUT_AFTER - (time.monotonic() - request.started))
    finally:
        request.close()
    expect(not answered, f"la réponse (HTTP {request.status}) est arrivée avant la coupure à {CUT_AFTER:.0f} s")
    deadline = time.monotonic() + LOG_TIMEOUT
    while not (lines := request_lines(state.dictee_lines()[before:])):
        expect(time.monotonic() < deadline, f"aucune ligne de requête {LOG_TIMEOUT:.0f} s après la coupure")
        time.sleep(POLL_INTERVAL)
    line = lines[0]
    task_id = field_of(line, "task") or "-"
    expect(
        field_of(line, "status") == "499", f"la requête coupée est journalisée avec status={field_of(line, 'status')}"
    )
    expect(task_id.startswith("trn_"), f"la requête coupée n'a servi aucune tâche (task={task_id})")
    return task_id, request, before


def check_rejoin(state: State) -> str:
    """Une requête synchrone coupée après CUT_AFTER secondes, puis le même fichier renvoyé : la réponse arrive.

    Le second envoi rejoint la tâche en cours au lieu d'en créer une : le journal n'a qu'une tâche pour les deux
    requêtes, la coupée (499) et la seconde (200), et une seule ligne de fin pour elle.
    """
    wav = state.medium_wav()
    task_id, _, before = hang_up_sync(state, wav)
    try:
        result = state.transcribe_result(wav)
    finally:
        state.cancel_unfinished([task_id])
    expect(result.status == 200, f"le second envoi : HTTP {result.status} {result.body}")
    expected = state.medium_words
    found, ratio = found_ratio(expected, result.text)
    detail = f"{found} mots attendus sur {len(expected)} retrouvés ({ratio:.0%}, minimum {MIN_WORDS_FOUND:.0%})"
    expect(ratio >= MIN_WORDS_FOUND, detail)
    lines = state.dictee_lines()[before:]
    served = [(field_of(line, "status"), field_of(line, "task")) for line in request_lines(lines)]
    expect(
        served == [("499", task_id), ("200", task_id)],
        f"requêtes journalisées {served} au lieu de la coupée puis la seconde, sur la tâche {task_id}",
    )
    line = state.task_line(task_id, before)
    expect(field_of(line, "status") == "completed", f"la tâche finit en {field_of(line, 'status')}")
    state.expect_folders_gone([task_id])
    return (
        f"coupée à {CUT_AFTER:.0f} s (499), le second envoi répond 200 en {result.seconds:.1f} s ({detail}) ; une seule "
        f"tâche ({task_id}) et une seule ligne de fin pour les deux requêtes"
    )


def check_hangup(state: State) -> str:
    """Une requête synchrone coupée en plein travail : sa tâche continue, et son résultat se relit par `GET`.

    Juste après la coupure, la tâche est encore en file ou en cours ; elle finit ensuite `completed`, avec un texte
    qui contient au moins MIN_WORDS_FOUND des mots de l'audio, et une ligne de fin à elle.
    """
    wav = state.medium_wav()
    task_id, request, before = hang_up_sync(state, wav)
    try:
        after_cut = state.task(task_id)
        task, _ = state.wait_task(task_id)
    finally:
        state.cancel_unfinished([task_id])
    expect(
        after_cut["status"] in ("queued", "in_progress") and after_cut["progress"] < 1,
        f"juste après la coupure, la tâche est {after_cut['status']} à {after_cut['progress']} : rien ne prouve qu'elle "
        "a continué",
    )
    expect(task["status"] == "completed", f"la tâche finit en {task['status']} : {task['error']}")
    expected = state.medium_words
    found, ratio = found_ratio(expected, task["output"]["text"])
    detail = f"{found} mots attendus sur {len(expected)} retrouvés ({ratio:.0%}, minimum {MIN_WORDS_FOUND:.0%})"
    expect(ratio >= MIN_WORDS_FOUND, detail)
    line = state.task_line(task_id, before)
    expect(field_of(line, "status") == "completed", f"la ligne de fin porte status={field_of(line, 'status')}")
    state.expect_folders_gone([task_id])
    return (
        f"coupée à {CUT_AFTER:.0f} s, la tâche était {after_cut['status']} à {after_cut['progress']:.2f} et finit "
        f"completed {time.monotonic() - request.started:.1f} s après l'envoi ; résultat relu par GET ({detail})"
    )


def check_stream_hangup(state: State) -> str:
    """Raccroche au premier delta d'un flux long, puis vérifie que nemo s'arrête et qu'une courte n'en souffre pas.

    dictee relaie les deltas de NeMo : le premier arrive peu après l'envoi, bien avant la fin de la session, quand
    NeMo est en plein travail sur les 11 minutes d'audio. Le contrôle échoue si ce premier delta n'arrive pas en
    FIRST_DELTA_TIMEOUT secondes : le flux ne progresserait pas. Le socket se ferme dès ce delta, avant tout relevé
    de CPU : `docker stats` met une à deux secondes à répondre, et le CPU n'est lu qu'après le raccroché. Que nemo
    travaillait à ce moment-là, le delta le dit (NeMo n'en émet que pendant la session), et le flux n'avait pas fini.
    Un flux n'est pas enregistré : rien ne continue après le raccroché, à la différence d'une requête synchrone.
    """
    wait_idle(state.nemo)
    stream = RawRequest({"model": "dictee", "stream": "true"}, state.long_wav(), state.key)
    try:
        got_delta = stream.read_until(DELTA, FIRST_DELTA_TIMEOUT)
        first_delta = time.monotonic()
        status = stream.status
        finished = b"transcript.text.done" in stream.buffer
    finally:
        stream.close()
    hung_up = time.monotonic()
    expect(
        got_delta,
        f"aucun delta dans les {FIRST_DELTA_TIMEOUT:.0f} s qui suivent l'envoi de l'audio (HTTP {status}) : "
        "le flux ne progresse pas",
    )
    expect(status == 200, f"le flux a répondu HTTP {status}")
    expect(not finished, "le flux avait déjà fini au premier delta : le contrôle ne raccroche pas en plein travail")
    delay = f"premier delta {first_delta - stream.sent_at:.1f} s après la fin de l'envoi"
    info(
        f"audio long de {LONG_SECONDS / 60:.0f} min : {delay} ({first_delta - stream.started:.1f} s après le début), "
        f"socket fermé {(hung_up - first_delta) * 1000:.0f} ms après"
    )
    cpu: list[float] = []
    while time.monotonic() - hung_up < HANGUP_WINDOW:
        cpu.append(cpu_percent(state.nemo))
    # `docker stats` donne le CPU de la seconde qui précède chaque relevé : le premier couvre l'instant du raccroché.
    info(
        f"CPU de nemo après le raccroché, pendant {HANGUP_WINDOW:.0f} s : {[round(value) for value in cpu]} % "
        "(le premier couvre l'instant du raccroché)"
    )
    # Sur cette machine la courte reste sous la limite ci-dessous même quand NeMo traite encore une longue (4 s contre
    # 2 s au repos, mesuré en concurrence) : seul le CPU prouve que la session abandonnée est fermée.
    expect(cpu[-1] < BUSY_CPU, f"nemo encore à {cpu[-1]:.0f} % de CPU {HANGUP_WINDOW:.0f} s après le raccroché")
    short = state.transcribe_result(state.fresh_dialogue())
    expect(short.status == 200 and short.text, f"la courte après le raccroché : HTTP {short.status} {short.body}")
    limit = 2 * state.rest_seconds + 5
    detail = (
        f"la courte prend {short.seconds:.1f} s après le raccroché, "
        f"limite {limit:.1f} s (2 x {state.rest_seconds:.1f} s au repos + 5 s)"
    )
    expect(short.seconds < limit, detail)
    return f"raccroché au premier delta ({delay}) ; {detail}"


def check_long_stream(state: State) -> str:
    """Lit un flux de l'audio long jusqu'au bout : plus d'un delta, et leur concaténation égale le texte de `done`.

    Une seule phrase finale couvre les 11 minutes : c'est le cas où le texte relayé est le plus long avant son `Final`.
    Un flux n'est pas enregistré : sa ligne de requête ne sert aucune tâche, et aucune ligne de fin de tâche n'apparaît.
    """
    wait_idle(state.nemo)
    before = len(state.dictee_lines())
    streamed = stream_events(state, ("long.wav", state.long_wav(), "audio/wav"))
    expect(streamed.done is not None, "aucun événement transcript.text.done")
    expect(streamed.first_delta is not None, "aucun événement transcript.text.delta")
    expect(len(streamed.deltas) > 1, f"{len(streamed.deltas)} delta seulement : dictee ne relaie pas ceux de NeMo")
    joined, done = streamed.joined, streamed.done
    if joined != done:
        common = next((i for i, (a, b) in enumerate(zip(joined, done)) if a != b), min(len(joined), len(done)))
        raise Failure(
            f"les deltas mis bout à bout diffèrent du texte de done à partir du caractère {common} "
            f"(deltas {len(joined)} caractères, done {len(done)}) : deltas {joined[common : common + 40]!r}, "
            f"done {done[common : common + 40]!r}"
        )
    deadline = time.monotonic() + LOG_TIMEOUT
    while not request_lines(state.dictee_lines()[before:]):
        expect(time.monotonic() < deadline, f"aucune ligne de requête {LOG_TIMEOUT:.0f} s après la fin du flux")
        time.sleep(POLL_INTERVAL)
    lines = state.dictee_lines()[before:]
    served = [field_of(line, "task") for line in request_lines(lines)]
    ended = [line for line in lines if re.search(r"\stask=\S+ user=", line)]
    expect(served == ["-"] and not ended, f"le flux a servi les tâches {served}, et {len(ended)} tâche(s) ont fini")
    return (
        f"{len(streamed.deltas)} deltas dont la concaténation égale le texte de done ; premier delta "
        f"{streamed.first_delta:.1f} s après le début de la requête (envoi compris), fin après {streamed.seconds:.1f} s ; "
        "aucune tâche enregistrée"
    )


def check_queued(state: State) -> str:
    """Une tâche longue en cours et `MAX_QUEUED_LONG` en attente : la suivante reçoit 429 `long_audio_busy`.

    Les tâches partent en arrière-plan, chacune avec son audio long marqué : la première passe en cours, les autres
    restent en file, puis l'envoi de trop est refusé tout de suite, sans tâche. Le contrôle annule ensuite les tâches
    qu'il a lancées et vérifie que leurs dossiers ont disparu.
    """
    wait_idle(state.nemo)
    waiting = int(setting("MAX_QUEUED_LONG", 3))
    task_ids: list[str] = []
    try:
        for _ in range(1 + waiting):
            task_ids.append(state.created_task(state.long_wav())["id"])
        refused = state.background(state.long_wav())
        statuses = [state.task(task_id)["status"] for task_id in task_ids]
        if refused.status_code == 200:  # acceptée à tort : elle est aussi à annuler
            task_ids.append(refused.json()["id"])
    finally:
        state.cancel_unfinished(task_ids)
    code = refused.json().get("error", {}).get("code") if refused.status_code != 200 else None
    expect(
        refused.status_code == 429 and code == "long_audio_busy",
        f"la tâche longue n° {2 + waiting} reçoit HTTP {refused.status_code} (code {code}) au lieu de 429 long_audio_busy",
    )
    expect(
        statuses == ["in_progress"] + ["queued"] * waiting,
        f"statuts {statuses} au lieu d'une tâche en cours et {waiting} en file",
    )
    after = [state.task(task_id)["status"] for task_id in task_ids]
    expect(after == ["cancelled"] * len(task_ids), f"après annulation, statuts {after}")
    state.expect_folders_gone(task_ids)
    return (
        f"1 tâche longue en cours et {waiting} en file, la n° {2 + waiting} reçoit 429 long_audio_busy ; les "
        f"{len(task_ids)} tâches annulées et leurs dossiers effacés"
    )


def wait_healthy(state: State) -> float:
    """Attend que dictee réponde 200 sur `/health` et que Docker le dise sain ; rend les secondes attendues."""
    started = time.monotonic()
    deadline = started + HEALTHY_TIMEOUT
    health = "?"
    while time.monotonic() < deadline:
        try:
            answers = state.http.get("/health", timeout=5).status_code == 200
        except httpx.HTTPError:
            answers = False
        inspect = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", state.dictee], capture_output=True, text=True
        )
        health = inspect.stdout.strip()
        if answers and health == "healthy":
            return time.monotonic() - started
        time.sleep(1)
    raise Failure(f"dictee n'est pas sain {HEALTHY_TIMEOUT:.0f} s après le redémarrage (Docker : {health})")


def check_restart(state: State) -> str:
    """Une tâche longue en mode `dictee`, en arrière-plan, avec `compose restart dictee` après deux morceaux.

    Le premier avancement non nul vaut 1/N pour une tâche de N morceaux : le redémarrage part dès que l'avancement
    atteint deux morceaux. Après le redémarrage, dictee redevient sain, l'avancement n'a pas reculé et la tâche
    reprend jusqu'à `completed`. Sa ligne de fin porte `starts=2` et `chunks=N`.
    """
    created = state.created_task(state.long_wav())
    task_id = created["id"]
    before = len(state.dictee_lines())
    try:
        step = 0.0
        deadline = time.monotonic() + TASK_TIMEOUT
        while True:
            task = state.task(task_id)
            progress = task["progress"]
            expect(task["status"] not in TERMINAL, f"la tâche finit en {task['status']} avant le redémarrage")
            step = step or progress
            if step and progress >= 2 * step - 1e-9:
                break
            expect(time.monotonic() < deadline, f"la tâche n'atteint pas deux morceaux en {TASK_TIMEOUT:.0f} s")
            time.sleep(POLL_INTERVAL)
        chunks = round(1 / step)
        restart = check_nemo.compose("restart", "dictee")
        expect(restart.returncode == 0, f"`compose restart dictee` a échoué : {restart.stderr.strip()[:300]}")
        healthy_s = wait_healthy(state)
        resumed = state.task(task_id)
        expect(
            resumed["progress"] >= progress,
            f"l'avancement recule de {progress} à {resumed['progress']} après le redémarrage",
        )
        task, _ = state.wait_task(task_id)
    finally:
        state.cancel_unfinished([task_id])
    expect(task["status"] == "completed", f"la tâche finit en {task['status']} : {task['error']}")
    line = state.task_line(task_id, before)
    found = {name: field_of(line, name) for name in ("mode", "chunks", "starts", "status")}
    wanted = {"mode": "raw", "chunks": str(chunks), "starts": "2", "status": "completed"}
    expect(found == wanted, f"la ligne de fin porte {found} au lieu de {wanted}")
    state.expect_folders_gone([task_id])
    return (
        f"redémarrage à {progress:.3f} ({round(progress * chunks)} morceaux sur {chunks}), dictee sain {healthy_s:.0f} s "
        f"après, avancement relu {resumed['progress']:.3f} ; la tâche finit completed avec starts=2 et chunks={chunks}"
    )


# (clé, titre, fonction, contrôles requis avant, mesure sans échec). Les contrôles longs sont à la fin, et `restart`,
# qui redémarre dictee, en dernier.
CHECKS: list[tuple[str, str, Callable[[State], str], tuple[str, ...], bool]] = [
    ("compose", "Conventions du compose", check_compose, (), False),
    ("health", "GET /health", check_health, (), False),
    ("synthesis", "Synthèse des deux phrases en wav", check_synthesis, ("health",), False),
    ("transcription", "Transcription dictee du dialogue", check_transcription, ("synthesis",), False),
    ("diarize", "dictee-diarize en diarized_json, vtt et srt", check_diarize, ("synthesis",), False),
    ("stream", "stream=true", check_stream, ("synthesis",), False),
    ("clean", "dictee-clean avec LiteLLM injoignable", check_clean, ("transcription",), False),
    ("formats", "Synthèse en mp3, opus, aac, flac et pcm", check_formats, ("health",), False),
    ("background", "Tâche en arrière-plan lue par GET", check_background, ("synthesis",), False),
    ("live", "Dictée en direct, avec input_audio_buffer.clear", check_live, ("synthesis",), False),
    ("concurrency", "Concurrence", check_concurrency, ("synthesis",), False),
    ("rejoin", "Requête coupée puis fichier renvoyé", check_rejoin, ("synthesis",), False),
    ("hangup", "Raccroché d'une requête synchrone", check_hangup, ("synthesis",), False),
    ("streamhangup", "Raccroché en plein flux", check_stream_hangup, ("transcription",), False),
    ("longstream", "Flux de l'audio long jusqu'au bout", check_long_stream, ("synthesis",), False),
    ("queued", "File des tâches longues pleine", check_queued, ("synthesis",), False),
    ("restart", "Redémarrage de dictee pendant une tâche", check_restart, ("synthesis",), False),
]


def select(only: set[str]) -> list[tuple[str, str, Callable[[State], str], tuple[str, ...], bool]]:
    """Les contrôles demandés, avec ceux dont ils dépendent, dans l'ordre de CHECKS."""
    wanted = set(only)
    for key, _, _, needs, _ in reversed(CHECKS):
        if key in wanted:
            wanted.update(needs)
    return [check for check in CHECKS if check[0] in wanted]


def run_check(state: State, title: str, func: Callable[[State], str], measure: bool) -> bool:
    started = time.monotonic()
    try:
        detail, ok = func(state), True
    except Failure as error:
        detail, ok = str(error), False
    except Exception as error:  # un échec de transport, de l'API ou du script compte comme un échec du contrôle
        detail, ok = f"{type(error).__name__} : {error}", False
    label = ("INFO" if measure else "OK") if ok else "ÉCHEC"
    print(f"{label} {title} ({time.monotonic() - started:.1f} s) : {detail[:700]}", flush=True)
    return ok


def peaks(state: State) -> str:
    nemo = state.nemo_sampler.peak() if state.nemo_sampler else None
    dictee = state.dictee_sampler.peak() if state.dictee_sampler else None
    nemo_text = "inconnu" if nemo is None else f"{nemo / 1024**3:.2f} Gio"
    dictee_text = "inconnu" if dictee is None else f"{dictee / 1024**2:.0f} Mio"
    return (
        f"pic de mémoire sur tout le script : nemo {nemo_text} (limite {NEMO_MEMORY_LIMIT / 1024**3:.0f} Gio), "
        f"dictee {dictee_text} (limite {DICTEE_MEMORY_LIMIT / 1024**2:.0f} Mio)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    names = ", ".join(check[0] for check in CHECKS)
    parser.add_argument(
        "--only",
        default="",
        help=f"rejoue seulement ces contrôles (séparés par des virgules), avec ceux dont ils dépendent, sur la stack "
        f"déjà démarrée. Contrôles : {names}",
    )
    args = parser.parse_args()
    only = {part.strip() for part in args.only.split(",") if part.strip()}
    unknown = only - {check[0] for check in CHECKS}
    if unknown:
        parser.error(f"contrôle inconnu : {', '.join(sorted(unknown))}")
    selected = select(only) if only else CHECKS

    key = nemo_rt.env_value("OPENWEBUI_SERVICE_KEY")
    WORK.mkdir(exist_ok=True)
    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=httpx.Timeout(900.0, connect=10.0)) as http:
        ai = openai.OpenAI(base_url=f"{BASE_URL}/v1", api_key=key, max_retries=0, timeout=900.0)
        state = State(
            key,
            http,
            ai,
            nemo=check_nemo.nemo_container(),
            dictee=check_nemo.compose("ps", "-q", "dictee").stdout.strip(),
        )
        if state.nemo:
            state.nemo_sampler = Sampler(state.nemo)
        if state.dictee:
            state.dictee_sampler = Sampler(state.dictee)
        passed = ran = 0
        failed = False
        try:
            for _, title, func, _, measure in selected:
                ok = run_check(state, title, func, measure)
                if not measure:
                    ran += 1
                    passed += ok
                if not ok:
                    failed = True
                    break
        finally:
            info(peaks(state))
            for sampler in (state.nemo_sampler, state.dictee_sampler):
                if sampler:
                    sampler.stop()
    print(f"{passed} contrôle(s) OK sur {ran}" + (", arrêt au premier échec" if failed else ""), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
