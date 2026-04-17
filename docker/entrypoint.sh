#!/bin/sh
# Container entrypoint. Dispatches between the project's three modes.
#
# Usage:
#   docker run image-organizer                # web UI on :8765 (default)
#   docker run -i image-organizer mcp         # MCP server over stdio
#   docker run    image-organizer cli ARGS... # one-shot CLI run
#   docker run    image-organizer test        # run pytest (image must be built with tests)
#   docker run    image-organizer shell       # /bin/sh for debugging

set -e

mode="${1:-web}"
shift || true

case "$mode" in
  web)
    # Default web UI. Bind 0.0.0.0 inside the container so the published
    # port is reachable from the host.
    exec python -m uvicorn organizer.webui.server:app \
      --host 0.0.0.0 --port "${IMGORG_PORT:-8765}" \
      --log-level "${IMGORG_LOG_LEVEL:-warning}" "$@"
    ;;
  mcp)
    # MCP stdio server. The container MUST be run with -i so stdin stays open.
    exec python -m organizer.mcp_server "$@"
    ;;
  cli)
    exec python -m organizer.cli "$@"
    ;;
  test)
    exec python -m pytest "$@"
    ;;
  shell|sh|bash)
    exec /bin/sh
    ;;
  *)
    echo "Unknown mode: $mode" >&2
    echo "Usage: web | mcp | cli | test | shell" >&2
    exit 64
    ;;
esac
