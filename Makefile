# `docker compose` (plugin) ou `docker-compose` (binaire seul) selon ce que la machine fournit.
COMPOSE ?= $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")
COMPOSE_FILES = -f compose.yaml -f compose.integration.yaml

.PHONY: down integration nemo-up network test

.env:
	sh integration/make-env.sh > .env

network:
	docker network inspect dokploy-network >/dev/null 2>&1 || docker network create dokploy-network

# Démarre seulement models et nemo (le service dictee n'existe pas encore avant la tâche 2).
nemo-up: .env network
	$(COMPOSE) $(COMPOSE_FILES) up -d --build models nemo

down:
	$(COMPOSE) $(COMPOSE_FILES) down

# Lance pytest dans l'image Docker : ffmpeg n'est pas installé sur le Mac et la CI utilise les mêmes commandes.
test:
	docker build --target test -t dictee-test dictee
	docker run --rm dictee-test

# Lance la stack complète avec les vrais modèles, attend /health (300 s au plus), puis joue integration/run.py.
# La stack reste démarrée à la fin : `make down` l'arrête. Le script reçoit le binaire compose du Makefile.
integration: .env network
	$(COMPOSE) $(COMPOSE_FILES) up -d --build
	@echo "Attente de dictee sur /health (300 s au plus)..."
	@waited=0; \
	until curl -fs -o /dev/null http://127.0.0.1:18000/health; do \
		if [ $$waited -ge 300 ]; then echo "dictee ne répond pas sur /health après 300 s" >&2; exit 1; fi; \
		sleep 2; waited=$$((waited + 2)); \
	done
	COMPOSE="$(COMPOSE)" uv run --script integration/run.py
