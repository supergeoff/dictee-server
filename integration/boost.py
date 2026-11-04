# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx==0.28.1", "websockets==17.4"]
# ///
"""Mesure la force du glossaire sur le vrai NeMo-Speech.cpp (tâche 13, vérification 12.6 de la spec).

Prérequis : un NeMo qui répond (`NEMO_URL`, par défaut http://127.0.0.1:18001) et sa clé (`NEMO_API_KEY`, sinon `.env`).
Usage :
    uv run --script integration/boost.py --audio call.wav --audio phrase.wav:phrase.txt [--glossary termes.txt]
    uv run --script integration/boost.py --self-test

Pour chaque audio (WAV PCM16 mono) et chaque moteur, le script fait une passe sans glossaire, puis une passe par force
avec le glossaire de `--glossary` (un terme par ligne, vide par défaut) augmenté de « Nemotron », « Sortformer » et
« Discord ». Le moteur `rest` envoie des tranches nettes de 30 s à `/v1/audio/transcriptions` ; le moteur `realtime`
ouvre une session par fichier sur `/v1/audio/transcriptions/realtime`. La référence d'un audio est un fichier texte
(`--audio WAV:RÉFÉRENCE`) qui dit ce qui est prononcé ; sans elle, la référence est vide.

Les insertions d'un terme dans une passe valent `max(0, occurrences dans la passe - max(occurrences sans glossaire,
occurrences dans la référence))`, sur les mots repliés par `fold`. Le script affiche les insertions par moteur et par
force, sous forme de nombres seulement (les termes avec `--show-terms`), puis la force retenue : la plus haute sans
aucune insertion sur tout le corpus, ou 0,5 quand aucune n'y parvient. Les textes transcrits vont dans `--out`, sous
`.integration/` : ils ne se commitent jamais.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nemo_rt  # noqa: E402

# Les termes ajoutés au glossaire de `--glossary`, et la force à retenir quand toutes les autres font des insertions.
FIXED_TERMS = ("Nemotron", "Sortformer", "Discord")
FALLBACK_FORCE = 0.5
DEFAULT_FORCES = "0.5,1,1.5,2"
ENGINES = ("rest", "realtime")
LANGUAGE = "fr-FR"
SLICE_SECONDS = 30
DEFAULT_OUT = nemo_rt.ROOT / ".integration" / "verif-taches" / "boost"
COMPLETED = "conversation.item.input_audio_transcription.completed"
COMMITTED = "input_audio_buffer.committed"


@dataclass(frozen=True)
class Audio:
    name: str
    rate: int
    pcm: bytes
    reference: str

    @property
    def seconds(self) -> float:
        return len(self.pcm) / (self.rate * 2)


# texts[(moteur, audio, force)] -> texte transcrit ; la force vaut None pour la passe sans glossaire.
Texts = dict[tuple[str, str, float | None], str]


def fold(text: str) -> str:
    """Minuscules sans accents, pour comparer sans tenir compte de la casse ni des accents."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


def words(text: str) -> list[str]:
    """Les mots de `text`, repliés par `fold`."""
    return re.findall(r"\w+", fold(text))


def occurrences(term: str, text: str) -> int:
    """Nombre de fois où les mots de `term` se suivent dans `text`, sur les mots repliés."""
    wanted, found = words(term), words(text)
    if not wanted:
        return 0
    return sum(found[start : start + len(wanted)] == wanted for start in range(len(found) - len(wanted) + 1))


def insertions(term: str, passe: str, baseline: str, reference: str) -> int:
    """Insertions de `term` dans `passe` : ses occurrences au-delà de la sortie sans glossaire et de la référence."""
    return max(0, occurrences(term, passe) - max(occurrences(term, baseline), occurrences(term, reference)))


def force_label(force: float) -> str:
    """La force écrite à la française : 0,5 ; 1 ; 1,5."""
    return f"{force:g}".replace(".", ",")


def verdict(totals: dict[float, int]) -> str:
    """La ligne de verdict : la plus haute force sans insertion, ou la force de repli quand toutes en font."""
    free = [force for force, total in totals.items() if total == 0]
    if free:
        return f"force retenue : {force_label(max(free))}"
    return f"aucune force sans insertion : {force_label(FALLBACK_FORCE)}"


def glossary_terms(file_terms: list[str]) -> list[str]:
    """Les termes du fichier, puis ceux de `FIXED_TERMS` qui n'y figurent pas, sans doublon à la casse près."""
    terms: list[str] = []
    seen: set[str] = set()
    for term in [*file_terms, *FIXED_TERMS]:
        key = " ".join(words(term))
        if key and key not in seen:
            seen.add(key)
            terms.append(term)
    return terms


