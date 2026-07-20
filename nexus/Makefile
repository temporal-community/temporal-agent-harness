.PHONY: test test-integration test-all

MODULES := agent_adapter/nexus_worker ui_connector integration_tests

# Run every workspace module's default suite (race, no build tags). Anchored
# per-module because `nexus/` itself isn't a `use`d module in go.work, so a
# bare `./...` from here doesn't resolve.
test:
	@for m in $(MODULES); do \
		echo "== $$m =="; \
		(cd $$m && go test -race -count=1 ./...) || exit 1; \
	done

# Run only the tagged durability integration suite (embedded dev server).
test-integration:
	cd integration_tests && go test -tags integration -timeout 300s -race -count=1 ./...

# Everything: all modules' default suites, then the integration suite.
test-all: test test-integration
