# vibe-caddy project tasks. Run `just` to list them.
# Everything goes through uv; no tool is installed into the project environment.

set shell := ["bash", "-euo", "pipefail", "-c"]

_default:
    @just --list --unsorted

# Install the project and its dev dependencies.
install:
    uv sync

# Format sources in place (uv's managed Ruff formatter).
fmt:
    uv format

# Verify formatting without writing (CI gate).
fmt-check:
    uv format --check

# Type check (uv's managed ty).
check:
    uv check

# Audit dependencies for known vulnerabilities.
audit:
    uv audit

# Run the test suite.
test *ARGS:
    uv run pytest {{ARGS}}

# Run the test suite with a coverage report.
cov:
    uv run pytest --cov=vibe_caddy --cov-report=term-missing

# Everything CI runs, in CI's order.
ci: fmt-check check test audit

# Format, type check and test. The pre-commit loop.
all: fmt check test

# Install the CLI onto this machine as the `vibe-caddy` command.
install-cli:
    uv tool install --force --reinstall .

# Reinstall from the working tree after a code change.
reinstall: install-cli
    @vibe-caddy --version

# Remove the installed CLI.
uninstall-cli:
    uv tool uninstall vibe-caddy

# Print the Caddyfile vibe would generate from the current registry.
caddyfile:
    uv run vibe-caddy caddyfile

# Validate that generated Caddyfile against the real caddy binary.
validate: caddyfile
    uv run vibe-caddy caddyfile --validate

# Run the dashboard in the foreground against the current registry.
dashboard PORT="7999":
    uv run vibe-caddy dashboard serve --port {{PORT}}

# Delete build artefacts and caches.
clean:
    rm -rf dist build .pytest_cache .coverage htmlcov
    find . -name __pycache__ -type d -prune -exec rm -rf {} +

# Build the wheel and sdist.
build: clean
    uv build
