#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${DATA_GRAPH_BASE_URL:-http://127.0.0.1:8080}"
TOKEN="${DATA_GRAPH_API_TOKEN:-}"
PARALLEL_REQUESTS="${PARALLEL_REQUESTS:-10}"
DEBOUNCE_WAIT_SECONDS="${DEBOUNCE_WAIT_SECONDS:-3}"

if [[ -z "$TOKEN" && -f "$ROOT/.env" ]]; then
  TOKEN="$(grep '^DATA_GRAPH_API_TOKEN=' "$ROOT/.env" | cut -d= -f2- || true)"
fi
if [[ -z "$TOKEN" ]]; then
  echo "Set DATA_GRAPH_API_TOKEN or create .env first." >&2
  exit 2
fi

CONFIG='{"name":"Parallel Ingest Test","dataSchema":{"id":"String","kind":"String","summary":"String"},"groupingFields":["kind"],"recordIdField":"id","titleField":"id","detailField":"summary"}'
GRAPH_ID="$(
  curl -fsS "$BASE_URL/api/data-graph" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"config\":$CONFIG}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["dataGraphId"])'
)"

for index in $(seq 1 "$PARALLEL_REQUESTS"); do
  curl -fsS "$BASE_URL/api/data-graph/$GRAPH_ID/data" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"data\":[{\"id\":\"row-$index\",\"kind\":\"k$((index % 3))\",\"summary\":\"parallel row $index\"}]}" >/dev/null &
done
wait
sleep "$DEBOUNCE_WAIT_SECONDS"

curl -fsS "$BASE_URL/api/data-graph/$GRAPH_ID/status" \
  -H "Authorization: Bearer $TOKEN"
echo
