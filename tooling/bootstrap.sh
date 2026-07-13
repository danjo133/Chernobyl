#!/usr/bin/env bash
#
# bootstrap.sh — idempotent installer for the bug bounty hunting toolset.
#
# Mirrors tooling/docker/Dockerfile so a bare Kali VM gets the same tools as the
# container. Safe to re-run: already-installed tools are detected and skipped.
#
# Usage:
#   ./bootstrap.sh            install/repair the toolset (skips what's present)
#   ./bootstrap.sh --update   refresh go tools + nuclei-templates (and reinstall)
#   ./bootstrap.sh --help
#
# Requires: a Debian/Kali host with sudo (for apt). Go tools install into
# $GOPATH/bin for the invoking user — no root needed for those.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config / flags
# ---------------------------------------------------------------------------
UPDATE=0
for arg in "$@"; do
  case "$arg" in
    --update) UPDATE=1 ;;
    -h|--help)
      grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'
      exit 0
      ;;
    *)
      echo "unknown flag: $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

# GOPATH / PATH wiring so go-installed binaries are found this run.
export GOPATH="${GOPATH:-$HOME/go}"
export GOBIN="${GOBIN:-$GOPATH/bin}"
export PATH="$GOBIN:$HOME/.local/bin:/usr/local/go/bin:$PATH"
export PIP_BREAK_SYSTEM_PACKAGES=1
export DEBIAN_FRONTEND=noninteractive

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
fi

section() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info()    { printf '    %s\n' "$*"; }
have()    { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# 1) APT packages (curated — NOT the kali-linux-* metapackages)
# ---------------------------------------------------------------------------
APT_PKGS="git curl wget jq python3 python3-pip pipx golang-go ca-certificates \
dnsutils nmap apktool jadx default-jre unzip build-essential libpcap-dev massdns \
ripgrep nodejs npm gron"

section "APT system packages"
MISSING=""
for pkg in $APT_PKGS; do
  if dpkg -s "$pkg" >/dev/null 2>&1; then
    info "ok: $pkg"
  else
    MISSING="$MISSING $pkg"
  fi
done
if [ -n "${MISSING# }" ]; then
  info "installing:$MISSING"
  $SUDO apt-get update
  # shellcheck disable=SC2086
  $SUDO apt-get install -y --no-install-recommends $MISSING
else
  info "all apt packages already present"
fi

# Ensure pipx PATH is registered for the user (idempotent).
have pipx && pipx ensurepath >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# 2) Go tools — "binary => module path". Canonical paths only.
# ---------------------------------------------------------------------------
export GOFLAGS="-buildvcs=false"
export CGO_ENABLED=1

# Each line: <binary name> <go module path with @version>
GO_TOOLS="
subfinder|github.com/projectdiscovery/subfinder/v2/cmd/subfinder@latest
dnsx|github.com/projectdiscovery/dnsx/cmd/dnsx@latest
naabu|github.com/projectdiscovery/naabu/v2/cmd/naabu@latest
httpx|github.com/projectdiscovery/httpx/cmd/httpx@latest
katana|github.com/projectdiscovery/katana/cmd/katana@latest
nuclei|github.com/projectdiscovery/nuclei/v3/cmd/nuclei@latest
chaos|github.com/projectdiscovery/chaos-client/cmd/chaos@latest
ffuf|github.com/ffuf/ffuf/v2@latest
gau|github.com/lc/gau/v2/cmd/gau@latest
dalfox|github.com/hahwul/dalfox/v2@latest
jsluice|github.com/BishopFox/jsluice/cmd/jsluice@latest
waybackurls|github.com/tomnomnom/waybackurls@latest
assetfinder|github.com/tomnomnom/assetfinder@latest
amass|github.com/owasp-amass/amass/v4/...@master
puredns|github.com/d3mondev/puredns/v2@latest
task|github.com/go-task/task/v3/cmd/task@latest
"

section "Go tools (GOBIN=$GOBIN)"
if ! have go; then
  echo "go not found on PATH after apt install — aborting go tool phase" >&2
  exit 1
fi

echo "$GO_TOOLS" | while IFS='|' read -r bin path; do
  [ -z "$bin" ] && continue
  if [ "$UPDATE" -eq 1 ] || ! have "$bin"; then
    info "installing $bin ($path)"
    go install -v "$path"
  else
    info "ok: $bin"
  fi
done

# ---------------------------------------------------------------------------
# 3) trufflehog — Go binary via the official install script (NOT pipx)
# ---------------------------------------------------------------------------
section "trufflehog (secret scanner)"
if [ "$UPDATE" -eq 1 ] || ! have trufflehog; then
  info "installing trufflehog -> $HOME/.local/bin"
  mkdir -p "$HOME/.local/bin"
  curl -sSfL https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/scripts/install.sh \
    | sh -s -- -b "$HOME/.local/bin"
else
  info "ok: trufflehog"
fi

# ---------------------------------------------------------------------------
# 3b) hurl — plain-text HTTP request runner (PoC + validator-replay format).
#     Prefer the distro package; fall back to the upstream static release.
#     Bump HURL_VERSION to upgrade the release fallback.
# ---------------------------------------------------------------------------
HURL_VERSION="6.1.1"
section "hurl (HTTP request runner)"
if [ "$UPDATE" -eq 1 ] || ! have hurl; then
  if $SUDO apt-get install -y --no-install-recommends hurl >/dev/null 2>&1; then
    info "installed hurl via apt"
  else
    info "apt hurl unavailable — installing release binary $HURL_VERSION"
    arch="$(uname -m)"
    case "$arch" in
      x86_64)  htriple="x86_64-unknown-linux-gnu" ;;
      aarch64) htriple="aarch64-unknown-linux-gnu" ;;
      *) echo "unsupported arch for hurl: $arch" >&2; exit 1 ;;
    esac
    tmpd="$(mktemp -d)"
    curl -sSfL \
      "https://github.com/Orange-OpenSource/hurl/releases/download/${HURL_VERSION}/hurl-${HURL_VERSION}-${htriple}.tar.gz" \
      -o "$tmpd/hurl.tar.gz"
    tar -xzf "$tmpd/hurl.tar.gz" -C "$tmpd"
    $SUDO install -m 0755 "$tmpd/hurl-${HURL_VERSION}-${htriple}/bin/hurl"    /usr/local/bin/hurl
    $SUDO install -m 0755 "$tmpd/hurl-${HURL_VERSION}-${htriple}/bin/hurlfmt" /usr/local/bin/hurlfmt
    rm -rf "$tmpd"
  fi
