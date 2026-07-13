# Hunting container for the AI-assisted bug bounty framework.
# Base: Kali rolling, but we DO NOT install the giant kali-linux-* metapackages.
# We install a curated, lean toolset only — see docs/PLAN.md Phase 0.
#
# Build:  docker compose build         (from tooling/docker/)
# Run:    docker compose run --rm hunter
#
# NOTE: the `go install` and `git clone` steps below need NETWORK ACCESS at
#       build time. If you build offline they will fail.
# NOTE: wordlists (SecLists/Assetnote) and nuclei-templates are NOT baked into
#       the image. Wordlists are a mounted volume (/opt/wordlists); nuclei
#       templates are updated at container start / on demand (see README).

FROM kalilinux/kali-rolling

LABEL org.opencontainers.image.title="bb-hunter" \
      org.opencontainers.image.description="Lean Kali-based hunting box (web/Android/Linux/infra)" \
      org.opencontainers.image.source="https://github.com/danjo133"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# ---------------------------------------------------------------------------
# 0) Pin a reliable Kali mirror. The default http.kali.org redirector geo-routes
#    to a nearby mirror that is sometimes down/flaky (e.g. mirror.accum.se),
#    which fails `apt-get update`. kali.download is Cloudflare-backed and stable.
#    Swap this line for a specific mirror you trust if you prefer.
# ---------------------------------------------------------------------------
RUN printf 'deb http://kali.download/kali kali-rolling main contrib non-free non-free-firmware\n' \
        > /etc/apt/sources.list

# ---------------------------------------------------------------------------
# 1) APT layer — curated system packages only (keep this first for cache hits).
# ---------------------------------------------------------------------------
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git \
        curl \
        wget \
        jq \
        python3 \
        python3-pip \
        pipx \
        golang-go \
        ca-certificates \
        dnsutils \
        nmap \
        apktool \
        jadx \
        default-jre \
        unzip \
        build-essential \
        libpcap-dev \
        ripgrep \
        nodejs \
        npm \
        gron \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# 2) Non-root hunter user + shared dirs.
#    /workspace = repo mount, /opt/wordlists = wordlist volume, /opt/go = GOPATH.
# ---------------------------------------------------------------------------
ENV GOPATH=/opt/go \
    GOBIN=/opt/go/bin \
    PATH=/opt/go/bin:/home/hunter/.local/bin:/usr/local/go/bin:$PATH

RUN useradd -m -s /bin/bash hunter && \
    mkdir -p /workspace /opt/wordlists /opt/go/bin /home/hunter/.local/bin && \
    chown -R hunter:hunter /workspace /opt/wordlists /opt/go /home/hunter

# ---------------------------------------------------------------------------
# 3) Go tools (latest). Needs network. Grouped so a single failure is easy to
#    spot in the build log. Canonical module paths only — do not "guess" paths.
#    GOFLAGS keeps the cache warm; CGO is needed by some (naabu uses libpcap).
# ---------------------------------------------------------------------------
ENV GOFLAGS=-buildvcs=false \
    CGO_ENABLED=1

# ProjectDiscovery core
RUN go install -v github.com/projectdiscovery/subfinder/v2/cmd/subfinder@latest && \
    go install -v github.com/projectdiscovery/dnsx/cmd/dnsx@latest && \
    go install -v github.com/projectdiscovery/naabu/v2/cmd/naabu@latest && \
    go install -v github.com/projectdiscovery/httpx/cmd/httpx@latest && \
    go install -v github.com/projectdiscovery/katana/cmd/katana@latest && \
    go install -v github.com/projectdiscovery/nuclei/v3/cmd/nuclei@latest && \
    go install -v github.com/projectdiscovery/chaos-client/cmd/chaos@latest

# Fuzzing / XSS / URL discovery / JS
RUN go install -v github.com/ffuf/ffuf/v2@latest && \
    go install -v github.com/lc/gau/v2/cmd/gau@latest && \
    go install -v github.com/hahwul/dalfox/v2@latest && \
    go install -v github.com/BishopFox/jsluice/cmd/jsluice@latest && \
    go install -v github.com/tomnomnom/waybackurls@latest && \
    go install -v github.com/tomnomnom/assetfinder@latest

# Subdomain enum extras: amass (v4) + puredns (needs massdns at runtime — see README)
RUN go install -v github.com/owasp-amass/amass/v4/...@master && \
    go install -v github.com/d3mondev/puredns/v2@latest

# Orchestration: go-task (`task` runner used by the framework's pipelines).
RUN go install -v github.com/go-task/task/v3/cmd/task@latest

