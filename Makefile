# `docker compose` (plugin) ou `docker-compose` (binaire seul) selon ce que la machine fournit.
COMPOSE ?= $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")
COMPOSE_FILES = -f compose.integration.yaml

.PHONY: check-crispasr-version check-nemo-version check-s2s-version down integration nemo-up s2s-check test

.env:
	sh integration/make-env.sh > .env

# Démarre seulement models et nemo, sans dictee : pratique pour travailler sur les modèles ou sur NeMo seul.
nemo-up: check-nemo-version .env
	$(COMPOSE) $(COMPOSE_FILES) up -d --build models nemo

# La version de NeMo-Speech.cpp n'est écrite nulle part dans ce dépôt : elle vient du compose de Dokploy, seule source.
# On la donne à chaque lancement qui construit l'image : NEMO_VERSION=<version> make integration.
# Le contrôle teste la variable d'environnement, celle que compose reçoit.
check-nemo-version:
	@test -n "$$NEMO_VERSION" || { echo "NEMO_VERSION est requise : la valeur de NEMO_VERSION dans les args du compose dictee de Dokploy (NEMO_VERSION=<version> make integration)" >&2; exit 1; }

# Même principe pour CrispASR : CRISPASR_VERSION=<version> make integration, avec la valeur du compose de Dokploy.
check-crispasr-version:
	@test -n "$$CRISPASR_VERSION" || { echo "CRISPASR_VERSION est requise : la valeur de CRISPASR_VERSION dans les args du compose dictee de Dokploy (CRISPASR_VERSION=<version> make integration)" >&2; exit 1; }

check-s2s-version:
	@test -n "$(S2S_REF)" || { echo "S2S_REF est requise : la valeur de S2S_REF dans les args du compose dictee de Dokploy (S2S_REF=<ref> make s2s-check)" >&2; exit 1; }

# Construit l'image s2s, valide conversation.yaml dedans, puis construit le relais (s2s/gate) et lance ses tests dans son image.
# Les variables {env: ...} du YAML reçoivent des valeurs factices : la validation ne contacte aucun service.
s2s-check: check-s2s-version
	docker build --build-arg S2S_REF=$(S2S_REF) -t conversation-check s2s
	docker run --rm --entrypoint python \
		-e LITELLM_URL=http://litellm.invalid/v1 -e CONVERSATION_LITELLM_KEY=test \
		-v $(PWD)/s2s/conversation.yaml:/config/conversation.yaml:ro \
		conversation-check -c "from speech_to_speech.config import load_config, validate_config; validate_config(load_config('/config/conversation.yaml')); print('Configuration valide')"
	docker build --target test -t conversation-gate-check s2s/gate
	docker run --rm conversation-gate-check

down:
	$(COMPOSE) $(COMPOSE_FILES) down

# Lance pytest dans l'image Docker : ffmpeg n'est pas installé sur le Mac et la CI utilise les mêmes commandes.
# ARGS est passé à pytest, avec des chemins relatifs à dictee/ : make test ARGS="tests/test_audio.py::test_name".
# Sans ARGS, toute la suite passe, comme dans la CI.
test:
	docker build --target test -t dictee-test dictee
	docker run --rm dictee-test uv run --frozen pytest -q $(ARGS)

# Demande CRISPASR_VERSION, NEMO_VERSION, PTTS_MODEL (hf://gradium/phonon-40m-fr@<commit>) et HF_TOKEN : les deux dernières
# sont lues par integration/make-env.sh, qui fabrique le .env, et l'échec de cette cible .env arrête tout avant le démarrage.
# Lance la stack complète avec les vrais modèles, attend /health (300 s au plus), puis joue integration/run.py.
# La stack reste démarrée à la fin : `make down` l'arrête. Le script reçoit le binaire compose du Makefile.
# Pour rejouer un seul contrôle sur la stack démarrée : uv run --script integration/run.py --only hangup (--help).
integration: check-nemo-version check-crispasr-version .env
	$(COMPOSE) $(COMPOSE_FILES) up -d --build
	@echo "Attente de dictee sur /health (300 s au plus)..."
	@waited=0; \
	until curl -fs -o /dev/null http://127.0.0.1:18000/health; do \
		if [ $$waited -ge 300 ]; then echo "dictee ne répond pas sur /health après 300 s" >&2; exit 1; fi; \
		sleep 2; waited=$$((waited + 2)); \
	done
	COMPOSE="$(COMPOSE)" uv run --script integration/run.py
