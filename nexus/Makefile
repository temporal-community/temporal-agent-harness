.PHONY: test test-integration test-agent-registry-nexus test-all

MODULES := agent_adapter/nexus_worker slack_connector integration_tests

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

# Real-Nexus-wire test of the agent registry's client-side self-registration helper (Python).
# Not part of the root `pytest`/`uv run pytest` run (nexus/tests/ isn't in testpaths) since it
# needs a standalone-Nexus-capable temporal-server binary — see nexus/tests/conftest.py. Build
# that binary once with `make -C slack_connector install-dev-server`.
test-agent-registry-nexus:
	cd .. && uv run pytest nexus/tests -v

# Everything: all modules' default suites, then the integration suite.
test-all: test test-integration
