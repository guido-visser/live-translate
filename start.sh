#!/usr/bin/env sh
# Start Live Translate from a source checkout on macOS or Linux.
# Needs uv: https://docs.astral.sh/uv/getting-started/installation/
set -e
cd "$(dirname "$0")"
if ! command -v uv >/dev/null 2>&1; then
    echo "Please install uv first: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi
uv sync --frozen --no-dev
exec uv run --frozen --no-dev python -m livetranslate "$@"
