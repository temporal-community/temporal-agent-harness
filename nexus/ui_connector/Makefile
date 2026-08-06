.PHONY: generate generate-from-local install-nexgen build test test-go test-python test-contract lint lint-python fmt vet workflowcheck ci run-teams-activity-worker install-dev-server start-dev-server

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

TEMPORAL_SERVER_REPO ?= https://github.com/temporalio/temporal.git
TEMPORAL_SERVER_REF ?= main

install-dev-server:
	@BUILDDIR=$$(pwd)/.build/temporal-server-src && \
	if [ ! -d $$BUILDDIR ]; then \
		echo "Cloning $(TEMPORAL_SERVER_REPO)@$(TEMPORAL_SERVER_REF)..." && \
		mkdir -p $$(dirname $$BUILDDIR) && \
		git clone --depth=1 -b $(TEMPORAL_SERVER_REF) $(TEMPORAL_SERVER_REPO) $$BUILDDIR; \
	fi && \
	cd $$BUILDDIR && \
	echo "Building temporal-server..." && \
	GOWORK=off GOFLAGS= go build -o $$(go env GOPATH)/bin/temporal-server ./cmd/server && \
	echo "Installed temporal-server to $$(go env GOPATH)/bin/temporal-server"

start-dev-server:
	temporal-server --config-file ../devserver/config.yaml --allow-no-auth start
