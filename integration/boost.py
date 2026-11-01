# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx==0.28.1", "websockets==17.4"]
# ///
"""Mesure la force du renforcement du glossaire sur le vrai NeMo-Speech.cpp (tâche 14).

Prérequis : `make integration` ou `make nemo-up` (NeMo répond sur http://127.0.0.1:18001).
Usage : uv run --script integration/boost.py

Sofia dit quatre phrases qui contiennent chacune un terme rare (Nemotron, Sortformer, Magpie, Starlette) et une phrase
témoin qui n'en contient aucun. Chaque phrase passe seule dans une session temps réel de NeMo, par
`integration/nemo_rt.py`, sans renforcement (force 0) puis avec chaque force de FORCES, les quatre termes formant le
glossaire. Tout le balayage est joué SWEEPS fois, pour voir si le résultat est stable.

Pour chaque force le script affiche le nombre de termes rares reconnus sur 4, les termes apparus à tort dans la phrase
témoin, la stabilité entre les passes et le texte transcrit de chaque phrase. Il termine par la plus petite force qui
reconnaît les quatre termes sans rien insérer dans la phrase témoin, ou dit qu'aucune ne le fait. Une phrase témoin
transcrite en texte vide compte comme un échec, pas comme une phrase sans insertion.

Un terme est reconnu s'il figure comme mot, sans tenir compte de la casse ni des accents, dans la transcription de sa
propre phrase. Un terme est inséré s'il figure, sous la même règle de casse et d'accents, n'importe où dans la
transcription de la phrase témoin. Les phrases sont neutres : aucun contenu personnel.
"""

from __future__ import annotations

import re
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nemo_rt  # noqa: E402

RATE = 16000
VOICE = "Sofia"
LANGUAGE = "fr-FR"
# Le glossaire envoyé à NeMo, et la phrase de chaque terme.
SENTENCES = {
    "Nemotron": "Le modèle Nemotron transcrit la réunion en direct.",
    "Sortformer": "Le modèle Sortformer sépare les voix des participants.",
    "Magpie": "La synthèse vocale Magpie lit le texte à voix haute.",
    "Starlette": "Le serveur web repose sur Starlette pour ses routes.",
}
TERMS = list(SENTENCES)
CONTROL_LABEL = "témoin"
CONTROL = "Nous parlons de la météo de demain matin."
LABELS = [*TERMS, CONTROL_LABEL]
# Force 0 : aucune liste `speech_contexts`, donc aucun renforcement. À 6 et 10 le terme renforcé remplace d'autres
# mots (relevé de la tâche 1) : le balayage s'arrête à 6.
FORCES = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
SWEEPS = 2
COMPLETED = "conversation.item.input_audio_transcription.completed"

# transcripts[sweep][force][étiquette] -> texte transcrit
Transcripts = list[dict[float, dict[str, str]]]


def fold(text: str) -> str:
    """Minuscules sans accents, pour comparer sans tenir compte de la casse ni des accents."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


def recognized(term: str, text: str) -> bool:
    """Le terme figure comme mot de `text`."""
    return fold(term) in re.findall(r"\w+", fold(text))


def inserted(text: str) -> list[str]:
    """Les termes du glossaire qui figurent n'importe où dans `text`."""
    return [term for term in TERMS if fold(term) in fold(text)]


def transcribe(pcm: bytes, force: float) -> str:
    """Transcrit `pcm` par une session temps réel ; sans renforcement quand la force est 0."""
    events = nemo_rt.realtime(pcm, RATE, TERMS if force > 0 else [], force, language=LANGUAGE)
    errors = [event for event in events if event.get("type") == "error"]
    if errors:
        raise RuntimeError(f"NeMo a répondu une erreur à la force {force} : {errors[0]}")
    finals = [event.get("transcript", "").strip() for event in events if event.get("type") == COMPLETED]
    return " ".join(text for text in finals if text)


def score(texts: dict[str, str]) -> tuple[int, list[str]]:
    """(termes reconnus sur 4, termes insérés dans la phrase témoin) pour un balayage d'une force."""
    found = sum(recognized(term, texts[term]) for term in TERMS)
    return found, inserted(texts[CONTROL_LABEL])


def control_ok(text: str) -> bool:
    """La phrase témoin est transcrite et n'a reçu aucun terme du glossaire."""
    return bool(text.strip()) and not inserted(text)


def meets_criterion(texts: dict[str, str]) -> bool:
    found, _ = score(texts)
    return found == len(TERMS) and control_ok(texts[CONTROL_LABEL])


