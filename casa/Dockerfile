# Multi-arch Debian base (glibc, ships s6-overlay + bashio). The default keeps
# Supervisor local builds working now that build.yaml is retired upstream and
# Supervisor ≥2026.04 no longer injects BUILD_FROM; under buildx the same
# manifest resolves to the right per-platform layer. We migrated off
# alpine-python:3.12 in v0.8.1 for glibc wheels (onnxruntime, since retired);
# the glibc base is kept.
ARG BUILD_FROM=ghcr.io/home-assistant/base-debian:bookworm
FROM ${BUILD_FROM}

# #1268: every core s6 script runs through /opt/casa/scripts/core-bashio.sh,
# which execs `/bin/bash /usr/bin/bashio` by absolute path, and the bashio
# launcher finds its library through the link's target. A base that moves
# either path would stop every core service at boot, and CI's e2e image swaps
# these scripts for its own, so the build checks the layout here.
RUN test "$(readlink -f /usr/bin/bashio)" = /usr/lib/bashio/bashio \
    && test -f /usr/lib/bashio/bashio.sh \
    || { echo "bashio not found at /usr/bin/bashio -> /usr/lib/bashio/bashio (base image layout changed; see /opt/casa/scripts/core-bashio.sh)" >&2; exit 1; }

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         python3 python3-pip python3-venv \
         nodejs npm git nginx-light jq yq curl ca-certificates unzip \
         openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@2.1.293 \
    && npm cache clean --force

# Install ttyd (not packaged in Debian).
# GitHub's release CDN failed this fetch twice in a row on one QA run — curl
# 56 "connection died", then an HTTP error — while the asset served 200 from
# outside Actions. --retry alone would not have covered the first: curl's
# default retry set is transient HTTP statuses only, and --retry-all-errors
# is what extends it to transport errors. --retry-max-time bounds the whole
# retry loop: a 429/5xx carrying Retry-After overrides --retry-delay (measured
# on this base image's curl 7.88.1), so without a cap a rate-limiting server
# could stall every build for as long as it likes. The same flags are on every
# release-asset fetch below (#545), since the failure mode is not ttyd's.
ARG TTYD_VERSION=1.7.7
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
        amd64) TTYD_ARCH="x86_64" ;; \
        arm64) TTYD_ARCH="aarch64" ;; \
        *) echo "Unsupported arch: ${TARGETARCH}" && exit 1 ;; \
    esac \
    && curl -fsSL --retry 8 --retry-all-errors --retry-delay 2 --retry-max-time 120 \
       "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${TTYD_ARCH}" \
       -o /usr/bin/ttyd \
    && chmod +x /usr/bin/ttyd

# gh CLI: plugin_store resolves plugin refs and releases with `gh api`
ARG GH_CLI_VERSION=2.45.0
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) GH_ARCH="linux_amd64" ;; \
        arm64) GH_ARCH="linux_arm64" ;; \
        *) echo "unsupported arch $(dpkg --print-architecture)"; exit 1 ;; \
    esac; \
    curl -fsSL --retry 8 --retry-all-errors --retry-delay 2 --retry-max-time 120 -o /tmp/gh.tar.gz \
      "https://github.com/cli/cli/releases/download/v${GH_CLI_VERSION}/gh_${GH_CLI_VERSION}_${GH_ARCH}.tar.gz"; \
    tar -xzf /tmp/gh.tar.gz -C /tmp; \
    mv /tmp/gh_${GH_CLI_VERSION}_${GH_ARCH}/bin/gh /usr/local/bin/; \
    rm -rf /tmp/gh*; \
    gh --version

# 1Password op CLI for secret resolution (§8 universal resolver)
ARG OP_CLI_VERSION=2.30.0
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) OP_ARCH="amd64" ;; \
        arm64) OP_ARCH="arm64" ;; \
        *) echo "unsupported arch $(dpkg --print-architecture)"; exit 1 ;; \
    esac; \
    curl -fsSL --retry 8 --retry-all-errors --retry-delay 2 --retry-max-time 120 -o /tmp/op.zip \
      "https://cache.agilebits.com/dist/1P/op2/pkg/v${OP_CLI_VERSION}/op_linux_${OP_ARCH}_v${OP_CLI_VERSION}.zip"; \
    unzip /tmp/op.zip -d /usr/local/bin; \
    rm /tmp/op.zip; \
    op --version

# Python dependencies live in a venv under /opt/casa/venv — Debian
# 12's system Python is PEP 668 "externally managed"; the venv keeps
# pip off the system site-packages without --break-system-packages.
# All runtime services invoke Python via /opt/casa/venv/bin/python3
# (put on PATH below).
RUN python3 -m venv /opt/casa/venv
ENV PATH="/opt/casa/venv/bin:${PATH}"

# The memory store's search runs on SQLite FTS5 (memory/store.py checks it
# again at every boot). The interpreter Casa runs must be able to create and
# query an FTS5 table, or the image does not build.
RUN /opt/casa/venv/bin/python3 -c 'import sqlite3; c = sqlite3.connect(":memory:"); c.execute("CREATE VIRTUAL TABLE t USING fts5(body)"); c.execute("INSERT INTO t (body) VALUES (?)", ("probe",)); n = c.execute("SELECT count(*) FROM t WHERE t MATCH ?", ("probe",)).fetchone()[0]; assert n == 1, n; print("SQLite", sqlite3.sqlite_version, "has FTS5")' \
    || { echo "this image's SQLite cannot create an FTS5 table; the memory store needs it" >&2; exit 1; }

COPY requirements.txt /tmp/
RUN /opt/casa/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# Copy application (frequently-changing layer, kept last).
COPY rootfs /

# Set permissions for s6-rc services and scripts
RUN chmod +x /etc/s6-overlay/scripts/*.sh \
    && find /etc/s6-overlay/s6-rc.d -name 'run' -o -name 'finish' \
    | xargs chmod +x \
    && chmod +x /opt/casa/scripts/*.sh \
    && chmod +x /usr/local/bin/casactl

# #957: Casa's service database compiles in this image. At every container
# start the base's stage2 (s6-overlay's rc.init) compiles the overlay's own
# sources, Casa's service definitions in /etc/s6-overlay/s6-rc.d and the MERGED
# /etc/s6-overlay/user-bundles.d; a set that does not compile starts none of
# Casa's services. Since s6-overlay 3.2.3.2 (#1357) the base ships the `user`
# and `user2` bundles in user-bundles.d and Casa only adds members to `user`,
# so Casa's half does not compile alone and this must follow `COPY rootfs /`
# and the permissions above. A `user` directory left in s6-rc.d fails here too
# (a duplicate bundle, or one with no type). BUILD_FROM floats on a tag, so
# this runs against the base the image is actually built on. The database is
# thrown away inside this layer.
RUN scratch="$(mktemp -d)" \
    && s6-rc-compile "${scratch}/db" /package/admin/s6-overlay/etc/s6-rc/sources /etc/s6-overlay/s6-rc.d /etc/s6-overlay/user-bundles.d \
    && rm -rf "${scratch}" \
    || { echo "Casa's s6-rc service database does not compile in this image (the sources stage2 compiles at boot)" >&2; exit 1; }

# io.hass.* labels are injected by the deploy workflow (build-image action).
LABEL org.opencontainers.image.title="Casa" \
      org.opencontainers.image.description="Personal AI agent framework powered by Claude" \
      org.opencontainers.image.source="https://github.com/bonzanni/ha-casa-app" \
      org.opencontainers.image.licenses="MIT"
