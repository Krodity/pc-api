#!/bin/sh
# Import the pc-api connector into n8n:
#   - credential "pc-api" (Header Auth: Authorization: Bearer <token>)
#   - workflow  "pc-api: call"     sub-workflow: items {method, path, body} -> pc-api
#   - workflow  "pc-api: example"  manual run that sends a desktop notification
#
#   N8N="n8n" PROJECT_ID=<id> ./install.sh
#
# N8N: how to run the n8n CLI (it must be the same n8n/Node the server uses).
# PROJECT_ID: n8n project to put them in (Settings -> Projects, or the
# project table). Re-running updates them in place: the IDs are fixed.
# Running workflows pick up new credentials/sub-workflows without a restart.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
N8N="${N8N:-n8n}"
TOKEN_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/pc-api/token"
[ -r "$TOKEN_FILE" ] || { echo "no pc-api token at $TOKEN_FILE (start pc-api first)" >&2; exit 1; }
PROJ=""
[ -n "${PROJECT_ID:-}" ] && PROJ="--projectId=$PROJECT_ID"

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
chmod 600 "$TMP"
TOKEN="$(cat "$TOKEN_FILE")" python3 - "$TMP" <<'PY'
import json, os, sys
json.dump([{"id": "pcApiBearer01", "name": "pc-api", "type": "httpHeaderAuth",
            "data": {"name": "Authorization", "value": "Bearer " + os.environ["TOKEN"]}}],
          open(sys.argv[1], "w"))
PY

# shellcheck disable=SC2086
$N8N import:credentials --input="$TMP" $PROJ
# shellcheck disable=SC2086
$N8N import:workflow --input="$HERE/pc-api-call.workflow.json" $PROJ
# shellcheck disable=SC2086
$N8N import:workflow --input="$HERE/pc-api-example.workflow.json" $PROJ
# n8n 2.x only runs sub-workflows that are published.
$N8N publish:workflow --id=pcApiCallSub01
echo "done. Test: N8N_RUNNERS_BROKER_PORT=5699 $N8N execute --id=pcApiExample01 (a desktop notification should appear;
the extra port avoids clashing with a running server's task broker)"
