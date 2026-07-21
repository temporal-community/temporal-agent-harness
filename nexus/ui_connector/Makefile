.PHONY: generate build test test-go test-python test-contract lint lint-python fmt vet workflowcheck ci run-teams-activity-worker install-dev-server start-dev-server

generate:
	npx nexus-rpc-gen@alpha --lang go --package generated \
	  --out-file outbound/driver/temporal_agent_harness/generated/generated.go \
	  ../agent_adapter/nexus_worker/agent.nexusrpc.yaml

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
