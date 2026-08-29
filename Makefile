# Agent Sandbox — build, install, uninstall.
#
#   make install                    per-user install into ~/.local (no root)
#   make install PREFIX=/usr/local  system-wide, shared by every user (needs root)
#   make uninstall                  remove it again (leaves your sandboxes/state alone)
#   make check                      report on host prerequisites
#   make test                       run the FUSE filter's unit tests
#
# The install tree is READ-ONLY at runtime: everything the CLI writes goes to
# $SANDBOX_STATE_DIR (default $XDG_STATE_HOME/agent-sandbox, i.e. ~/.local/state/
# agent-sandbox), which is why one install can be shared across users.

PREFIX  ?= $(HOME)/.local
DESTDIR ?=

pkgname := agent-sandbox
bindir  := $(DESTDIR)$(PREFIX)/bin
libdir  := $(DESTDIR)$(PREFIX)/lib/$(pkgname)
# The symlink must point at the FINAL path, not the staging one, so DESTDIR builds
# (packaging) still resolve correctly on the target machine.
linkdst := $(PREFIX)/lib/$(pkgname)/sandbox

FUSE_SRC := .devcontainer/fusefilter
FUSE_BIN := $(FUSE_SRC)/gitignore-fuse

# What a working install needs at runtime. `tooling/` is included because the
# bb-hunter image, SearXNG and litellm are all deployed from it.
PAYLOAD := sandbox broker .devcontainer tooling docs README.md SECURITY.md

# Per-sandbox state that older versions wrote into the source tree, plus build
# droppings. Never copy these into an install.
EXCLUDES := --exclude=.broker-control-\* --exclude=.allowlist-\*.txt \
            --exclude=.tools-\* --exclude=.env --exclude=__pycache__ \
            --exclude=\*.pyc --exclude=.venv --exclude=node_modules

.PHONY: all build install uninstall check test clean

all: build

## Build the gitignore FUSE filter. CGO off: go-fuse is pure Go, so the binary
## stays static and builds without a C toolchain.
build: $(FUSE_BIN)

$(FUSE_BIN): $(FUSE_SRC)/go.mod $(wildcard $(FUSE_SRC)/*.go)
	@command -v go >/dev/null 2>&1 || { \
	  echo "make: need 'go' to build $(FUSE_BIN) — install Go, or run 'nix shell nixpkgs#go'" >&2; exit 1; }
	cd $(FUSE_SRC) && CGO_ENABLED=0 go build -o gitignore-fuse .

test:
	cd $(FUSE_SRC) && go test ./...

install: build
	@install -d "$(libdir)" "$(bindir)"
	@tar -cf - $(EXCLUDES) $(PAYLOAD) | tar -xf - -C "$(libdir)"
	@chmod +x "$(libdir)/sandbox" "$(libdir)/.devcontainer/fusefilter/"*.sh \
	          "$(libdir)/.devcontainer/"*.sh "$(libdir)/tooling/"*.sh 2>/dev/null || true
	@ln -sfn "$(linkdst)" "$(bindir)/sandbox"
	@echo "installed: $(libdir)"
	@echo "           $(bindir)/sandbox -> $(linkdst)"
	@case ":$$PATH:" in *":$(PREFIX)/bin:"*) ;; \
	  *) echo "NOTE: $(PREFIX)/bin is not on your PATH — add it to your shell rc." ;; esac

uninstall:
	@rm -f "$(bindir)/sandbox"
	@rm -rf "$(libdir)"
	@echo "removed: $(libdir) and $(bindir)/sandbox"
	@echo "kept:    your sandboxes/state and harness logins (see 'sandbox ls')"

check:
	@./install.sh --check

clean:
	@rm -f $(FUSE_BIN)
