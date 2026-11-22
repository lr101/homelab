FROM node:26-slim

ARG RUSTFS_VERSION=1.0.0-beta.12
ARG RUSTFS_SHA256=683bef16247ab04bedb76d0444736f286d21943375d2d57d2ded9ec277498427

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        python3 \
        python3-venv \
        build-essential \
        bubblewrap \
        dbus-x11 \
        docker-cli \
        docker-buildx \
        jq \
        openssl \
        ripgrep \
        shellcheck \
        sudo \
        libnss3 \
        libglib2.0-0t64 \
        libatk1.0-0t64 \
        libatk-bridge2.0-0t64 \
        libatspi2.0-0t64 \
        libdbus-1-3 \
        libx11-6 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxrandr2 \
        libxkbcommon0 \
        libgbm1 \
        libasound2t64 \
        libexpat1 \
        fontconfig \
        fonts-liberation \
        libglu1-mesa \
        openssh-client \
        pkg-config \
        unzip \
        zip \
        xz-utils \
        wget \
        nginx \
        postgresql \
        postgresql-client \
        postgresql-contrib \
        postgis \
        postgresql-postgis \
        procps \
        iproute2 \
        util-linux && \
    mkdir -p -m 755 /etc/apt/keyrings && \
    wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        > /etc/apt/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends gh && \
    rm -rf /var/lib/apt/lists/*

RUN npm install -g \
    t3 \
    @openai/codex \
    @anthropic-ai/claude-code

ENV MISE_INSTALL_PATH=/usr/local/bin/mise
RUN curl -fsSL https://mise.run -o /tmp/mise-install.sh && \
    sh /tmp/mise-install.sh && \
    rm /tmp/mise-install.sh

ENV MISE_DATA_DIR=/opt/mise
ENV PATH="/opt/mise/shims:/usr/local/share/mise/shims:${PATH}"

# System installs remain visible when Compose bind-mounts /opt/mise.
# Match MonaServer's mise.toml while keeping the base Node for T3 itself.
RUN mkdir -p /etc/mise && \
    printf '%s\n' '[tools]' \
        'agy = "1.5.14"' \
        'go = "1.27.1"' \
        'flutter = "3.47.4"' \
        'java = "temurin-17.0.21.1+2"' \
        'oapi-codegen = "2.8.0"' \
        'sqlc = "1.31.1"' \
        'docker-compose = "2.40.3"' \
        'age = "1.3.2"' \
        'sops = "3.13.3"' \
        'gitleaks = "8.24.3"' \
        'actionlint = "1.7.12"' \
        'uv = "0.12.23"' \
        > /etc/mise/config.toml && \
    export MISE_DOWNLOAD_CACHE=0 && \
    mise install --system go node@24.21.0 && \
    mise install --system && \
    mise reshim --system && \
    mkdir -p /usr/local/lib/docker/cli-plugins /opt/mise && \
    ln -s "$(mise which agy)" /usr/local/bin/agy && \
    ln -s "$(mise which docker-compose)" /usr/local/lib/docker/cli-plugins/docker-compose && \
    chown -R node:node /opt/mise "$(readlink -f "$(mise where flutter)")" && \
    setpriv --reuid=node --regid=node --init-groups env HOME=/home/node \
        sh -c '/usr/local/bin/agy --version && flutter precache --web --android' && \
    go clean -cache -modcache && \
    mise cache clear

# Keep both generators pinned to the versions used by MonaServer's Go and Dart clients.
ENV OPENAPI_GENERATOR_JAR=/opt/openapi-generator/openapi-generator-cli-7.19.0.jar
RUN mkdir -p /opt/openapi-generator && \
    for version in 7.19.0 7.9.0; do \
        curl -fsSL --retry 3 \
            "https://repo.maven.apache.org/maven2/org/openapitools/openapi-generator-cli/${version}/openapi-generator-cli-${version}.jar" \
            -o "/opt/openapi-generator/openapi-generator-cli-${version}.jar" || exit 1; \
    done && \
    printf '%s\n' \
        "3d8140c691410e0004b1bb9b1e431c1293734830f30d6d5922f8e5dbf2e42e19  $OPENAPI_GENERATOR_JAR" \
        'f0cb7839a2ead9040b204519b03f1473b39c7725fd1f43134bb550515cb3dbee  /opt/openapi-generator/openapi-generator-cli-7.9.0.jar' \
        | sha256sum -c -

# Android packages required by the bundled Flutter SDK for local APK builds.
ENV ANDROID_HOME=/opt/android-sdk
ENV PATH="${ANDROID_HOME}/cmdline-tools/latest/bin:${ANDROID_HOME}/platform-tools:${PATH}"
RUN curl -fsSL --retry 3 \
        https://dl.google.com/android/repository/commandlinetools-linux-15859902_latest.zip \
        -o /tmp/android-tools.zip && \
    echo '4e4c464f145a7512b57d088ac6c278c03c9eea610886b35a5e0804e74eedf583  /tmp/android-tools.zip' | sha256sum -c - && \
    unzip -q /tmp/android-tools.zip -d /tmp/android-tools && \
    mkdir -p "$ANDROID_HOME/cmdline-tools" && \
    mv /tmp/android-tools/cmdline-tools "$ANDROID_HOME/cmdline-tools/latest" && \
    (yes | sdkmanager --licenses >/dev/null) && \
    sdkmanager --install 'platform-tools' 'platforms;android-36' 'build-tools;36.0.0' 'ndk;28.2.13676358' && \
    chown -R node:node "$ANDROID_HOME" && \
    rm -rf /tmp/android-tools /tmp/android-tools.zip /root/.android

# Match MonaServer's locked Playwright Chromium revision.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN npm exec --yes --package=playwright@1.63.0 -- playwright install --with-deps chromium && \
    chown -R node:node "$PLAYWRIGHT_BROWSERS_PATH" && \
    npm cache clean --force && \
    rm -rf /var/lib/apt/lists/*

RUN curl -fsSL --retry 3 \
        "https://github.com/RustFS/RustFS/releases/download/${RUSTFS_VERSION}/rustfs-linux-x86_64-musl-v${RUSTFS_VERSION}.zip" \
        -o /tmp/rustfs.zip && \
    echo "${RUSTFS_SHA256}  /tmp/rustfs.zip" | sha256sum -c - && \
    mkdir -p /tmp/rustfs && \
    unzip -q /tmp/rustfs.zip -d /tmp/rustfs && \
    install -m 0755 /tmp/rustfs/rustfs /usr/local/bin/rustfs && \
    rm -rf /tmp/rustfs /tmp/rustfs.zip

RUN git config --system credential.helper \
    '!f() { echo "username=${GITHUB_USER:-x-access-token}"; echo "password=${GH_TOKEN:-$GITHUB_TOKEN}"; }; f'

RUN git config --system user.name "lr101-coding-bot" && \
    git config --system user.email "info@lr-projects.de"

# Only the local development database needs another OS identity. Agents cannot
# use this rule to run commands as root.
RUN printf '%s\n' 'node ALL=(postgres) NOPASSWD: /usr/bin/psql' \
        > /etc/sudoers.d/t3-preview-postgres && \
    chmod 0440 /etc/sudoers.d/t3-preview-postgres && \
    visudo -cf /etc/sudoers.d/t3-preview-postgres && \
    mkdir -p /home/node/.t3 /home/node/.codex /home/node/.codex_p /home/node/.agents/skills /workspace && \
    chown -R node:node /home/node /workspace && \
    chmod 0711 /root && \
    ln -s /home/node/.t3 /root/.t3 && \
    ln -s /home/node/.codex /root/.codex && \
    ln -s /home/node/.codex_p /root/.codex_p && \
    ln -s /home/node/.agents /root/.agents

WORKDIR /workspace

ENV T3CODE_HOST=0.0.0.0
ENV T3CODE_PORT=8080
ENV T3CODE_NO_BROWSER=true

EXPOSE 8080 18080

COPY ./entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["t3", "serve", "--host", "0.0.0.0", "--port", "8080"]
