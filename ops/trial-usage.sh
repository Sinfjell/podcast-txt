#!/usr/bin/env bash
# Report what the free trial has cost us so far.
#
# Trial minutes are spent on OUR OpenAI key, so this is the only number that
# turns into a bill. Run it on the server:
#   ops/trial-usage.sh [/path/to/podcast.db]
set -euo pipefail

DB="${1:-$(dirname "$0")/../data/podcast.db}"
COST_PER_MIN=0.006

if [ ! -s "$DB" ]; then
    echo "No database at $DB" >&2
    exit 1
fi

# sqlite3 creates an empty file for a missing path and still exits 0, so the
# -s test above is what actually proves the database exists.
sqlite3 "$DB" <<SQL
.mode column
.headers on
SELECT 'total_trial_minutes' AS metric,
       COALESCE(SUM(trial_seconds_used), 0) / 60 AS value,
       printf('\$%.2f', COALESCE(SUM(trial_seconds_used), 0) / 60.0 * $COST_PER_MIN) AS cost
  FROM users;

.print ''
SELECT id, email,
       COALESCE(trial_seconds_used, 0) / 60 AS trial_minutes_used,
       CASE WHEN openai_api_key IS NULL THEN 'trial' ELSE 'own key' END AS key_source
  FROM users
 WHERE COALESCE(trial_seconds_used, 0) > 0
 ORDER BY trial_seconds_used DESC;
SQL
