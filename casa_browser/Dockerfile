# Casa Browser — the optional second app beside Casa. Debian trixie base (its python3 is
# 3.13, the version the app's tests run on). The image carries the Python runtime, the
# pinned dependencies, the service users, root bootstrap, the nftables egress floor, the
# enforcing proxy, Google Chrome on a virtual display and its launcher, and the ingress layer
# (nginx, a view-only x11vnc, noVNC's client files, xdotool and xclip for the live view).
ARG BUILD_FROM=ghcr.io/home-assistant/base-debian:trixie
FROM ${BUILD_FROM}

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates nftables \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 13))' \
    || { echo "casa_browser needs Python 3.13 from the base image" >&2; exit 1; }

# Every release installs the Chrome that is current when it is built. The builder passes the
# app's version as BUILD_VERSION; declared here, a new version is a cache miss at the Chrome
# step and every step after it (an ARG's value is part of each later RUN's cache key), so a
# layer cache never ships an old Chrome under a new version. The steps above stay cached.
ARG BUILD_VERSION=""

# Google Chrome (branded, from Google's signed repository; the build log prints the version),
# the virtual display, common fonts, NSS tools for the proxy CA and the Mesa Vulkan driver for
# the Intel GPU (ANGLE-Vulkan, measured on the target box).
RUN echo "Casa Browser ${BUILD_VERSION}: installing the current stable Chrome" \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
       | gpg --dearmor -o /usr/share/keyrings/google-linux.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
       > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable xvfb xauth \
       fonts-noto-core fonts-liberation fonts-dejavu-core libnss3-tools \
       mesa-vulkan-drivers libvulkan1 libgl1-mesa-dri libegl1 \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && chmod u-s /opt/google/chrome/chrome-sandbox \
    && /opt/google/chrome/chrome --version
# No setuid sandbox helper: Chrome uses its namespace sandbox (the launch passes
# chromium_sandbox=True), and without user namespaces it refuses to start rather than
# falling back to a setuid binary.

# The live view (Task 13): nginx answers HA ingress; x11vnc serves the display view-only on a
# Unix socket; the controller injects the operator's input with xdotool and reads the remote
# selection with xclip. noVNC is Debian's 1.6.0 client files only (pinned: the viewer uses two
# of its private members), extracted from the package without its nodejs/websockify
# dependencies — the controller bridges the VNC socket itself.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx-light x11vnc xdotool xclip \
    && cd /tmp && apt-get download novnc=1:1.6.0-2 \
    && dpkg-deb -x /tmp/novnc_*.deb /tmp/novnc \
    && mkdir -p /usr/share/novnc \
    && cp -a /tmp/novnc/usr/share/novnc/core /tmp/novnc/usr/share/novnc/vendor /usr/share/novnc/ \
    && rm -rf /tmp/novnc /tmp/novnc_*.deb /var/lib/apt/lists/* /etc/nginx/sites-enabled \
    && test -f /usr/share/novnc/core/rfb.js \
    && nginx -v && x11vnc -version

# Pinned dependencies in their own venv; the build log prints every installed version and
# the base's release, so a CI log shows exactly what an image carries.
COPY requirements.txt /tmp/casa_browser-requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/casa_browser-requirements.txt \
    && rm /tmp/casa_browser-requirements.txt \
    && /opt/venv/bin/python --version \
    && /opt/venv/bin/pip freeze \
    && cat /etc/os-release

# One OS user per service (Plan 1 Task 2). Fixed ids: Task 5's egress firewall matches
# them. No login shell, no password. casa-vncview (Task 13) is the one group that may open
# the VNC socket: the controller only (the broker's group membership does not reach it).
RUN groupadd --system --gid 1100 casa-browser \
    && useradd --system --uid 1100 --gid 1100 --home-dir /nonexistent --shell /usr/sbin/nologin casa-browser \
    && groupadd --system --gid 1101 casa-proxy \
    && useradd --system --uid 1101 --gid 1101 --home-dir /nonexistent --shell /usr/sbin/nologin casa-proxy \
    && groupadd --system --gid 1102 casa-broker \
    && useradd --system --uid 1102 --gid 1102 --home-dir /nonexistent --shell /usr/sbin/nologin casa-broker \
    && groupadd --system --gid 1103 casa-controller \
    && useradd --system --uid 1103 --gid 1103 --home-dir /nonexistent --shell /usr/sbin/nologin casa-controller \
    && usermod --append --groups casa-controller casa-broker \
    && groupadd --system --gid 1104 casa-vnc \
    && useradd --system --uid 1104 --gid 1104 --home-dir /nonexistent --shell /usr/sbin/nologin casa-vnc \
    && groupadd --system --gid 1105 casa-ingress \
    && useradd --system --uid 1105 --gid 1105 --home-dir /nonexistent --shell /usr/sbin/nologin casa-ingress \
    && groupadd --system --gid 1106 casa-vncview \
    && usermod --append --groups casa-vncview casa-controller

COPY rootfs /
RUN chmod +x /etc/s6-overlay/scripts/*.sh /opt/casa_browser/bin/chrome-launch \
    && scratch="$(mktemp -d)" \
    && s6-rc-compile "${scratch}/db" /package/admin/s6-overlay/etc/s6-rc/sources /etc/s6-overlay/s6-rc.d /etc/s6-overlay/user-bundles.d \
    && rm -rf "${scratch}" \
    || { echo "the s6-rc database does not compile in this image" >&2; exit 1; }

LABEL org.opencontainers.image.title="Casa Browser" \
      org.opencontainers.image.description="Sandboxed browser for Casa's browsing specialist" \
      org.opencontainers.image.source="https://github.com/bonzanni/ha-casa-app" \
      org.opencontainers.image.licenses="MIT"
