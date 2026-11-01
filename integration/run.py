# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx==0.28.1", "openai==3.42.0", "websockets==17.4"]
# ///
"""Test d'intégration de bout en bout de dictee, sur la stack complète et les vrais modèles (spec, section 8).

Prérequis : `make integration`, qui démarre la stack (`compose.yaml` et `compose.integration.yaml`), attend `/health`
puis lance ce script. Usage : uv run --script integration/run.py [--only a,b,c]

`--only` rejoue seulement les contrôles nommés, avec ceux dont ils dépendent, sur une stack déjà démarrée : pratique
pour refaire un contrôle long (`--only hangup`) sans relancer tout le reste. `--help` donne la liste des noms.

dictee répond sur http://127.0.0.1:18000. Le script l'appelle avec la clé de service (`OPENWEBUI_SERVICE_KEY` du
`.env`, jamais affichée) et sans en-tête d'email : les valeurs par défaut s'appliquent. Chaque contrôle affiche `OK`
ou `ÉCHEC` avec sa durée, et le script s'arrête au premier échec. Les lignes `INFO` sont des mesures, sans verdict.
Les contrôles rapides passent d'abord, les contrôles longs à la fin, l'un après l'autre.

Le script ne lit du `.env` que la clé de service et n'affiche aucune valeur de ce fichier. Les audios produits vont
dans `.integration/`, ignoré par git.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import secrets
import socket
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

# Fréquence de tous les formats de sortie de la synthèse de dictee (`dictee.audio`).
SPEECH_RATE = 24000
MIN_WORDS_FOUND = 0.6
SPEED_TOLERANCE = 0.10
# Une transcription est longue au-delà de LONG_AUDIO_SECONDS (600 s par défaut) : 11 minutes la dépassent.
LONG_SECONDS = 11 * 60
# Avance de la transcription longue sur la requête qui la suit, en secondes.
LONG_HEADSTART = 5.0
# Raccroché en plein flux : attente maximale du premier delta après l'envoi de l'audio, puis fenêtre de relevé du CPU
# de nemo après le raccroché.
FIRST_DELTA_TIMEOUT = 30.0
HANGUP_WINDOW = 15.0
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


class Failure(Exception):
    """Un contrôle qui échoue ; le message dit pourquoi."""


def expect(condition: object, message: str) -> None:
    if not condition:
        raise Failure(message)


def info(text: str) -> None:
    print(f"INFO {text}", flush=True)


def note(text: str) -> None:
    print(f"   {text}", flush=True)


def env_value(name: str) -> str:
    """Lit une valeur du `.env` sans jamais l'afficher."""
    for line in (ROOT / ".env").read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip()
    raise Failure(f"{name} absente de .env (lancer `make integration`)")


def words(text: str) -> list[str]:
    """Les mots d'un texte, en minuscules et sans ponctuation."""
    return re.findall(r"\w+", text.lower())


def silence(seconds: float, rate: int = SPEECH_RATE) -> bytes:
    return bytes(int(rate * seconds) * 2)


def repeat_pcm(pcm: bytes, rate: int, seconds: float, gap: float = 1.0) -> bytes:
    """Répète `pcm`, séparé par `gap` seconde de silence, jusqu'à durer au moins `seconds`.

    `check_nemo.repeat_to` est figé à 16 kHz, alors que la synthèse de dictee rend du 24 kHz.
    """
    unit = pcm + silence(gap, rate)
    return unit * math.ceil(seconds * rate * 2 / len(unit))


def duration(pcm: bytes, rate: int = SPEECH_RATE) -> float:
    return len(pcm) / (rate * 2)


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


