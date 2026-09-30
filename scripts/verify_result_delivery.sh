#!/usr/bin/env bash
# Manual, one-shot verification that a real sandbox run makes it all the way
# through: Service Bus enqueue -> sandbox-worker pickup -> LLM stub ->
# Redis publish -> result-consumer's consumer-group read -> Postgres write
# -> visible via GET /api/v1/runs/{id}.
#
# Deliberately NOT part of scripts/smoke_test.py -- that script cancels its
# run immediately after creating it (see its own docstring), on purpose, so
# it never exercises the Redis-based delivery path this script is for.
#
# Run this against the stack started by run_smoke_test.sh (or plain
# `docker compose up -d`), while it's still up:
#
#   bash scripts/verify_result_delivery.sh
#
# Requires: curl, and a grep that supports -P (GNU grep's PCRE mode --
# available in git-bash on Windows and virtually every Linux distro).
set -uo pipefail

BASE="${SMOKE_TEST_BASE_URL:-http://localhost:8000}"
EMAIL="verifyrun_$(date +%s)@example.com"

echo "== registering $EMAIL against $BASE =="
REG=$(curl -sf -X POST "$BASE/api/v1/auth/register" -H "Content-Type: application/json" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"Testpass123!\",\"display_name\":\"Verify Run\"}") || {
  echo "FAILED: register call itself failed -- is the stack up and reachable at $BASE?" >&2
  exit 1
}

VTOKEN=$(echo "$REG" | grep -oP '(?<=token=)[^"&]*')
if [ -z "$VTOKEN" ]; then
  echo "FAILED: could not extract verification token from register response:" >&2
  echo "$REG" >&2
  exit 1
fi

echo "== verifying email (logs the account straight in) =="
LOGIN=$(curl -sf -X POST "$BASE/api/v1/auth/verify-email" -H "Content-Type: application/json" \
  -d "{\"token\":\"$VTOKEN\"}") || { echo "FAILED: verify-email call failed" >&2; exit 1; }
ACCESS=$(echo "$LOGIN" | grep -oP '(?<="access_token":")[^"]*')
if [ -z "$ACCESS" ]; then
  echo "FAILED: could not extract access_token from verify-email response:" >&2
  echo "$LOGIN" >&2
  exit 1
fi

echo "== creating conversation =="
CONV=$(curl -sf -X POST "$BASE/api/v1/conversations" -H "Authorization: Bearer $ACCESS" \
  -H "Content-Type: application/json" -d '{}') || { echo "FAILED: create conversation failed" >&2; exit 1; }
CONV_ID=$(echo "$CONV" | grep -oP '(?<="id":")[^"]*')

echo "== uploading a throwaway fake PDF =="
TMP_PDF="$(mktemp --suffix=.pdf 2>/dev/null || mktemp /tmp/verify-XXXXXX.pdf)"
printf '%%PDF-1.4\n%%fake-pdf-for-verify-script\n' > "$TMP_PDF"
UPLOAD=$(curl -sf -X POST "$BASE/api/v1/conversations/$CONV_ID/files" \
  -H "Authorization: Bearer $ACCESS" -F "file=@${TMP_PDF};type=application/pdf") \
  || { echo "FAILED: upload failed" >&2; rm -f "$TMP_PDF"; exit 1; }
rm -f "$TMP_PDF"
FILE_ID=$(echo "$UPLOAD" | grep -oP '(?<="id":")[^"]*')

echo "== creating a run (NOT cancelling it, unlike the smoke test) =="
RUN=$(curl -sf -X POST "$BASE/api/v1/conversations/$CONV_ID/runs" \
  -H "Authorization: Bearer $ACCESS" -H "Content-Type: application/json" \
  -d '{"uploaded_file_id":"'"$FILE_ID"'","question":"Any issues?"}') \
  || { echo "FAILED: create run failed" >&2; exit 1; }
RUN_ID=$(echo "$RUN" | grep -oP '(?<="id":")[^"]*')
if [ -z "$RUN_ID" ]; then
  echo "FAILED: could not extract run id from create-run response:" >&2
  echo "$RUN" >&2
  exit 1
fi
echo "run id: $RUN_ID"

echo "== polling GET /api/v1/runs/$RUN_ID (up to 30s) =="
STATUS=""
POLL=""
for i in $(seq 1 15); do
  sleep 2
  POLL=$(curl -sf "$BASE/api/v1/runs/$RUN_ID" -H "Authorization: Bearer $ACCESS")
  STATUS=$(echo "$POLL" | grep -oP '(?<="status":")[^"]*')
  echo "  [$i] status=$STATUS"
  if [ "$STATUS" = "succeeded" ] || [ "$STATUS" = "failed" ]; then
    break
  fi
done

echo
echo "== final run body =="
echo "$POLL"
echo

if [ "$STATUS" = "succeeded" ]; then
  echo "PASSED: run reached status=succeeded -- the full Service Bus -> sandbox-worker -> Redis -> result-consumer -> Postgres path is confirmed live."
  exit 0
elif [ "$STATUS" = "failed" ]; then
  echo "RUN FAILED (status=failed) -- see error_message above, and 'docker compose logs sandbox-worker result-consumer' for why." >&2
  exit 1
else
  echo "NOT CONFIRMED: run is still '$STATUS' after ~30s. Check 'docker compose logs sandbox-worker result-consumer' for what's stuck -- most likely acb-msak-redis reachability/credential from one side or the other (see DEFERRED_ITEMS.md items 16-17), or the Service Bus session simply hasn't been picked up yet (try polling longer by hand)." >&2
  exit 1
fi
