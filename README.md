# dictee-server

dictee est le point d'entrée audio de la stack supergeoff. Il sert la transcription (Nemotron 3.5 ASR), la séparation des locuteurs (Nemotron 3 Diarization) et la synthèse vocale (Magpie TTS) de NVIDIA, exécutées sur CPU par [NeMo-Speech.cpp](https://github.com/NVIDIA/NeMo-Speech.cpp), derrière une API compatible avec l'API audio d'OpenAI.

Chaque utilisateur se connecte avec PocketID sur une petite page de configuration, où il gère ses clés, son glossaire, ses consignes de reformulation et sa voix.

Le design est validé et l'implémentation reste à faire. La spec est dans [`docs/superpowers/specs/2026-10-31-dictee-server-design.md`](docs/superpowers/specs/2026-10-31-dictee-server-design.md), et les règles du dépôt pour les agents sont dans [`AGENTS.md`](AGENTS.md).