def read_glossary(path: Path | None) -> list[str]:
    """Les termes de `path`, un par ligne ; une liste vide sans fichier."""
    if path is None:
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_audio(spec: str) -> Audio:
    """Lit `WAV[:RÉFÉRENCE]` : le WAV PCM16 mono, et le texte de référence quand un fichier est donné."""
    wav, _, reference = spec.partition(":")
    path = Path(wav)
    rate, pcm = nemo_rt.parse_wav(path.read_bytes())
    text = Path(reference).read_text(encoding="utf-8") if reference else ""
    return Audio(path.stem, rate, pcm, text)


def transcribe_rest(audio: Audio, phrases: list[str], boost: float) -> str:
    """Transcrit `audio` par tranches nettes de `SLICE_SECONDS` sur la route REST ; joint les textes par une espace."""
    form = {"language": LANGUAGE, "response_format": "verbose_json", "timestamp_granularities[]": "word"}
    if phrases:
        form["speech_contexts"] = json.dumps([{"phrases": phrases, "boost": boost}])
    size = SLICE_SECONDS * audio.rate * 2
    texts = []
    for start in range(0, len(audio.pcm), size):
        wav = nemo_rt.wav_file(audio.pcm[start : start + size], audio.rate)
        response = httpx.post(
            f"{nemo_rt.BASE_URL}/v1/audio/transcriptions",
            data=form,
            files={"file": ("slice.wav", wav, "audio/wav")},
            headers=nemo_rt.auth_headers(),
            timeout=600,
        )
        response.raise_for_status()
        texts.append(response.json()["text"].strip())
    return " ".join(text for text in texts if text)


def transcribe_realtime(audio: Audio, phrases: list[str], boost: float) -> str:
    """Transcrit `audio` dans une seule session temps réel ; joint les textes finals par une espace."""
    info: dict[str, object] = {}
    events = nemo_rt.realtime(audio.pcm, audio.rate, phrases, boost, language=LANGUAGE, info=info)
    errors = [event for event in events if event.get("type") == "error"]
    if errors:
        raise RuntimeError(f"NeMo a répondu une erreur sur {audio.name} : {errors[0]}")
    if info["ended_by"] != COMMITTED:
        raise RuntimeError(
            f"session interrompue sur {audio.name} : fin par {info['ended_by']}, code {info['close_code']}"
        )
    finals = [event.get("transcript", "").strip() for event in events if event.get("type") == COMPLETED]
    return " ".join(text for text in finals if text)


def measure(audios: list[Audio], terms: list[str], forces: list[float], engines: list[str], out: Path) -> Texts:
    """Joue, pour chaque audio et chaque moteur, la passe sans glossaire puis une passe par force ; écrit les textes."""
    transcribe = {"rest": transcribe_rest, "realtime": transcribe_realtime}
    out.mkdir(parents=True, exist_ok=True)
    texts: Texts = {}
    for audio in audios:
        for engine in engines:
            for force in [None, *forces]:
                started = time.monotonic()
                phrases, boost = ([], 0.0) if force is None else (terms, force)
                text = transcribe[engine](audio, phrases, boost)
                texts[(engine, audio.name, force)] = text
                label = "sans-glossaire" if force is None else f"force-{force:g}"
                (out / f"{audio.name}.{engine}.{label}.txt").write_text(text + "\n", encoding="utf-8")
                shown = "sans glossaire" if force is None else f"force {force_label(force)}"
                print(
                    f"{audio.name} ({audio.seconds:.0f} s), {engine}, {shown} : {time.monotonic() - started:.0f} s",
                    flush=True,
                )
    return texts


def cell_insertions(texts: Texts, terms: list[str], engine: str, audio: Audio, force: float) -> dict[str, int]:
    """Insertions de chaque terme (celles qui ne sont pas nulles) dans la passe de `audio` à `force`."""
    passe = texts[(engine, audio.name, force)]
    baseline = texts[(engine, audio.name, None)]
    counts = {term: insertions(term, passe, baseline, audio.reference) for term in terms}
    return {term: count for term, count in counts.items() if count}


