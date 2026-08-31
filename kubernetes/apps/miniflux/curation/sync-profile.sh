#!/usr/bin/env bash
#
# Push the local (git-ignored) interest profile into the SOPS-encrypted n8n
# secret as CURATOR_PROFILE. Nothing personal is ever written in plaintext to a
# tracked file. Re-run whenever you edit interest-profile.local.md.
#
#   bash sync-profile.sh                       # -> CURATOR_PROFILE from interest-profile.local.md
#   bash sync-profile.sh CURATOR_DIGEST_PROFILE interest-profile-digest.local.md
#
# Needs: sops (with the Age key available), jq. Then commit secret.enc.yaml.
set -eu

KEY="${1:-CURATOR_PROFILE}"
SRC="${2:-$(dirname "$0")/interest-profile.local.md}"
SEC="$(dirname "$0")/../../n8n/secret.enc.yaml"

command -v sops >/dev/null || { echo "!! sops not found"; exit 1; }
command -v jq   >/dev/null || { echo "!! jq not found (brew install jq)"; exit 1; }
[ -f "$SRC" ] || { echo "!! $SRC missing — copy interest-profile.local.md.example and fill it in"; exit 1; }
[ -s "$SEC" ] || { echo "!! $SEC missing/empty — 'git checkout -- $SEC' first"; exit 1; }

# JSON-encode the whole file (preserves newlines) and set it via sops
JSONVAL="$(jq -Rs . < "$SRC")"
sops --set "[\"stringData\"][\"$KEY\"] $JSONVAL" "$SEC"

echo ">> $KEY written into $(basename "$SEC") ($(wc -c < "$SRC" | tr -d ' ') bytes)"
sops --decrypt "$SEC" | grep -q "$KEY" && echo ">> verified: decrypts and contains $KEY"
echo ">> now: git add $SEC && git commit"