def cell(texts: dict[str, str]) -> tuple[str, str]:
    found, extra = score(texts)
    if not texts[CONTROL_LABEL].strip():
        return f"{found}/{len(TERMS)}", "témoin vide"
    return f"{found}/{len(TERMS)}", f"{len(extra)} ({', '.join(extra)})" if extra else "0"


def print_table(transcripts: Transcripts) -> None:
    print("\nTABLEAU (une colonne par passe ; stable = textes identiques sur les cinq phrases entre les passes)")
    numbers = range(1, SWEEPS + 1)
    head = ["force", *[f"reconnus p{n}" for n in numbers], *[f"insérés p{n}" for n in numbers], "stable"]
    rows = []
    for force in FORCES:
        cells = [cell(sweep[force]) for sweep in transcripts]
        stable = all(sweep[force] == transcripts[0][force] for sweep in transcripts)
        rows.append([f"{force:.1f}", *[c[0] for c in cells], *[c[1] for c in cells], "oui" if stable else "NON"])
    widths = [max(len(row[i]) for row in [head, *rows]) for i in range(len(head))]
    for row in [head, *rows]:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip())


def print_texts(transcripts: Transcripts) -> None:
    print("\nTEXTES TRANSCRITS")
    print("Référence :")
    for label in LABELS:
        print(f"  {label:<10} {CONTROL if label == CONTROL_LABEL else SENTENCES[label]}")
    for force in FORCES:
        print(f"\nForce {force:.1f}" + (" (sans renforcement)" if force == 0 else ""))
        for number, sweep in enumerate(transcripts, start=1):
            print(f"  passe {number}")
            for label in LABELS:
                text = sweep[force][label]
                mark = recognized(label, text) if label != CONTROL_LABEL else control_ok(text)
                print(f"    {label:<10} {'ok ' if mark else 'KO '} {text}")


def print_differences(transcripts: Transcripts) -> None:
    print("\nDIFFÉRENCES ENTRE LES PASSES")
    differences = 0
    for force in FORCES:
        for label in LABELS:
            variants = [sweep[force][label] for sweep in transcripts]
            if len(set(variants)) > 1:
                differences += 1
                print(f"  force {force:.1f}, {label} :")
                for number, text in enumerate(variants, start=1):
                    print(f"    passe {number} : {text}")
    if not differences:
        print("  aucune : les deux passes donnent les mêmes textes à toutes les forces")


def print_verdict(transcripts: Transcripts) -> None:
    print("\nCRITÈRE : les quatre termes reconnus, la phrase témoin transcrite et sans terme inséré")
    baseline = [meets_criterion(sweep[0.0]) for sweep in transcripts]
    if all(baseline):
        print("  Attention : la force 0 (sans renforcement) remplit déjà le critère dans toutes les passes.")
    elif any(baseline):
        print("  Attention : la force 0 (sans renforcement) remplit le critère dans une passe seulement.")
    smallest = None
    for force in FORCES[1:]:
        ok_per_sweep = [meets_criterion(sweep[force]) for sweep in transcripts]
        verdict = "aucune passe"
        if all(ok_per_sweep):
            verdict = "toutes les passes"
        elif any(ok_per_sweep):
            verdict = "une passe seulement"
        print(f"  force {force:.1f} : critère rempli dans {verdict}")
        if smallest is None and all(ok_per_sweep):
            smallest = force
    if smallest is None:
        print("PLUS PETITE FORCE RETENUE : aucune ne remplit le critère dans toutes les passes")
    else:
        print(f"PLUS PETITE FORCE RETENUE : {smallest:.1f}")


def main() -> int:
    started = time.monotonic()
    pcms: dict[str, bytes] = {}
    for label in LABELS:
        text = CONTROL if label == CONTROL_LABEL else SENTENCES[label]
        pcms[label] = nemo_rt.synth(text, VOICE, RATE)
        print(f"synthèse {label} : {len(pcms[label]) / (RATE * 2):.1f} s d'audio", flush=True)
    transcripts: Transcripts = []
    for number in range(1, SWEEPS + 1):
        sweep: dict[float, dict[str, str]] = {}
        for force in FORCES:
            force_started = time.monotonic()
            sweep[force] = {label: transcribe(pcms[label], force) for label in LABELS}
            found, extra = score(sweep[force])
            print(
                f"passe {number}, force {force:.1f} : {found}/{len(TERMS)} reconnus, {len(extra)} inséré(s), "
                f"{time.monotonic() - force_started:.1f} s",
                flush=True,
            )
        transcripts.append(sweep)
    print_table(transcripts)
    print_differences(transcripts)
    print_verdict(transcripts)
    print_texts(transcripts)
    print(f"\nDurée totale : {time.monotonic() - started:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
