# Ruta: Makefile
# Developer shortcuts for the XookHub stack. Run `make help` for the list.

# Use bash and run compose via the modern `docker compose` subcommand.
DC := docker compose

.DEFAULT_GOAL := help

.PHONY: help up down build restart logs ps migrate makemigration \
        downgrade revision-history shell worker-logs api-logs psql \
        create-bucket clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

up: ## Build (if needed) and start the whole stack in the background
	$(DC) up -d --build

down: ## Stop and remove all containers (keeps volumes/data)
	$(DC) down

build: ## Rebuild the api/worker image
	$(DC) build

restart: ## Restart api + worker (e.g. after dependency changes)
	$(DC) restart api worker

logs: ## Tail logs from all services
	$(DC) logs -f

api-logs: ## Tail only the API logs
	$(DC) logs -f api

worker-logs: ## Tail only the Celery worker logs
	$(DC) logs -f worker

ps: ## Show running services
	$(DC) ps

# --- Database migrations (run inside the api container so it shares the
# same env, network and installed deps) --------------------------------
migrate: ## Apply all pending Alembic migrations (upgrade head)
	$(DC) run --rm api alembic upgrade head

makemigration: ## Autogenerate a migration. Usage: make makemigration m="add x"
	$(DC) run --rm api alembic revision --autogenerate -m "$(m)"

downgrade: ## Roll back one migration (downgrade -1)
	$(DC) run --rm api alembic downgrade -1

revision-history: ## Show the migration history
	$(DC) run --rm api alembic history --verbose

# --- Utilities -------------------------------------------------------
shell: ## Open a shell inside the api container
	$(DC) exec api /bin/bash

psql: ## Open a psql session against the db service
	$(DC) exec db psql -U $${DB_USER} -d $${DB_NAME}

create-bucket: ## Re-run the MinIO bucket bootstrap manually (normally automatic on `make up`)
	$(DC) run --rm minio-init

clean: ## Stop everything AND delete volumes (DESTROYS all data)
	$(DC) down -v