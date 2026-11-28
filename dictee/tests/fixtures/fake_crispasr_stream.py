#!/usr/bin/env python3
"""Faux binaire `crispasr --stream --stream-json --vad` pour les tests de la dictée en direct.

Il lit du PCM16 mono 16 kHz sur l'entrée standard et suit le protocole relevé sur CrispASR 0.8.55 : il découpe l'audio
reçu en pas de `--stream-step` millisecondes et n'émet rien tant qu'un pas n'est pas complet. Un pas dont plus d'un
échantillon sur dix dépasse 100 en valeur absolue compte comme de la parole. Un pas de parole ouvre une prise de parole
au besoin et émet un `partial` ; un pas sans parole compte dans le silence d'une prise de parole ouverte, qui reçoit son
`final` quand ce silence atteint `--stream-final-on-silence-ms`, et émet un `silence` quand aucune prise de parole n'est
ouverte. Les temps sont ceux de l'audio reçu. À la fin de l'entrée, la prise de parole ouverte reçoit son `final`, puis
le processus sort avec le code 0.

Variables d'environnement :

- `FAKE_CRISPASR_SCENARIO` : un objet JSON. `utterances` donne, pour chaque prise de parole dans l'ordre, ses textes
  partiels (`partials`, un par pas de parole, le dernier répété au besoin) et son texte final (`final`). `exit`, s'il
  est donné, fait sortir le processus avec ce code dès son lancement ; `exit_after_bytes` le fait sortir avec le code 1
  dès qu'il a reçu ce nombre d'octets ; `long_line` lui fait écrire d'abord une ligne de 70000 caractères ;
  `read_after` est un chemin de fichier : le faux binaire ne lit pas son entrée tant que ce fichier n'existe pas.
- `FAKE_CRISPASR_AUDIO` : un fichier où le faux binaire ajoute tout l'audio reçu, au fil de la lecture.
- `FAKE_CRISPASR_ARGV` : un fichier où il écrit ses arguments, en liste JSON.
"""

import json
import os
import struct
import sys
import time

RATE = 16000
SAMPLE_WIDTH = 2


def option(argv: list[str], name: str, default: int) -> int:
    return int(argv[argv.index(name) + 1]) if name in argv else default


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def is_speech(chunk: bytes) -> bool:
    samples = struct.unpack(f"<{len(chunk) // SAMPLE_WIDTH}h", chunk)
    return sum(1 for sample in samples if abs(sample) > 100) * 10 > len(samples)


def main() -> int:
    argv = sys.argv[1:]
    scenario = json.loads(os.environ.get("FAKE_CRISPASR_SCENARIO") or "{}")
    if path := os.environ.get("FAKE_CRISPASR_ARGV"):
        with open(path, "w") as file:
            json.dump(argv, file)
    if scenario.get("exit") is not None:
        return int(scenario["exit"])
    if scenario.get("long_line"):
        emit({"type": "partial", "text": "x" * 70000})
    if read_after := scenario.get("read_after"):
        while not os.path.exists(read_after):
            time.sleep(0.01)
    exit_after = scenario.get("exit_after_bytes")
    received = 0
    step_ms = option(argv, "--stream-step", 3000)
    final_ms = option(argv, "--stream-final-on-silence-ms", 800)
    step_bytes = RATE * SAMPLE_WIDTH * step_ms // 1000
    scripts = scenario.get("utterances", [])
    audio_path = os.environ.get("FAKE_CRISPASR_AUDIO")
    audio = open(audio_path, "ab") if audio_path else None  # noqa: SIM115
    buffer = bytearray()
    t = 0.0
    current: dict | None = None
    opened = 0
    silence_ms = 0

    def final() -> None:
        emit({"type": "final", "utterance_id": current["id"], "text": current["final"], "t0": current["t0"], "t1": t})

    while data := sys.stdin.buffer.read1(65536):
        if audio is not None:
            audio.write(data)
            audio.flush()
        buffer += data
        received += len(data)
        if exit_after is not None and received >= exit_after:
            return 1
        while len(buffer) >= step_bytes:
            chunk = bytes(buffer[:step_bytes])
            del buffer[:step_bytes]
            start, t = t, t + step_ms / 1000
            if is_speech(chunk):
                silence_ms = 0
                if current is None:
                    script = scripts[opened] if opened < len(scripts) else {}
                    opened += 1
                    current = {"id": opened, "t0": start, "partials": script.get("partials", []), "count": 0}
                    current["final"] = script.get("final", "")
                if partials := current["partials"]:
                    text = partials[min(current["count"], len(partials) - 1)]
                    current["count"] += 1
                    emit({"type": "partial", "utterance_id": current["id"], "text": text, "t0": current["t0"], "t1": t})
            elif current is not None:
                silence_ms += step_ms
                if silence_ms >= final_ms:
                    final()
                    current = None
            else:
                emit({"type": "silence", "t": t})
    if current is not None:
        final()
    return 0


if __name__ == "__main__":
    sys.exit(main())
