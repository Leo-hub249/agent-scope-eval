# agent-scope-eval: common entry points. Run `make help`.
SHELL := /bin/bash
.DEFAULT_GOAL := help
export DOCKER_CLI_HINTS := false

UV      := uv
COMPOSE := docker compose -f sandbox/compose.yaml
IMAGE   := agent-scope-eval/sandbox:latest

.PHONY: help setup sandbox sandbox-test sandbox-shell sandbox-clean lock-sandbox test run score monitor analyze

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-15s %s\n", $$1, $$2}'

setup: ## Create the pinned Python 3.12 environment from uv.lock
	$(UV) sync --locked

sandbox: ## Build the sandbox image and print its id
	$(COMPOSE) build
	@echo "image id: $$(docker image inspect --format '{{.Id}}' $(IMAGE))"

sandbox-test: sandbox ## Smoke-test the sandbox: services, isolation, reset identity
	$(UV) run pytest -m docker tests/sandbox -v

sandbox-shell: ## Throwaway sandbox shell as ops (container destroyed on exit)
	@P=sbx-shell-$$$$; $(COMPOSE) -p $$P up -d --wait --no-build >/dev/null && \
	  docker exec -it -u ops $$P-server-1 bash; $(COMPOSE) -p $$P down -v --timeout 5 >/dev/null

sandbox-clean: ## Remove any leftover sandbox containers
	@docker ps -aq --filter label=project=agent-scope-eval | xargs -r docker rm -f

lock-sandbox: ## Re-pin the sandbox app dependencies (with hashes)
	$(UV) pip compile sandbox/app/requirements.in --universal --python-version 3.12 --generate-hashes -o sandbox/app/requirements.txt

test: ## Unit tests (no Docker, no API)
	$(UV) run pytest -m "not docker"

run: ## Run the experiment (Phase 4+)
	@echo "not implemented yet (Phase 4)"; exit 1

score: ## Score runs from logs (Phase 5+)
	@echo "not implemented yet (Phase 5)"; exit 1

monitor: ## Run the monitor over runs (Phase 7+)
	@echo "not implemented yet (Phase 7)"; exit 1

analyze: ## Tables and plots (Phase 7+)
	@echo "not implemented yet (Phase 7)"; exit 1
