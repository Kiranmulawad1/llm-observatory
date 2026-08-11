.DEFAULT_GOAL := help
.PHONY: help bootstrap up up-all down clean api worker web lint fmt typecheck test test-unit logs psql redis-cli

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

bootstrap: ## First-time setup: .env, Python deps, web deps
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")
	uv sync --all-packages
	cd apps/web && npm install

up: ## Start Postgres + Redis (fast inner loop; run apps on the host)
	docker compose up -d postgres redis
	@docker compose ps

up-all: ## Start the whole stack in containers (exercises the real Dockerfiles)
	docker compose --profile full up -d --build
	@docker compose ps

down: ## Stop all services, keep data volumes
	docker compose --profile full down

clean: ## Stop everything and DELETE the data volumes
	docker compose --profile full down -v

api: ## Run the API on the host with hot reload
	uv run uvicorn lo_api.main:app --reload --host 0.0.0.0 --port 8000

worker: ## Run the arq worker on the host
	uv run arq lo_worker.main.WorkerSettings

web: ## Run the Next.js dev server
	cd apps/web && npm run dev

lint: ## Ruff lint + format check (matches CI exactly)
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Auto-fix lint and format
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## mypy strict
	uv run mypy packages apps

test: ## Full test suite
	uv run pytest

test-unit: ## Unit tests only (no Postgres/Redis needed)
	uv run pytest tests/unit -m "not integration"

logs: ## Tail all container logs
	docker compose --profile full logs -f

psql: ## Open psql against the local database
	docker compose exec postgres psql -U $${POSTGRES_USER:-lo} -d $${POSTGRES_DB:-llm_observatory}

redis-cli: ## Open redis-cli against local Redis
	docker compose exec redis redis-cli
