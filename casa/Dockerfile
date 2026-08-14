# Multi-arch Debian base (glibc, ships s6-overlay + bashio). The default keeps
# Supervisor local builds working now that build.yaml is retired upstream and
# Supervisor ≥2026.04 no longer injects BUILD_FROM; under buildx the same
# manifest resolves to the right per-platform layer. We migrated off
# alpine-python:3.12 in v0.8.1 for glibc wheels (onnxruntime, since retired);
# the glibc base is kept.
ARG BUILD_FROM=ghcr.io/home-assistant/base-debian:bookworm
FROM ${BUILD_FROM}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         python3 python3-pip python3-venv \
         nodejs npm git nginx-light jq yq curl ca-certificates unzip \
         openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@2.1.220 \
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

# gh CLI for plugin-developer's GitHub repo creation + push (§6.1 / §6.3 / §8.5)
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

COPY requirements.txt /tmp/
RUN /opt/casa/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# --- Bundled plugin artifacts (unified plugin architecture, spec 3.6) --------
# Network-bound layer kept BEFORE `COPY rootfs /` so routine code changes do
# not re-fetch. Inputs are narrow-COPY'd; pins are exact commits checked into
# defaults/plugin-registry.json — the build NEVER resolves a tag/branch.
#
# Build-time SSH→HTTPS rewrite + auth flow through /etc/gitconfig + the
# credential helper; ship them via narrow COPYs so the fetch does not depend
# on the broad `COPY rootfs /` below (re-copied identically later, harmless).
COPY rootfs/etc/gitconfig /etc/gitconfig
COPY rootfs/opt/casa/scripts/git-credential-casa.sh /opt/casa/scripts/git-credential-casa.sh
COPY rootfs/opt/casa/plugin_registry.py rootfs/opt/casa/plugin_store.py rootfs/opt/casa/plugin_triggers.py rootfs/opt/casa/plugin_callbacks.py rootfs/opt/casa/plugin_events.py rootfs/opt/casa/callback_attempts.py rootfs/opt/casa/atomic_io.py rootfs/opt/casa/text_util.py /opt/casa/
COPY rootfs/opt/casa/scripts/build_plugin_bundle.py /opt/casa/scripts/build_plugin_bundle.py
COPY rootfs/opt/casa/defaults/plugin-registry.json /opt/casa/defaults/plugin-registry.json
RUN python3 /opt/casa/scripts/build_plugin_bundle.py \
      --registry /opt/casa/defaults/plugin-registry.json \
      --out /opt/casa/plugin-bundle \
 && rm -rf /opt/casa/.bundle-staging \
 && chmod -R a-w /opt/casa/plugin-bundle \
 && chmod -R a+rX /opt/casa/plugin-bundle

# Copy application (frequently-changing layer; kept AFTER the bundle build so a
# code edit does not bust the seed-install layer cache — L31).
COPY rootfs /

# Set permissions for s6-rc services and scripts
RUN chmod +x /etc/s6-overlay/scripts/*.sh \
    && find /etc/s6-overlay/s6-rc.d -name 'run' -o -name 'finish' \
    | xargs chmod +x \
    && chmod +x /opt/casa/scripts/*.sh \
    && chmod +x /usr/local/bin/casactl


# io.hass.* labels are injected by the deploy workflow (build-image action).
LABEL org.opencontainers.image.title="Casa" \
      org.opencontainers.image.description="Personal AI agent framework powered by Claude" \
      org.opencontainers.image.source="https://github.com/bonzanni/ha-casa-app" \
      org.opencontainers.image.licenses="MIT"
