.PHONY: generate install-nexgen

# Installs the newest nex-gen release into ~/.local/bin, replacing whatever's there if a
# newer tag exists. We're deliberately tracking latest (not pinning) while nex-gen is
# under active development — no backward-compat concerns here yet.
# (Every nex-gen release is marked "pre-release" on GitHub, so /releases/latest 404s;
# we list releases instead and take the first, which is the newest.)
install-nexgen:
	@BINDIR="$$HOME/.local/bin"; \
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

# Regenerates the RegistryService Python bindings.
generate: install-nexgen
	"$$HOME/.local/bin/nexgen" python registry.nexusrpc.yaml --output generated
