#!/usr/bin/env bash
# Writes web/config.js from .env (anon key only; it is public by design, RLS protects the data).
set -euo pipefail
cd "$(dirname "$0")"
set -a; source .env; set +a
: "${SUPABASE_URL:?SUPABASE_URL missing in .env}"
: "${SUPABASE_ANON_KEY:?SUPABASE_ANON_KEY missing in .env}"
cat > web/config.js <<JS
window.TWINGATE = {
  SUPABASE_URL: "${SUPABASE_URL}",
  SUPABASE_ANON_KEY: "${SUPABASE_ANON_KEY}",
  PUBLIC_URL: "${PUBLIC_URL:-}",
  HOME_VIEWER: "${HOME_VIEWER:-}"
};
JS
echo "web/config.js written"
