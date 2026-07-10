.PHONY: generate build build-lambda lambda-build test test-contract vet

generate:
	npx nexus-rpc-gen@alpha --lang go --package handler \
	  --out-file handler/generated.go \
	  agent.nexusrpc.yaml

build:
	go build ./...

# Build an AWS Lambda deployment artifact for a cmd/<name> binary.
# Produces build/<CMD>/bootstrap and build/<CMD>/<CMD>.zip.
#
# LAMBDA_ARCH MUST match `Architectures` in deploy/worker-lambda.cfn.yaml (arm64).
# A mismatch builds and packages fine but fails at invocation with "exec format error".
# GOWORK=off so the artifact resolves against this module's go.mod, not the workspace.
LAMBDA_ARCH ?= arm64

# Reusable: `make lambda-build CMD=worker-lambda`
lambda-build:
	mkdir -p build/$(CMD)
	GOWORK=off GOOS=linux GOARCH=$(LAMBDA_ARCH) CGO_ENABLED=0 \
	  go build -tags lambda.norpc -ldflags="-s -w" -o build/$(CMD)/bootstrap ./cmd/$(CMD)
	cd build/$(CMD) && rm -f $(CMD).zip && zip -q -X $(CMD).zip bootstrap
	@echo "Built build/$(CMD)/$(CMD).zip (GOARCH=$(LAMBDA_ARCH))"

build-lambda:
	$(MAKE) lambda-build CMD=worker-lambda

vet:
	go vet ./...

# Run all tests. Contract tests (requiring uv/Python) skip gracefully if unavailable.
test:
	go test -race -count=1 ./...

# Run only the contract tests; fails loudly if uv is not installed.
test-contract:
	@command -v uv >/dev/null 2>&1 || (echo "error: uv not found — required for contract tests" && exit 1)
	go test -race -count=1 -run 'Contract' ./...
