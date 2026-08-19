.DEFAULT_GOAL := help
.PHONY: help bootstrap up up-all down clean api worker web lint fmt typecheck test test-unit logs psql redis-cli migrate migration downgrade \
        images k8s-render k8s-validate kind-up kind-deploy kind-status kind-down tf-init tf-validate tf-fmt infra-check

help: ## Show available targets
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

bootstrap: ## First-time setup: .env, Python deps, web deps
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")
	@# Generate a local operator token if .env does not have one. Auth is
	@# enforced in every environment including local — the alternative is a
	@# dev-only bypass that eventually ships.
	@grep -q '^LO_ADMIN_TOKEN=.\+' .env || ( \
		TOKEN=$$(python3 -c "import secrets; print(secrets.token_urlsafe(32))"); \
		if grep -q '^LO_ADMIN_TOKEN=' .env; then \
			sed -i.bak "s|^LO_ADMIN_TOKEN=.*|LO_ADMIN_TOKEN=$$TOKEN|" .env && rm -f .env.bak; \
		else \
			printf "\nLO_ADMIN_TOKEN=%s\n" "$$TOKEN" >> .env; \
		fi; \
		echo "generated LO_ADMIN_TOKEN in .env" )
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

migrate: ## Apply all pending migrations
	uv run alembic upgrade head

migration: ## Autogenerate a migration: make migration m="add traces"
	@test -n "$(m)" || (echo 'usage: make migration m="short description"' && exit 1)
	uv run alembic revision --autogenerate -m "$(m)"
	@echo "Review the generated file before committing — check the downgrade()."

downgrade: ## Roll back exactly one migration
	uv run alembic downgrade -1

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

# --- Containers and Kubernetes ---------------------------------------------

IMAGE_TAG ?= dev
KIND_CLUSTER := llm-observatory

images: ## Build all three application images
	docker build -f apps/api/Dockerfile    -t llm-observatory/api:$(IMAGE_TAG) .
	docker build -f apps/worker/Dockerfile -t llm-observatory/worker:$(IMAGE_TAG) .
	docker build -f apps/web/Dockerfile    -t llm-observatory/web:$(IMAGE_TAG) apps/web

k8s-render: ## Print the fully rendered manifests: make k8s-render o=gcp
	kubectl kustomize infra/k8s/overlays/$(or $(o),kind)

k8s-validate: ## Schema-validate every overlay against the real Kubernetes API schemas
	@kubectl kustomize infra/k8s/base | kubeconform -strict -summary -
	@kubectl kustomize infra/k8s/overlays/kind | kubeconform -strict -summary -
	@# GCP CRDs (ManagedCertificate, BackendConfig, ExternalSecret) have no
	@# published schema outside a cluster, so they are skipped rather than
	@# failed. Everything with a schema is still checked strictly.
	@kubectl kustomize infra/k8s/overlays/gcp | kubeconform -strict -summary -ignore-missing-schemas -

kind-up: ## Create the local cluster and generate its secrets
	kind create cluster --config infra/k8s/overlays/kind/kind-cluster.yaml
	@# Real random values, not placeholders. The file is gitignored; a
	@# committed dev secret is the one that ends up reused in production.
	@test -f infra/k8s/overlays/kind/secrets.env || ( \
		PGPASS=$$(python3 -c "import secrets;print(secrets.token_urlsafe(24))"); \
		printf 'POSTGRES_PASSWORD=%s\nLO_DATABASE_URL=postgresql+asyncpg://lo:%s@lo-postgres:5432/llm_observatory\nLO_REDIS_URL=redis://lo-redis:6379/0\nLO_API_KEY_PEPPER=%s\nLO_ADMIN_TOKEN=%s\n' \
			"$$PGPASS" "$$PGPASS" \
			"$$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" \
			"$$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" \
			> infra/k8s/overlays/kind/secrets.env \
		&& echo "generated infra/k8s/overlays/kind/secrets.env" )

kind-deploy: images ## Build, load and deploy the whole stack into the local cluster
	kind load docker-image llm-observatory/api:$(IMAGE_TAG)    --name $(KIND_CLUSTER)
	kind load docker-image llm-observatory/worker:$(IMAGE_TAG) --name $(KIND_CLUSTER)
	kind load docker-image llm-observatory/web:$(IMAGE_TAG)    --name $(KIND_CLUSTER)
	kubectl delete job lo-migrate -n llm-observatory --ignore-not-found
	kubectl apply -k infra/k8s/overlays/kind
	@# Migrations first, then the rollout. `kubectl apply -k` does not order
	@# resources, so waiting here is what stops the API from starting against
	@# a schema that does not have its columns yet.
	kubectl wait --for=condition=complete job/lo-migrate -n llm-observatory --timeout=300s
	kubectl rollout status deployment/lo-api    -n llm-observatory --timeout=300s
	kubectl rollout status deployment/lo-worker -n llm-observatory --timeout=300s
	kubectl rollout status deployment/lo-web    -n llm-observatory --timeout=300s
	@echo
	@echo "  dashboard  http://localhost:30300"
	@echo "  api        http://localhost:30800/docs"

kind-status: ## What is running in the local cluster
	kubectl get pods,svc,job -n llm-observatory

kind-down: ## Delete the local cluster entirely
	kind delete cluster --name $(KIND_CLUSTER)
	rm -f infra/k8s/overlays/kind/secrets.env

# --- Terraform --------------------------------------------------------------
#
# There is deliberately no `tf-apply` target. This project provisions nothing:
# no GCP account, no billing, no spend. `validate` type-checks every resource
# argument against the real provider schemas, which is where configuration
# errors actually live.

tf-init: ## Download providers and initialise modules (no backend, no cloud call)
	cd infra/terraform && terraform init -backend=false

tf-validate: tf-init ## Type-check the configuration against the provider schemas
	cd infra/terraform && terraform validate

tf-fmt: ## Canonical formatting for every .tf file
	cd infra/terraform && terraform fmt -recursive .

infra-check: k8s-validate tf-validate ## Everything CI checks about infrastructure
	cd infra/terraform && terraform fmt -check -recursive .
