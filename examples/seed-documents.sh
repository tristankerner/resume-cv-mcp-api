#!/usr/bin/env bash
#
# Upload the three example documents in this directory to a running instance
# of this service. Redundant for a brand-new account — POST /users and the
# admin bootstrap now seed these same three documents automatically, under
# `resume`/`metadata`/`skill` rather than the *.example.json names this script
# uses — but still useful for re-seeding an existing account, or for loading
# real content in place of the fictional starting point.
#
# Usage:
#
#   ./examples/seed-documents.sh                              # prompts for host and credentials
#   BASE_URL=http://localhost:8000 \
#     ADMIN_USERNAME=you ./examples/seed-documents.sh          # prompts for the password only
#   ./examples/seed-documents.sh --dry-run                     # validate the files, upload nothing
#
# Re-running is safe. The store is append-only and deduplicating: a document
# whose `data` and `public` flag are both byte-identical to the current
# revision is left alone and its existing revision number comes back, so an
# unchanged run reports the same numbers as the last one rather than piling up
# revisions. A changed `data` or a flipped `public` writes a new revision
# alongside the old, never over it.
#
# Note that only `data` and `public` are compared — editing `revision_note` on
# its own is a no-op. Change the content if you want the note to land.

set -euo pipefail

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

DOCS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# filename : route. The route sets the type of what it stores; the document's
# name is the operator's choice and no longer something a route checks — these
# three are just the names this script uses consistently.
DOCUMENTS=(
  "resume.example.json:/documents/resume"
  "resume.metadata.example.json:/documents/metadata"
  "resume.skill.example.json:/documents/skill"
)

die() { printf '\nerror: %s\n' "$*" >&2; exit 1; }

command -v curl    >/dev/null || die "curl is required"
command -v python3 >/dev/null || die "python3 is required"

# ---------------------------------------------------------------- inputs ----

BASE_URL="${BASE_URL:-}"
if [[ -z "$BASE_URL" ]]; then
  read -rp "Base URL [http://localhost:8000]: " BASE_URL
  BASE_URL="${BASE_URL:-http://localhost:8000}"
fi
BASE_URL="${BASE_URL%/}"   # a trailing slash would produce //documents/...

ADMIN_USERNAME="${ADMIN_USERNAME:-}"
if [[ -z "$ADMIN_USERNAME" ]]; then
  read -rp "Admin username: " ADMIN_USERNAME
fi
[[ -n "$ADMIN_USERNAME" ]] || die "a username is required"

# Read rather than accept as an argument: an argument lands in shell history and
# in the process list. -s so it is not echoed.
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
if [[ -z "$ADMIN_PASSWORD" ]]; then
  read -rsp "Admin password: " ADMIN_PASSWORD
  echo
fi
[[ -n "$ADMIN_PASSWORD" ]] || die "a password is required"

# -------------------------------------------------------------- validate ----

echo
echo "Checking documents in $DOCS_DIR"

missing=0
for entry in "${DOCUMENTS[@]}"; do
  IFS=: read -r filename route <<<"$entry"
  path="$DOCS_DIR/$filename"

  if [[ ! -f "$path" ]]; then
    printf '  %-30s missing\n' "$filename"
    missing=$((missing + 1))
    continue
  fi

  # The request body is a CreateDocumentRequest: name, revision_note, data
  # (and optionally public). Catching a malformed one here turns a 422 with a
  # pydantic trace into a line of English, and stops a half-finished run
  # partway through the set.
  note=$(python3 - "$path" <<'PY'
import json, sys

path = sys.argv[1]
try:
    with open(path) as handle:
        body = json.load(handle)
except json.JSONDecodeError as exc:
    sys.exit(f"not valid JSON: {exc}")

if not isinstance(body, dict):
    sys.exit("expected a JSON object")

missing = [k for k in ("name", "revision_note", "data") if k not in body]
if missing:
    sys.exit(f"missing key(s): {', '.join(missing)}")

print(str(body["revision_note"])[:44])
PY
  ) || { printf '  %-30s %s\n' "$filename" "$note"; missing=$((missing + 1)); continue; }

  printf '  %-30s ok — %s\n' "$filename" "$note"
done

[[ $missing -eq 0 ]] || die "$missing document(s) unusable; nothing was uploaded"

if [[ "$DRY_RUN" == true ]]; then
  echo
  echo "Dry run: all documents valid, nothing uploaded."
  exit 0
fi

# ----------------------------------------------------------------- login ----

echo
echo "Logging in to $BASE_URL"

login_response=$(curl -fsS -X POST "$BASE_URL/token" \
  --data-urlencode "username=$ADMIN_USERNAME" \
  --data-urlencode "password=$ADMIN_PASSWORD" \
  2>&1) || die "login failed: $login_response"

TOKEN=$(printf '%s' "$login_response" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin)["access_token"])
except Exception:
    sys.exit(1)
') || die "login succeeded but returned no access_token"

unset ADMIN_PASSWORD
echo "  authenticated as $ADMIN_USERNAME"

# ---------------------------------------------------------------- upload ----

echo
echo "Uploading"

failed=0
for entry in "${DOCUMENTS[@]}"; do
  IFS=: read -r filename route <<<"$entry"
  path="$DOCS_DIR/$filename"

  response=$(curl -sS -X POST "$BASE_URL$route" \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    --data-binary "@$path" \
    -w '\n%{http_code}')

  status="${response##*$'\n'}"
  body="${response%$'\n'*}"

  if [[ "$status" == "200" || "$status" == "201" ]]; then
    revision=$(printf '%s' "$body" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin)["revision_id"])
except Exception:
    print("?")
')
    printf '  %-30s revision %s\n' "$filename" "$revision"
  else
    printf '  %-30s HTTP %s — %s\n' "$filename" "$status" "${body:0:200}"
    failed=$((failed + 1))
  fi
done

echo
if [[ $failed -gt 0 ]]; then
  die "$failed document(s) failed to upload"
fi

echo "All three documents uploaded."
echo
echo "Verify the public projection is being served (only if resume.example.json"
echo "was uploaded with \"public\": true):"
echo "  curl -s $BASE_URL/public/$ADMIN_USERNAME/resume/resume.example.json | head -c 200"
