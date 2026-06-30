.PHONY: generate build test test-contract vet

generate:
	npx nexus-rpc-gen@alpha --lang go --package handler \
	  --out-file handler/generated.go \
	  agent.nexusrpc.yaml

build:
	go build ./...

vet:
	go vet ./...

# Run all tests. Contract tests (requiring uv/Python) skip gracefully if unavailable.
test:
	go test -race -count=1 ./...

# Run only the contract tests; fails loudly if uv is not installed.
test-contract:
	@command -v uv >/dev/null 2>&1 || (echo "error: uv not found — required for contract tests" && exit 1)
	go test -race -count=1 -run 'Contract' ./...