else
  info "ok: hurl"
fi

# ---------------------------------------------------------------------------
# 3c) JS deobfuscation / unminification + tree-sitter (npm globals).
#     Needs network. tree-sitter-cli is the source-grammar CLI; the rest
#     reverse webpack/minified/obfuscated bundles. Installed one-by-one so a
#     single package failure is debuggable.
# ---------------------------------------------------------------------------
section "npm global tools (deobfuscation / tree-sitter)"
if have npm; then
  # binary name => npm package
  NPM_TOOLS="
tree-sitter|tree-sitter-cli
webcrack|webcrack
js-beautify|js-beautify
wakaru-unminify|@wakaru/unminify
wakaru-unpacker|@wakaru/unpacker
restringer|restringer
"
  echo "$NPM_TOOLS" | while IFS='|' read -r bin pkg; do
    [ -z "$bin" ] && continue
    if [ "$UPDATE" -eq 1 ] || ! have "$bin"; then
      info "installing $pkg"
      $SUDO npm install -g "$pkg" || info "npm install $pkg failed (check network)"
    else
      info "ok: $bin"
    fi
  done
else
  info "npm not found — skipping JS deobfuscation tools"
fi

# ---------------------------------------------------------------------------
# 4) Python CLIs via pipx (apkleaks, objection, mitmproxy, semgrep, ast-grep)
# ---------------------------------------------------------------------------
section "pipx Python tools"
pipx_install() {
  local name="$1"
  if [ "$UPDATE" -eq 1 ] && pipx list 2>/dev/null | grep -q "package $name "; then
    info "upgrading $name"
    pipx upgrade "$name" >/dev/null
  elif have "$name" || pipx list 2>/dev/null | grep -q "package $name "; then
    info "ok: $name"
  else
    info "installing $name"
    pipx install "$name"
  fi
}
pipx_install apkleaks
pipx_install objection
pipx_install mitmproxy
pipx_install semgrep
# ast-grep CLI (binaries: ast-grep / sg). pipx variant chosen over npm @ast-grep/cli.
pipx_install ast-grep-cli

# ---------------------------------------------------------------------------
# 5) sqlmap from source (official distribution method)
# ---------------------------------------------------------------------------
section "sqlmap (from source)"
SQLMAP_DIR="$HOME/.local/sqlmap"
if [ -d "$SQLMAP_DIR/.git" ]; then
  if [ "$UPDATE" -eq 1 ]; then
    info "updating sqlmap"
    git -C "$SQLMAP_DIR" pull --ff-only
  else
    info "ok: sqlmap"
  fi
else
  info "cloning sqlmap"
  git clone --depth 1 https://github.com/sqlmapproject/sqlmap.git "$SQLMAP_DIR"
fi
mkdir -p "$HOME/.local/bin"
ln -sf "$SQLMAP_DIR/sqlmap.py" "$HOME/.local/bin/sqlmap"

# ---------------------------------------------------------------------------
# 6) nuclei templates (network-dependent; always refreshed on --update)
# ---------------------------------------------------------------------------
section "nuclei templates"
if have nuclei; then
  if [ "$UPDATE" -eq 1 ] || [ ! -d "$HOME/.local/nuclei-templates" ] && [ ! -d "$HOME/nuclei-templates" ]; then
    info "running nuclei -update-templates"
    nuclei -update-templates || info "template update failed (check network)"
  else
    info "templates present (run with --update to refresh)"
  fi
else
  info "nuclei not installed — skipping template update"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
section "Done"
info "Ensure these are on your PATH (add to ~/.bashrc if missing):"
info "  export PATH=\"\$HOME/go/bin:\$HOME/.local/bin:\$PATH\""
info "Populate wordlists into /opt/wordlists (see tooling/README.md)."
info "Set CHAOS_API_KEY / GITHUB_TOKEN / SHODAN_API_KEY for full functionality."