# ---------------------------------------------------------------------------
# 4) Secret scanning — trufflehog is GO, install the official binary (NOT pipx).
# ---------------------------------------------------------------------------
RUN curl -sSfL https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/scripts/install.sh \
        | sh -s -- -b /usr/local/bin

# ---------------------------------------------------------------------------
# 4b) hurl — plain-text HTTP request runner (PoC + validator-replay format).
#     Prefer the distro package; fall back to the upstream static release.
#     Bump HURL_VERSION to upgrade the release fallback.
# ---------------------------------------------------------------------------
ENV HURL_VERSION=6.1.1
RUN if apt-get update && apt-get install -y --no-install-recommends hurl; then \
        rm -rf /var/lib/apt/lists/*; \
    else \
        rm -rf /var/lib/apt/lists/*; \
        arch="$(uname -m)"; \
        case "$arch" in \
            x86_64)  htriple="x86_64-unknown-linux-gnu" ;; \
            aarch64) htriple="aarch64-unknown-linux-gnu" ;; \
            *) echo "unsupported arch for hurl: $arch" >&2; exit 1 ;; \
        esac; \
        curl -sSfL \
            "https://github.com/Orange-OpenSource/hurl/releases/download/${HURL_VERSION}/hurl-${HURL_VERSION}-${htriple}.tar.gz" \
            -o /tmp/hurl.tar.gz; \
        tar -xzf /tmp/hurl.tar.gz -C /tmp; \
        install -m 0755 "/tmp/hurl-${HURL_VERSION}-${htriple}/bin/hurl"  /usr/local/bin/hurl; \
        install -m 0755 "/tmp/hurl-${HURL_VERSION}-${htriple}/bin/hurlfmt" /usr/local/bin/hurlfmt; \
        rm -rf /tmp/hurl.tar.gz "/tmp/hurl-${HURL_VERSION}-${htriple}"; \
    fi

# ---------------------------------------------------------------------------
# 4c) JS deobfuscation / unminification + tree-sitter (npm globals).
#     Needs NETWORK at build time. Kept in one layer; a single package failure
#     is visible in the build log. tree-sitter-cli is the source-grammar CLI;
#     the rest reverse webpack/minified/obfuscated bundles.
# ---------------------------------------------------------------------------
RUN npm install -g \
        tree-sitter-cli \
        webcrack \
        js-beautify \
        @wakaru/unminify \
        @wakaru/unpacker \
        restringer

# ---------------------------------------------------------------------------
# 5) Python tooling. Use pipx for the pip-installable, isolated CLIs.
#    sqlmap is distributed as a git repo (not a clean pip CLI) — clone it and
#    symlink the entrypoint. objection/mitmproxy/apkleaks are pipx-friendly.
#    PIP_BREAK_SYSTEM_PACKAGES guards against PEP-668 if pipx falls back.
# ---------------------------------------------------------------------------
USER hunter
ENV PIPX_HOME=/home/hunter/.local/pipx \
    PIPX_BIN_DIR=/home/hunter/.local/bin

RUN pipx install apkleaks && \
    pipx install objection && \
    pipx install mitmproxy && \
    pipx install semgrep && \
    pipx install ast-grep-cli   # ast-grep CLI (binaries: ast-grep / sg). pipx variant chosen over npm @ast-grep/cli to keep it isolated.

# sqlmap from source (official distribution method).
RUN git clone --depth 1 https://github.com/sqlmapproject/sqlmap.git \
        /home/hunter/.local/sqlmap && \
    ln -sf /home/hunter/.local/sqlmap/sqlmap.py /home/hunter/.local/bin/sqlmap

# ---------------------------------------------------------------------------
# 6) Final environment.
#    nuclei-templates are intentionally NOT fetched here (network + freshness).
#    Run `nuclei -update-templates` at container start or on demand (README).
# ---------------------------------------------------------------------------
WORKDIR /workspace

# Sanity banner on shell start so the operator knows what's missing/mounted.
RUN printf '%s\n' \
    'echo "[bb-hunter] tools on PATH: subfinder dnsx naabu httpx katana nuclei ffuf gau dalfox jsluice chaos amass puredns waybackurls assetfinder trufflehog sqlmap apkleaks objection mitmproxy nmap jadx apktool task hurl ripgrep gron semgrep ast-grep tree-sitter webcrack js-beautify restringer"' \
    'echo "[bb-hunter] wordlists -> /opt/wordlists (mounted volume). Populate via README."' \
    'echo "[bb-hunter] run nuclei -update-templates if templates are missing."' \
    >> /home/hunter/.bashrc

CMD ["/bin/bash"]
