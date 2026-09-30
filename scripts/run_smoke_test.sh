#!/usr/bin/env bash
# One command instead of two: builds + starts the whole throwaway stack
# detached, waits specifically for the smoketest container to finish, then
# reports pass/fail using its real exit code. Run from the directory that
# contains docker-compose.yml:
#
#   bash scripts/run_smoke_test.sh
#
# (prefix with `sudo` too if your plain `docker` commands already need it)
set -uo pipefail

docker compose up --build -d
docker compose wait smoketest
STATUS=$?

echo
echo "=== smoketest logs ==="
docker compose logs smoketest
echo

if [ "$STATUS" -eq 0 ]; then
    echo "SMOKE TEST PASSED"
    echo "Stack left running -- try http://localhost:8000/docs for the interactive Swagger UI."
    echo "Run 'docker compose down' yourself when you're done poking at it."
else
    echo "SMOKE TEST FAILED (smoketest exited $STATUS)"
    echo "Stack left running for inspection -- try:"
    echo "  docker compose logs migrate"
    echo "  docker compose logs api"
    echo "Run 'docker compose down' yourself when you're done looking."
fi

exit "$STATUS"
