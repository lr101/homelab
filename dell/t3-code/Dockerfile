FROM node:26-slim

ARG RUSTFS_VERSION=1.0.0-beta.12
ARG RUSTFS_SHA256=683bef16247ab04bedb76d0444736f286d21943375d2d57d2ded9ec277498427

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        python3 \
        build-essential \
        bubblewrap \
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
        openssh-client \
        pkg-config \
        unzip \
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
ENV PATH="/opt/mise/shims:${PATH}"

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
    mkdir -p /home/node/.t3 /home/node/.codex /home/node/.codex_p /home/node/.agents/skills /workspace /opt/mise && \
    chown -R node:node /home/node /workspace /opt/mise && \
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
