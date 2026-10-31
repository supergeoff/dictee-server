# `docker compose` (plugin) ou `docker-compose` (binaire seul) selon ce que la machine fournit.
COMPOSE ?= $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")
COMPOSE_FILES = -f compose.yaml -f compose.integration.yaml

.PHONY: nemo-up down network test

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
