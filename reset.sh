#!/usr/bin/env bash
# Clean slate before the demo: missions, events, votes, reports, teach submissions,
# crowd skills (unless --keep-skills); releases E-STOP, Sentry and the teach slot.
set -euo pipefail
cd "$(dirname "$0")"
set -a; source .env; set +a
H=(-H "apikey: $SUPABASE_SERVICE_ROLE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY" -H "Content-Type: application/json")
curl -s -X DELETE "$SUPABASE_URL/rest/v1/events?id=gt.0" "${H[@]}"
curl -s -X DELETE "$SUPABASE_URL/rest/v1/reports?id=gt.0" "${H[@]}"
curl -s -X DELETE "$SUPABASE_URL/rest/v1/teach_submissions?id=gt.0" "${H[@]}"
curl -s -X DELETE "$SUPABASE_URL/rest/v1/missions?id=gt.0" "${H[@]}"
curl -s -X PATCH "$SUPABASE_URL/rest/v1/control?id=eq.1" "${H[@]}" -d '{"estop":false}'
curl -s -X PATCH "$SUPABASE_URL/rest/v1/watch?id=eq.1" "${H[@]}" -d '{"enabled":false}'
curl -s -X PATCH "$SUPABASE_URL/rest/v1/teach?id=eq.1" "${H[@]}" -d '{"owner":null,"owner_name":null}'
if [ "${1:-}" != "--keep-skills" ]; then
  curl -s -X DELETE "$SUPABASE_URL/rest/v1/skills?author=neq.seed" "${H[@]}"
  curl -s -X PATCH "$SUPABASE_URL/rest/v1/skills?author=eq.seed" "${H[@]}" -d '{"uses":0}'
fi
echo "reset done"
