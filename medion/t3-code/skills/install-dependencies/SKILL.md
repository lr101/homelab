---
name: install-dependencies
description: Install missing development runtimes, CLI tools, project dependencies, or system libraries in this development container. Prefer mise for runtimes and developer tools, project package managers for application dependencies, and apt only for OS-level packages.
---

# Install Dependencies

Use the least invasive installation method appropriate for the dependency.

## Rules

1. Inspect the repository first for `mise.toml`, package manifests, lockfiles, README instructions, and existing tool versions.
2. For programming languages and developer CLIs, prefer `mise`.
   - Example: `mise use go@latest`
   - Example: `mise use python@3.14`
   - Example: `mise use rust@stable`
3. For project dependencies, use the project's package manager and lockfile:
   - Node: npm/pnpm/yarn
   - Python: uv/pip/poetry
   - Go: `go mod`
   - Rust: Cargo
4. Use `apt-get` only for OS packages, native libraries, or tools not appropriately managed by `mise`.
5. Run `apt-get update` immediately before installing apt packages and use `apt-get install -y`.
6. Do not install unrelated packages, remove existing tooling, or change versions unnecessarily.
7. Avoid global npm/pip installs for project libraries.
8. Never expose or print credentials, GitHub tokens, or files under `/run/secrets`.
9. After installation, verify the dependency with its version command or the project's build/test command.
10. If a dependency should survive container recreation, record it in `mise.toml` when possible. Recommend adding recurring OS packages to the Dockerfile rather than relying on an ephemeral `apt-get` install.

## Preferred order

1. Existing project configuration
2. `mise`
3. Project package manager
4. `apt-get`
5. Direct installer only when the above are unsuitable and the source is trusted
