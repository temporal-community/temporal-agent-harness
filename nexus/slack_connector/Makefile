.PHONY: generate build build-lambda test test-contract lint fmt vet workflowcheck ci install-dev-server start-dev-server

generate:
	npx nexus-rpc-gen@alpha --lang go --package agent \
	  --out-file agent/generated/generated.go \
	  ../agent_adapter/nexus_worker/agent.nexusrpc.yaml

build:
	go build ./...

# Build the AWS Lambda deployment artifact for cmd/worker-lambda.
# Produces $(LAMBDA_BUILD_DIR)/bootstrap and $(LAMBDA_BUILD_DIR)/worker-lambda.zip.
#
# LAMBDA_ARCH MUST match `Architectures` in deploy/worker-lambda.cfn.yaml (arm64).
# A mismatch builds and packages fine but fails at invocation with "exec format error".
# GOWORK=off so the artifact resolves against this module's go.mod, not the workspace.
LAMBDA_ARCH ?= arm64
LAMBDA_BUILD_DIR ?= build/worker-lambda
build-lambda:
	mkdir -p $(LAMBDA_BUILD_DIR)
	GOWORK=off GOOS=linux GOARCH=$(LAMBDA_ARCH) CGO_ENABLED=0 \
	  go build -tags lambda.norpc -ldflags="-s -w" -o $(LAMBDA_BUILD_DIR)/bootstrap ./cmd/worker-lambda
	cd $(LAMBDA_BUILD_DIR) && rm -f worker-lambda.zip && zip -q -X worker-lambda.zip bootstrap
	@echo "Built $(LAMBDA_BUILD_DIR)/worker-lambda.zip (GOARCH=$(LAMBDA_ARCH))"

# Run all tests. Contract tests (requiring uv/Python) skip gracefully if unavailable.
test:
	go test -race -count=1 ./...

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

ci: vet workflowcheck lint test

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
