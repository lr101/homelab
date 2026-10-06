FROM node:24-slim

RUN apt-get update && \
    apt-get install -y \
        git \
        curl \
        ca-certificates \
        python3 \
        build-essential \
        bubblewrap \
        openssh-client \
        pkg-config \
        unzip \
        xz-utils && \
    rm -rf /var/lib/apt/lists/*

RUN npm install -g \
    t3 \
    @openai/codex \
    @anthropic-ai/claude-code

RUN apt-get update \
    && apt-get install -y --no-install-recommends wget ca-certificates \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       > /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

ENV MISE_INSTALL_PATH=/usr/local/bin/mise
RUN curl https://mise.run | sh

ENV MISE_DATA_DIR=/opt/mise
ENV PATH="/opt/mise/shims:${PATH}"

RUN git config --global credential.helper '!f() { echo "username=${GITHUB_USER}"; echo "password=${GITHUB_TOKEN}"; }; f'

RUN git config --global user.name "lr101-coding-bot" && \
    git config --global user.email "info@lr-projects.de"

WORKDIR /workspace

ENV T3CODE_HOST=0.0.0.0
ENV T3CODE_PORT=8080
ENV T3CODE_NO_BROWSER=true

EXPOSE 8080

COPY ./entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]

CMD ["t3", "serve", "--host", "0.0.0.0", "--port", "8080"]
