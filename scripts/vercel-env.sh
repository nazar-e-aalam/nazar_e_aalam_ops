#!/usr/bin/env bash
# Push every KEY=VALUE from a local .env to Vercel (creates or overwrites).
#   scripts/vercel-env.sh                      -> .env  -> production
#   scripts/vercel-env.sh .env production preview development
#   SKIP="OPS_DEV OPS_ENV_FILE" scripts/vercel-env.sh
#   SENSITIVE=1 scripts/vercel-env.sh          -> mark values as sensitive (hidden in the dashboard; not allowed for "development")
set -euo pipefail

FILE="${1:-.env}"; shift || true
TARGETS=("${@:-production}")
SKIP="${SKIP:-}"
command -v vercel >/dev/null || { echo "Install the CLI first: npm i -g vercel"; exit 1; }
[ -f "$FILE" ] || { echo "No such file: $FILE"; exit 1; }
vercel whoami >/dev/null 2>&1 || { echo "Run: vercel login"; exit 1; }
[ -f .vercel/project.json ] || { echo "Run: vercel link   (once, in this folder)"; exit 1; }

n=0
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
  line="${line#export }"
  [[ "$line" == *=* ]] || continue
  key="${line%%=*}"; val="${line#*=}"
  key="$(echo "$key" | xargs)"
  # strip matching surrounding quotes
  if [[ "${#val}" -ge 2 && ( ( "${val:0:1}" == '"' && "${val: -1}" == '"' ) || ( "${val:0:1}" == "'" && "${val: -1}" == "'" ) ) ]]; then
    val="${val:1:${#val}-2}"
  fi
  [ -n "$val" ] || { echo "skip  $key (empty)"; continue; }
  case " $SKIP " in *" $key "*) echo "skip  $key"; continue;; esac
  for t in "${TARGETS[@]}"; do
    flags=(--force); [ "${SENSITIVE:-0}" = 1 ] && [ "$t" != development ] && flags+=(--sensitive)
    printf '%s' "$val" | vercel env add "$key" "$t" "${flags[@]}" >/dev/null 2>&1 \
      && echo "ok    $key -> $t" || echo "FAIL  $key -> $t"
  done
  n=$((n+1))
done < "$FILE"
echo "Done: $n variables. Redeploy for them to take effect:  vercel --prod"