class RawStream:
    """Une transcription `stream=true` sur un socket brut : on voit les octets arriver et on raccroche à volonté.

    Le constructeur envoie toute la requête (en-têtes et audio). `started` est l'horloge du début de l'envoi,
    `sent_at` celle de la fin de l'envoi.
    """

    def __init__(self, wav: bytes, key: str, model: str = "dictee") -> None:
        body, boundary = multipart({"model": model, "stream": "true"}, "audio.wav", wav)
        head = (
            f"POST /v1/audio/transcriptions HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nAuthorization: Bearer {key}\r\n"
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
    """Ce que les contrôles partagent : les clients, les conteneurs, l'audio de test et les mesures au repos."""

    key: str
    http: httpx.Client
    ai: openai.OpenAI
    nemo: str
    dictee: str
    nemo_sampler: Sampler | None = None
    dictee_sampler: Sampler | None = None
    dialogue: bytes = b""
    rest_text: str = ""
    rest_seconds: float = 0.0
    _long: bytes = field(default=b"", repr=False)

    def upload(self) -> tuple[str, bytes, str]:
        return ("dialogue.wav", self.dialogue, "audio/wav")

    def long_wav(self) -> bytes:
        """Le dialogue répété sur LONG_SECONDS, assez long pour passer par la place des transcriptions longues."""
        if not self._long:
            _, pcm = nemo_rt.parse_wav(self.dialogue)
            self._long = nemo_rt.wav_file(repeat_pcm(pcm, SPEECH_RATE, LONG_SECONDS), SPEECH_RATE)
        return self._long

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
            "/v1/audio/transcriptions", data=data, files={"file": ("audio.wav", wav or self.dialogue, "audio/wav")}
        )

    def transcribe_result(self, wav: bytes, model: str = "dictee") -> Result:
        started = time.monotonic()
        response = self.transcribe(model, wav=wav)
        ended = time.monotonic()
        if response.status_code != 200:
            return Result(response.status_code, "", ended - started, ended, response.text[:200])
        return Result(200, response.json().get("text", ""), ended - started, ended)


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
    """Synthétise les deux phrases de la tâche 1 en wav, puis assemble le dialogue avec une seconde de silence."""
    parts: list[str] = []
    pcms: list[bytes] = []
    for index, (text, voice) in enumerate((check_nemo.PHRASE_1, check_nemo.PHRASE_2), start=1):
        speech = state.speak(text, voice, "wav")
        expect(speech.data[:4] == b"RIFF", f"phrase {index} ({voice}) : la réponse ne commence pas par RIFF")
        try:
            rate, pcm = nemo_rt.parse_wav(speech.data)
        except ValueError as error:
            raise Failure(f"phrase {index} ({voice}) : WAV illisible, {error}") from None
        expect(rate == SPEECH_RATE, f"phrase {index} ({voice}) : {rate} Hz au lieu de {SPEECH_RATE} Hz")
        expect(duration(pcm) > 1.0, f"phrase {index} ({voice}) : {duration(pcm):.1f} s d'audio seulement")
        pcms.append(pcm)
        parts.append(f"{voice} {duration(pcm):.1f} s d'audio, RIFF à {rate} Hz")
        if index == 1:
            info(
                f"première phrase : premier octet après {speech.first_byte_s:.1f} s, réponse entière en "
                f"{speech.total_s:.1f} s (critère de la spec : moins de 2 s sur le VPS, ici une indication)"
            )
    dialogue = pcms[0] + silence(1.0) + pcms[1]
    state.dialogue = nemo_rt.wav_file(dialogue, SPEECH_RATE)
    (WORK / "dictee-dialogue.wav").write_bytes(state.dialogue)
    return ", ".join(parts) + f", dialogue de {duration(dialogue):.1f} s"


def check_transcription(state: State) -> str:
    started = time.monotonic()
    result = state.ai.audio.transcriptions.create(model="dictee", file=state.upload(), response_format="json")
    state.rest_seconds = time.monotonic() - started
    state.rest_text = result.text
    note(f"transcription : {result.text}")
    expected = words(check_nemo.PHRASE_1[0]) + words(check_nemo.PHRASE_2[0])
    found = sum((Counter(expected) & Counter(words(result.text))).values())
    ratio = found / len(expected)
    detail = f"{found} mots attendus sur {len(expected)} retrouvés ({ratio:.0%}, minimum {MIN_WORDS_FOUND:.0%})"
    expect(ratio >= MIN_WORDS_FOUND, detail)
    return f"{detail}, {state.rest_seconds:.1f} s"


def check_diarize(state: State) -> str:
    response = state.transcribe("dictee-diarize", "diarized_json")
    expect(response.status_code == 200, f"diarized_json : HTTP {response.status_code} {response.text[:200]}")
    segments = response.json().get("segments", [])
    speakers = {segment["speaker"] for segment in segments}
    expect(speakers == {"A", "B"}, f"locuteurs {sorted(speakers)} sur {len(segments)} segments au lieu de ['A', 'B']")
    vtt = state.transcribe("dictee-diarize", "vtt")
    expect(vtt.status_code == 200, f"vtt : HTTP {vtt.status_code} {vtt.text[:200]}")
    expect("<v A>" in vtt.text and "<v B>" in vtt.text, "le VTT ne porte pas les balises <v A> et <v B>")
    srt = state.transcribe("dictee-diarize", "srt")
    expect(srt.status_code == 200, f"srt : HTTP {srt.status_code} {srt.text[:200]}")
    expect(srt.text.startswith("1\n"), f"le SRT commence par {srt.text[:10]!r} au lieu de '1\\n'")
    return f"{len(segments)} segments, locuteurs A et B, VTT avec <v A> et <v B>, SRT qui commence par 1"


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


def check_concurrency(state: State) -> str:
    long_wav = state.long_wav()
    short_seconds = duration(nemo_rt.parse_wav(state.dialogue)[1])
    note(f"audio long : {LONG_SECONDS / 60:.0f} min, audio court : {short_seconds:.1f} s")
    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        long_future = pool.submit(state.transcribe_result, long_wav)
        time.sleep(LONG_HEADSTART)
        short = state.transcribe_result(state.dialogue)
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


