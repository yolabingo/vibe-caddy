# vibe-caddy project tasks. Run `just` to list them.
# Everything goes through uv; no tool is installed into the project environment.

set shell := ["bash", "-euo", "pipefail", "-c"]

_default:
    @just --list --unsorted

# Install development dependencies and Git hooks.
[group('development')]
install:
    uv sync
    uv run prek install

# Install the prek Git hooks.
[group('development')]
hooks-install:
    uv run prek install

# Run every prek hook on all files.
[group('checks')]
hooks:
    uv run prek run --all-files

# Resolve all direct dependencies with default bounds and a five-day release cutoff.
[group('development')]
deps-bump:
    uv run --no-project scripts/bump_deps.py --exclude-newer "$(perl -MPOSIX=strftime -e 'print strftime("%Y-%m-%d", gmtime(time - 5 * 24 * 60 * 60))')"

# Format sources in place (uv's managed Ruff formatter).
[group('checks')]
fmt:
    uv format

# Verify formatting without writing (CI gate).
[group('checks')]
fmt-check:
    uv format --check

# Type check (uv's managed ty).
[group('checks')]
check:
    uv check

# Audit dependencies for known vulnerabilities.
[group('checks')]
audit:
    uv audit

# Run the test suite.
[group('checks')]
test *ARGS:
    uv run pytest {{ARGS}}

# Run the test suite with a coverage report.
[group('checks')]
cov:
    uv run pytest --cov=vibe_caddy --cov-report=term-missing

# Everything CI runs, in CI's order.
[group('checks')]
ci: fmt-check check test audit

# Format, type check and test. The pre-commit loop.
[group('checks')]
all: fmt check test

# Install the CLI and print the commands that apply it to a local setup.
[group('CLI installation')]
install-cli:
    uv tool install --force --reinstall .
    @printf '\nFirst install or after a reset:\n  sudo --preserve-env=XDG_DATA_HOME,XDG_STATE_HOME "%s/vibe-caddy" setup\n\nExisting setup:\n  vibe-caddy reload\n  vibe-caddy dashboard install\n\nRestart each other running managed app to apply the installed CLI and environment:\n  vibe-caddy restart <name>\n' "$(uv tool dir --bin)"

# Reinstall from the working tree after a code change.
[group('CLI installation')]
reinstall: install-cli
    @vibe-caddy --version

# Remove the installed CLI.
[group('CLI installation')]
uninstall-cli:
    uv tool uninstall vibe-caddy

# Print the Caddyfile vibe would generate from the current registry.
[group('runtime')]
caddyfile:
    uv run vibe-caddy caddyfile

# Validate that generated Caddyfile against the real caddy binary.
[group('runtime')]
validate: caddyfile
    uv run vibe-caddy caddyfile --validate

# Run the dashboard in the foreground against the current registry.
[group('runtime')]
dashboard PORT="7999":
    uv run vibe-caddy dashboard serve --port {{PORT}}

# Delete build artefacts and caches.
[group('packaging')]
clean:
    rm -rf dist build .pytest_cache .coverage htmlcov
    find . -name __pycache__ -type d -prune -exec rm -rf {} +

# Build the wheel and sdist.
[group('packaging')]
build: clean
    uv build
