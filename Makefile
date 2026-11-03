# agent-scope-eval: common entry points. Run `make help`.
SHELL := /bin/bash
.DEFAULT_GOAL := help
export DOCKER_CLI_HINTS := false

UV      := uv
COMPOSE := docker compose -f sandbox/compose.yaml
IMAGE   := agent-scope-eval/sandbox:latest

.PHONY: labels-export labels-compare help setup sandbox sandbox-rebuild sandbox-verify sandbox-test validate-tasks show-prompt dry-run show-run pilot sandbox-shell sandbox-clean lock-sandbox test run score monitor analyze

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-15s %s\n", $$1, $$2}'

setup: ## Create the pinned Python 3.12 environment from uv.lock
	$(UV) sync --locked

sandbox: ## Build the sandbox image once and pin it (no-op if the locked image is present)
	@if $(UV) run python -m runner.image_lock verify 2>/dev/null; then \
	  echo "nothing to do; 'make sandbox-rebuild' rebuilds (this always changes the image id)"; \
	else $(MAKE) --no-print-directory sandbox-rebuild; fi

sandbox-rebuild: ## Rebuild + re-pin the image in sandbox/lock/ (new id; never during an experiment)
	$(COMPOSE) build
	$(UV) run python -m runner.image_lock write

sandbox-verify: ## Check that the local image is the locked one
	$(UV) run python -m runner.image_lock verify

sandbox-test: sandbox-verify ## Smoke-test the locked image: services, isolation, reset identity
	$(UV) run pytest -m docker tests/sandbox -v

sandbox-shell: ## Throwaway sandbox shell as ops (container destroyed on exit)
	@P=sbx-shell-$$$$; $(COMPOSE) -p $$P up -d --wait --no-build >/dev/null && \
	  docker exec -it -u ops $$P-server-1 bash; $(COMPOSE) -p $$P down -v --timeout 5 >/dev/null

sandbox-clean: ## Remove any leftover sandbox containers
	@docker ps -aq --filter label=project=agent-scope-eval | xargs -r docker rm -f

lock-sandbox: ## Re-pin the sandbox app dependencies (with hashes)
	$(UV) pip compile sandbox/app/requirements.in --universal --python-version 3.12 --generate-hashes -o sandbox/app/requirements.txt

validate-tasks: sandbox-verify ## Validate all tasks with scripted scenarios (no API) -> docs/task-validation.md
	$(UV) run python -m tasks.validate

test: ## Unit tests (no Docker, no API)
	$(UV) run pytest -m "not docker"

show-prompt: ## Show the agent's system prompt + first message for TASK=T0x (no API call)
	$(UV) run python -m runner.cli show-prompt --task $(TASK)

dry-run: sandbox-verify ## One real agent run on TASK=T0x with the primary model (API spend, budget-guarded)
	$(UV) run python -m runner.cli dry-run --task $(TASK)

show-run: ## Readable log, usage and cost of RUN=runs/<run_id>
	$(UV) run python -m runner.cli show-run $(RUN)

pilot: sandbox-verify ## One real run per task (TASKS="T01 T02 ...") in namespace "pilot" (API spend, budget-guarded)
	$(UV) run python -m runner.cli pilot --tasks $(TASKS)

run: ## Run the experiment (Phase 6)
	@echo "not implemented yet (Phase 6)"; exit 1

score: ## Rescore every stored run offline (no API, no Docker) -> runs/<id>/scores.json
	$(UV) run python -m scoring.score runs/2*Z-*

labels-export: ## Blind labelling packets + blank labels.yaml for the dry run and pilot -> labels/phase5/
	$(UV) run python -m scoring.labels export --namespaces dry-run pilot --out labels/phase5

labels-compare: ## Automatic scores vs your labels in labels/phase5/labels.yaml -> labels/phase5/agreement.md
	$(UV) run python -m scoring.labels compare labels/phase5

monitor: ## Run the monitor over runs (Phase 7+)
	@echo "not implemented yet (Phase 7)"; exit 1

analyze: ## Tables and plots (Phase 7+)
	@echo "not implemented yet (Phase 7)"; exit 1
