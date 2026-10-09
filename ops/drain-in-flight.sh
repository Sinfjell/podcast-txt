#!/usr/bin/env bash
# Wait until Podskrift has no queued/running transcriptions (deploy drain).
#
# Polls the local-only /internal/in-flight endpoint. New jobs may still be
# accepted during the wait — boot resume will re-queue anything left mid-flight
# after restart. Exits 0 when drained or when MAX_WAIT_SEC is reached (deploy
# proceeds either way so the Actions job stays inside its timeout).
set -euo pipefail

BASE_URL="${PODSKRIFT_DRAIN_URL:-http://127.0.0.1:5002}"
MAX_WAIT_SEC="${PODSKRIFT_DRAIN_MAX_WAIT_SEC:-1200}"   # 20 minutes
POLL_SEC="${PODSKRIFT_DRAIN_POLL_SEC:-15}"

deadline=$((SECONDS + MAX_WAIT_SEC))
echo "drain: waiting up to ${MAX_WAIT_SEC}s for in-flight transcriptions (${BASE_URL})"

while true; do
  body="$(curl -fsS --max-time 5 "${BASE_URL}/internal/in-flight" || true)"
  if [[ -z "${body}" ]]; then
    echo "drain: could not reach in-flight endpoint; proceeding"
    exit 0
  fi
  count="$(python3 -c 'import json,sys; print(int(json.load(sys.stdin)["in_flight"]))' <<<"${body}" 2>/dev/null || true)"
  if ! [[ "${count}" =~ ^[0-9]+$ ]]; then
    echo "drain: unexpected in-flight response; proceeding"
    exit 0
  fi
  if [[ "${count}" -eq 0 ]]; then
    echo "drain: no in-flight transcriptions"
    exit 0
  fi
  if [[ "${SECONDS}" -ge "${deadline}" ]]; then
    echo "drain: timeout with ${count} still in flight; proceeding (boot will resume)"
    exit 0
  fi
  echo "drain: ${count} in flight; sleeping ${POLL_SEC}s..."
  sleep "${POLL_SEC}"
done
