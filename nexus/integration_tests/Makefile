.PHONY: build test-integration

# Build check — does not run tests.
build:
	go build -tags integration ./...

# Run all durability integration tests against an embedded Temporal dev server.
# Downloads the temporal CLI binary on first run (~20 MB, cached afterwards).
# Run from the repo root so the go.work workspace resolves workspace modules.
test-integration:
	go test -tags integration -timeout 300s -race -count=1 ./...