def check_hangup(state: State) -> str:
    """Raccroche au premier delta d'un flux long, puis vérifie que nemo s'arrête et qu'une courte n'en souffre pas.

    dictee relaie les deltas de NeMo : le premier arrive peu après l'envoi, bien avant la fin de la session, quand
    NeMo est en plein travail sur les 11 minutes d'audio. Le contrôle échoue si ce premier delta n'arrive pas en
    FIRST_DELTA_TIMEOUT secondes : le flux ne progresserait pas. Le socket se ferme dès ce delta, avant tout relevé
    de CPU : `docker stats` met une à deux secondes à répondre, et le CPU n'est lu qu'après le raccroché. Que nemo
    travaillait à ce moment-là, le delta le dit (NeMo n'en émet que pendant la session), et le flux n'avait pas fini.
    """
    wait_idle(state.nemo)
    stream = RawStream(state.long_wav(), state.key)
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
    short = state.transcribe_result(state.dialogue)
    expect(short.status == 200 and short.text, f"la courte après le raccroché : HTTP {short.status} {short.body}")
    limit = 2 * state.rest_seconds + 5
    detail = (
        f"la courte prend {short.seconds:.1f} s après le raccroché, "
        f"limite {limit:.1f} s (2 x {state.rest_seconds:.1f} s au repos + 5 s)"
    )
    expect(short.seconds < limit, detail)
    return f"raccroché au premier delta ({delay}) ; {detail}"


def check_queued(state: State) -> str:
    """Mesure le délai avant les en-têtes d'un flux long qui attend derrière une transcription longue.

    Mesure sans verdict sur les délais : le contrôle n'échoue que si une requête échoue (statut HTTP, texte vide).
    """
    wait_idle(state.nemo)
    long_wav = state.long_wav()
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(state.transcribe_result, long_wav)
        time.sleep(LONG_HEADSTART)
        stream = RawStream(long_wav, state.key)
        try:
            got_headers = stream.read_until(b"\r\n\r\n", 900)
            headers_at = time.monotonic()
            got_delta = got_headers and stream.read_until(DELTA, 60)
            status = stream.status
        finally:
            stream.close()
        first = running.result()
    expect(first.status == 200 and first.text, f"la longue qui tournait : HTTP {first.status} {first.body}")
    expect(status in (None, 200), f"le flux long en attente a répondu HTTP {status}")
    headers = (
        f"en-têtes {headers_at - stream.sent_at:.1f} s après la fin de l'envoi (HTTP {status}, premier delta "
        f"{'reçu' if got_delta else 'non reçu'})"
        if got_headers
        else "aucun en-tête reçu en 900 s"
    )
    return (
        f"{headers} ; la longue qui tournait (HTTP {first.status}) a fini "
        f"{first.ended_at - stream.sent_at:.1f} s après cet envoi"
    )


def check_long_stream(state: State) -> str:
    """Lit un flux de l'audio long jusqu'au bout : plus d'un delta, et leur concaténation égale le texte de `done`.

    Une seule phrase finale couvre les 11 minutes : c'est le cas où le texte relayé est le plus long avant son `Final`.
    """
    wait_idle(state.nemo)
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
    return (
        f"{len(streamed.deltas)} deltas dont la concaténation égale le texte de done ; premier delta "
        f"{streamed.first_delta:.1f} s après le début de la requête (envoi compris), fin après {streamed.seconds:.1f} s"
    )


# (clé, titre, fonction, contrôles requis avant, mesure sans échec). Les contrôles longs sont à la fin.
CHECKS: list[tuple[str, str, Callable[[State], str], tuple[str, ...], bool]] = [
    ("compose", "Conventions du compose", check_compose, (), False),
    ("health", "GET /health", check_health, (), False),
    ("synthesis", "Synthèse des deux phrases en wav", check_synthesis, ("health",), False),
    ("transcription", "Transcription dictee du dialogue", check_transcription, ("synthesis",), False),
    ("diarize", "dictee-diarize en diarized_json, vtt et srt", check_diarize, ("synthesis",), False),
    ("stream", "stream=true", check_stream, ("synthesis",), False),
    ("clean", "dictee-clean avec LiteLLM injoignable", check_clean, ("transcription",), False),
    ("formats", "Synthèse en mp3, opus, aac, flac et pcm", check_formats, ("health",), False),
    ("concurrency", "Concurrence", check_concurrency, ("synthesis",), False),
    ("hangup", "Raccroché en plein flux", check_hangup, ("transcription",), False),
    ("longstream", "Flux de l'audio long jusqu'au bout", check_long_stream, ("synthesis",), False),
    ("queued", "En-tête d'un flux long en attente", check_queued, ("synthesis",), True),
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

    key = env_value("OPENWEBUI_SERVICE_KEY")
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
