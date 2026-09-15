.PHONY: generate generate-from-local install-nexgen build test test-go test-python test-contract lint lint-python fmt vet workflowcheck ci run-teams-activity-worker require-temporal-cli start-dev-server

CONTRACT_URL := https://raw.githubusercontent.com/temporal-community/temporal-agent-harness/main/temporal_agent_harness/nexus_agent_adapter/agent.nexusrpc.yaml

# Installs the newest nex-gen release into `go env GOPATH`/bin, replacing whatever's
# there if a newer tag exists. We're deliberately tracking latest (not pinning) while
# nex-gen is under active development — no backward-compat concerns here yet.
# (Every nex-gen release is marked "pre-release" on GitHub, so /releases/latest 404s;
# we list releases instead and take the first, which is the newest.)
install-nexgen:
	@BINDIR=$$(go env GOPATH)/bin; \
	OS=$$(uname -s); ARCH=$$(uname -m); \
	case "$$OS-$$ARCH" in \
		Darwin-arm64) TARGET=aarch64-apple-darwin ;; \
		Darwin-x86_64) TARGET=x86_64-apple-darwin ;; \
		Linux-aarch64) TARGET=aarch64-unknown-linux-gnu ;; \
		Linux-x86_64) TARGET=x86_64-unknown-linux-gnu ;; \
		*) echo "error: unsupported platform $$OS-$$ARCH for nex-gen" && exit 1 ;; \
	esac; \
	TAG=$$(curl -sL https://api.github.com/repos/temporalio/nex-gen/releases | grep -o '"tag_name": *"[^"]*"' | head -1 | sed -E 's/.*"([^"]+)"$$/\1/'); \
	if [ -z "$$TAG" ]; then echo "error: could not resolve latest nex-gen release from GitHub" && exit 1; fi; \
	if [ -x "$$BINDIR/nexgen" ] && [ "$$($$BINDIR/nexgen --version 2>/dev/null | awk '{print $$2}')" = "$${TAG#v}" ]; then exit 0; fi; \
	URL="https://github.com/temporalio/nex-gen/releases/download/$$TAG/nexgen-$$TAG-$$TARGET.tar.gz"; \
	echo "Installing nex-gen $$TAG ($$TARGET) to $$BINDIR..."; \
	mkdir -p "$$BINDIR" && curl -sL "$$URL" | tar xz -C "$$BINDIR" nexgen && chmod +x "$$BINDIR/nexgen"

# Gets the contract from GitHub main. Use this if the handler is in a different repo.
# nexgen cannot read a URL. This target saves the file first, then generates from it.
generate: install-nexgen
	curl -sL -o agent.nexusrpc.yaml "$(CONTRACT_URL)"
	$$(go env GOPATH)/bin/nexgen go agent.nexusrpc.yaml --output agent/generated

# Gets the contract from the local checkout. Use this in this monorepo.
generate-from-local: install-nexgen
	$$(go env GOPATH)/bin/nexgen go ../../temporal_agent_harness/nexus_agent_adapter/agent.nexusrpc.yaml --output agent/generated

build:
	go build ./...

# Run the Go and Python connector suites.
test: test-go test-python

test-go:
	go test -race -count=1 ./...

test-python:
	cd teams_activity_worker && uv run python -m pytest

# Run only the contract tests; fails loudly if uv is not installed.
test-contract:
	@command -v uv >/dev/null 2>&1 || (echo "error: uv not found — required for contract tests" && exit 1)
	go test -race -count=1 -run 'Contract' ./...

fmt:
	golangci-lint run --fix --timeout=5m

vet:
	go vet ./...

workflowcheck:
	go vet -vettool=$$(go env GOPATH)/bin/workflowcheck ./...

lint:
	golangci-lint run --timeout=5m

lint-python:
	cd teams_activity_worker && uv run python -m ruff check . && uv run python -m pyright --pythonpath .venv/bin/python

run-teams-activity-worker:
	cd teams_activity_worker && uv run teams-activity-worker

ci: vet workflowcheck lint lint-python test

# Minimum `temporal` CLI for the Nexus recipes. Older builds reject the dynamic config
# keys below, so fail here rather than at the first Nexus call.
TEMPORAL_CLI_MIN ?= 1.9.1

require-temporal-cli:
	@have=$$(temporal --version 2>/dev/null | awk '{print $$3}'); \
	if [ -z "$$have" ]; then \
		echo "error: no 'temporal' CLI on PATH. Nexus needs >= $(TEMPORAL_CLI_MIN): https://docs.temporal.io/cli" >&2; exit 1; \
	fi; \
	if [ "$$(printf '%s\n%s\n' "$(TEMPORAL_CLI_MIN)" "$$have" | sort -V | head -1)" != "$(TEMPORAL_CLI_MIN)" ]; then \
		echo "error: temporal CLI $$have is too old. Nexus needs >= $(TEMPORAL_CLI_MIN)." >&2; exit 1; \
	fi

start-dev-server: require-temporal-cli
	temporal server start-dev \
		--dynamic-config-value 'system.maxCallbacksPerWorkflow=500' \
		--dynamic-config-value 'component.nexusoperations.callback.endpoint.template="http://localhost:7243/namespaces/{{.NamespaceName}}/nexus/callback"' \
		--dynamic-config-value 'callback.allowedAddresses=[{"Pattern":"*","AllowInsecure":true}]' \
		--dynamic-config-value 'history.enableUpdateCallbacks=true' \
		--dynamic-config-value 'nexusoperation.enableStandalone=true' \
		--dynamic-config-value 'activity.enableStandalone=true'
