.PHONY: up down reset logs migrate test lint typecheck check gen-api smoke frontend-check

COMPOSE := docker compose
API := $(COMPOSE) run --rm -T api
WEB := $(COMPOSE) run --rm -T --no-deps frontend

.env:
	cp .env.example .env
	@# A fresh local key for encrypting provider secrets. Never reuse it outside this machine.
	@key=$$(openssl rand -base64 32) && sed -i.bak "s|^SECRET_ENCRYPTION_KEYS=.*|SECRET_ENCRYPTION_KEYS=local1:$$key|" .env && rm -f .env.bak
	@key=$$(openssl rand -base64 32) && sed -i.bak "s|^ERASURE_HASH_KEY=.*|ERASURE_HASH_KEY=$$key|" .env && rm -f .env.bak

up: .env ## Start the whole local stack
	$(COMPOSE) up -d --build --wait

down: ## Stop the stack, keep data
	$(COMPOSE) down

reset: ## Stop the stack and delete local data
	$(COMPOSE) down -v

logs:
	$(COMPOSE) logs -f --tail=100

migrate: .env ## Apply database migrations as the migrator role
	$(COMPOSE) run --rm migrate

test: .env ## Backend tests against PostgreSQL as the runtime role
	$(API) pytest

lint: .env
	$(API) sh -c "ruff check . && ruff format --check ."

typecheck: .env
	$(API) mypy app

frontend-check: .env ## Frontend type-check, lint, tests and build
	$(WEB) sh -c "pnpm install --frozen-lockfile && pnpm typecheck && pnpm lint && pnpm test && pnpm build"

gen-api: .env ## Regenerate the OpenAPI document and the TypeScript client
	$(API) python -m app.export_openapi > frontend/openapi.json
	$(WEB) sh -c "pnpm install --frozen-lockfile && pnpm gen:api"

check: lint typecheck test frontend-check ## Everything CI runs

smoke: ## Verify a running stack end to end
	./infra/smoke.sh