def print_report(
    texts: Texts, audios: list[Audio], terms: list[str], forces: list[float], engines: list[str], show_terms: bool
) -> None:
    """Affiche le tableau des insertions, les termes si demandé, et le verdict."""
    cells = {
        (engine, force, audio.name): cell_insertions(texts, terms, engine, audio, force)
        for engine in engines
        for force in forces
        for audio in audios
    }
    head = ["moteur", "force", *[audio.name for audio in audios], "total"]
    rows = []
    for engine in engines:
        for force in forces:
            counts = [sum(cells[(engine, force, audio.name)].values()) for audio in audios]
            rows.append([engine, force_label(force), *map(str, counts), str(sum(counts))])
    widths = [max(len(row[index]) for row in [head, *rows]) for index in range(len(head))]
    print(f"\nINSERTIONS ({len(audios)} audio(s), {len(terms)} terme(s) dans le glossaire, nombres de mots en trop)")
    for row in [head, *rows]:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip())
    if show_terms:
        print("\nTERMES INSÉRÉS (cases non nulles)")
        for (engine, force, name), counts in cells.items():
            if counts:
                detail = ", ".join(f"{term} x{count}" for term, count in counts.items())
                print(f"  {engine}, force {force_label(force)}, {name} : {detail}")
    totals = {
        force: sum(sum(cells[(engine, force, audio.name)].values()) for engine in engines for audio in audios)
        for force in forces
    }
    print(f"\n{verdict(totals)}")


def self_test() -> None:
    # Un terme absent partout n'est jamais inséré.
    assert insertions("Nemotron", "bonjour tout le monde", "bonjour tout le monde", "bonjour tout le monde") == 0
    # Un terme inséré deux fois, absent de la sortie sans glossaire et de la référence, compte deux insertions.
    assert insertions("Nemotron", "un nemotron puis nemotron", "un puis", "un puis") == 2
    # Un terme dit une fois dans la référence et rendu deux fois ne compte qu'une insertion.
    assert insertions("Nemotron", "nemotron et nemotron", "et", "nemotron et") == 1
    # La casse et les accents ne comptent pas, et un terme de plusieurs mots se compte comme une suite de mots.
    assert insertions("Café Crème", "un CAFÉ, crème et un cafe creme", "un et un", "un et un") == 2
    # La force retenue est la plus haute sans insertion, même quand une force plus basse en a.
    assert verdict({0.5: 0, 1.0: 0, 1.5: 2, 2.0: 1}) == "force retenue : 1"
    assert verdict({0.5: 1, 1.0: 0, 1.5: 3, 2.0: 0}) == "force retenue : 2"
    assert verdict({0.5: 0, 1.0: 0}) == "force retenue : 1"
    # Sans aucune force sans insertion, la valeur est 0,5.
    assert verdict({0.5: 3, 1.0: 1}) == "aucune force sans insertion : 0,5"
    # Le glossaire reçoit les trois termes fixes une seule fois, à la casse près.
    assert glossary_terms(["discord", "Open WebUI"]) == ["discord", "Open WebUI", "Nemotron", "Sortformer"]
    print("self-test OK")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--glossary", type=Path, help="fichier de termes, un par ligne (vide par défaut)")
    parser.add_argument("--audio", action="append", default=[], metavar="WAV[:RÉFÉRENCE]", help="répétable")
    parser.add_argument("--forces", default=DEFAULT_FORCES, help=f"forces séparées par des virgules ({DEFAULT_FORCES})")
    parser.add_argument("--engines", default=",".join(ENGINES), help="moteurs séparés par des virgules (rest,realtime)")
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help="dossier des textes transcrits, sous .integration/"
    )
    parser.add_argument("--show-terms", action="store_true", help="affiche aussi les termes insérés")
    parser.add_argument("--self-test", action="store_true", help="vérifie le comptage et le verdict, sans NeMo")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if not args.audio:
        parser.error("au moins un --audio est nécessaire")
    engines = [engine for engine in args.engines.split(",") if engine]
    if not engines or any(engine not in ENGINES for engine in engines):
        parser.error(f"--engines accepte {', '.join(ENGINES)}")
    try:
        forces = [float(force) for force in args.forces.split(",") if force]
    except ValueError:
        parser.error("--forces attend des nombres séparés par des virgules")
    if not forces or any(force <= 0 for force in forces):
        parser.error("--forces attend des nombres positifs")
    audios = [load_audio(spec) for spec in args.audio]
    if len({audio.name for audio in audios}) != len(audios):
        parser.error("deux --audio portent le même nom de fichier")
    terms = glossary_terms(read_glossary(args.glossary))
    started = time.monotonic()
    texts = measure(audios, terms, forces, engines, args.out)
    print_report(texts, audios, terms, forces, engines, args.show_terms)
    print(f"\nDurée totale : {time.monotonic() - started:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
