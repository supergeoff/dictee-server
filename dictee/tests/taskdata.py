"""Données de test partagées par les tests des tâches : registre, traitement, boucle de travail et routes."""

from dictee.modes import Mode
from dictee.profile import GlossaryEntry
from dictee.taskstore import TaskParams, TaskRecord, TaskResult, TaskStore, new_task_id
from dictee.types import Segment, Word

# Un instant fixe, en secondes Unix : les dates des tâches sont toutes des entiers.
NOW = 1_793_500_000

RAW = TaskParams("fr-FR", Mode.RAW, (GlossaryEntry("Nemotron"),), 1.0, None)

# Deux personnes, A puis B, et les deux segments qui en découlent ; ni reformulation ni décision.
RESULT = TaskResult(
    text="Bonjour à tous. Salut !",
    words=(
        Word("Bonjour", 0.0, 0.4, "A"),
        Word("à", 0.5, 0.6, "A"),
        Word("tous.", 0.6, 1.2, "A"),
        Word("Salut", 2.0, 2.4, "B"),
        Word("!", 2.4, 2.5, "B"),
    ),
    segments=(
        Segment("seg_0", 0.0, 1.2, "A", "Bonjour à tous."),
        Segment("seg_1", 2.0, 2.5, "B", "Salut !"),
    ),
    duration=12.0,
    language="fr-FR",
    cleaned=None,
    decision=None,
)


def make(store: TaskStore, **changes) -> TaskRecord:
    """Crée une tâche en file : propriétaire 1, modèle `dictee`, `RAW`, `json`, granularité `segment`, 12 s.

    Chaque valeur de `changes` remplace celle par défaut, `now` comprise pour la date de création. Chaque appel tire un
    identifiant neuf, sauf si `task_id` est donné.
    """
    values = {
        "task_id": new_task_id(),
        "owner": 1,
        "model": "dictee",
        "params": RAW,
        "response_format": "json",
        "granularities": frozenset({"segment"}),
        "upload_sha256": "a" * 64,
        "duration": 12.0,
        "now": NOW,
    }
    return store.create(**{**values, **changes})
