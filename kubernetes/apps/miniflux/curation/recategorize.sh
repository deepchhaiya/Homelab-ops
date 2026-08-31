#!/usr/bin/env bash
#
# Reconcile feeds ALREADY subscribed in Miniflux against feeds.csv:
#   * move each to its feeds.csv category (OPML import won't re-file them)
#   * apply the same per-feed hygiene as add-feeds.sh (reddit UA + disable_http2,
#     keeplist_rules from column 3)                          [--apply only]
#   * optionally delete the old .../r/<sub>/.rss feeds superseded by top.rss
#
# Portable: macOS Bash 3.2. Needs curl + jq. Matching is by exact feed_url.
#
# Usage:
#   export MINIFLUX_URL="http://192.168.4.141:30080"
#   export MINIFLUX_TOKEN="xxxx"
#   bash recategorize.sh                              # dry run
#   bash recategorize.sh --apply                      # move + apply hygiene
#   bash recategorize.sh --apply --supersede-reddit   # + delete old r/*/.rss
#   bash recategorize.sh --apply --prune              # + delete feeds not in csv
set -eu

CSV="$(dirname "$0")/feeds.csv"
APPLY=0; PRUNE=0; SUPERSEDE=0
for a in "$@"; do case "$a" in
  --apply) APPLY=1 ;; --prune) PRUNE=1 ;; --supersede-reddit) SUPERSEDE=1 ;;
  *) echo "unknown arg: $a"; exit 2 ;;
esac; done

: "${MINIFLUX_URL:?set MINIFLUX_URL}"; : "${MINIFLUX_TOKEN:?set MINIFLUX_TOKEN}"
MINIFLUX_URL="${MINIFLUX_URL%/}"
command -v jq >/dev/null || { echo "!! jq not found (brew install jq)"; exit 1; }

UA_BROWSER="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
WANT="$(mktemp)"; CATCACHE="$(mktemp)"; SEEN="$(mktemp)"
trap 'rm -f "$WANT" "$CATCACHE" "$SEEN"' EXIT
TAB="$(printf '\t')"

api() {
  m="$1"; p="$2"; b="${3:-}"
  if [ -n "$b" ]; then
    curl -sS -X "$m" "${MINIFLUX_URL}${p}" -H "X-Auth-Token: ${MINIFLUX_TOKEN}" \
      -H "Content-Type: application/json" -w '\n%{http_code}' -d "$b"
  else
    curl -sS -X "$m" "${MINIFLUX_URL}${p}" -H "X-Auth-Token: ${MINIFLUX_TOKEN}" -w '\n%{http_code}'
  fi
}
body() { printf '%s\n' "$1" | sed '$d'; }
code() { printf '%s\n' "$1" | tail -n1; }
trim() { printf '%s' "$1" | awk '{sub(/^[ \t\r]+/,"");sub(/[ \t\r]+$/,"");print}'; }

hygiene_json() {  # <url> <keeplist> -> JSON with only the keys that apply
  u="$1"; kl="$2"; reddit=0
  case "$u" in *reddit.com/*) reddit=1 ;; esac
  jq -nc --arg ua "$UA_BROWSER" --arg kl "$kl" --argjson r "$reddit" '
    {} + (if $r==1 then {user_agent:$ua, disable_http2:true} else {} end)
       + (if ($kl|length)>0 then {keeplist_rules:$kl} else {} end)'
}

me="$(api GET /v1/me)"
[ "$(code "$me")" = "200" ] || { echo "!! auth failed (HTTP $(code "$me"))"; exit 1; }
echo ">> authenticated as: $(body "$me" | jq -r .username)"

# desired state: url <TAB> category <TAB> keeplist
while IFS=',' read -r u c kl || [ -n "${u:-}" ]; do
  case "$u" in ''|\#*|feed_url) continue ;; esac
  u="$(trim "$u")"; c="$(trim "$c")"; kl="$(trim "${kl:-}")"
  [ -n "$u" ] && printf '%s\t%s\t%s\n' "$u" "$c" "$kl" >> "$WANT"
done < "$CSV"

api GET /v1/categories | sed '$d' | jq -r '.[] | "\(.id)\t\(.title)"' > "$CATCACHE"
ensure_cat() {
  t="$1"
  id="$(awk -F'\t' -v t="$t" '$2==t{print $1; exit}' "$CATCACHE")"
  if [ -n "$id" ]; then printf '%s' "$id"; return; fi
  if [ "$APPLY" = 1 ]; then
    id="$(api POST /v1/categories "$(jq -nc --arg t "$t" '{title:$t}')" | sed '$d' | jq -r '.id // empty')"
    [ -z "$id" ] && id="$(api GET /v1/categories | sed '$d' | jq -r --arg t "$t" '.[]|select(.title==$t)|.id')"
    [ -n "$id" ] && printf '%s\t%s\n' "$id" "$t" >> "$CATCACHE"
    printf '%s' "$id"
  else printf 'NEW'; fi
}

feeds="$(api GET /v1/feeds)"
[ "$(code "$feeds")" = "200" ] || { echo "!! GET /v1/feeds -> $(code "$feeds")"; exit 1; }
feeds_tsv="$(body "$feeds" | jq -r '.[] | [.id, (.category.title // "-"), .feed_url, .title] | @tsv')"

moved=0; hyg=0; ok=0; orphan=0; missing=0; superseded=0
while IFS="$TAB" read -r id cur url title; do
  [ -n "${id:-}" ] || continue
  line="$(awk -F'\t' -v u="$url" '$1==u{print; exit}' "$WANT")"

  if [ -z "$line" ]; then
    case "$url" in *reddit.com/r/*/.rss*|*reddit.com/r/*/.rss)
      if [ "$SUPERSEDE" = 1 ]; then
        echo "  ~~ supersede (old reddit)  $title"
        [ "$APPLY" = 1 ] && api DELETE "/v1/feeds/$id" >/dev/null
        superseded=$((superseded+1)); continue
      fi ;;
    esac
    echo "  ?? orphan (not in feeds.csv) [$cur]  $title"
    orphan=$((orphan+1))
    if [ "$PRUNE" = 1 ] && [ "$APPLY" = 1 ]; then api DELETE "/v1/feeds/$id" >/dev/null; echo "     deleted"; fi
    continue
  fi

  printf '%s\n' "$url" >> "$SEEN"
  want="$(printf '%s' "$line" | cut -f2)"
  keeplist="$(printf '%s' "$line" | cut -f3-)"
  note=''

  if [ "$cur" != "$want" ]; then
    cid="$(ensure_cat "$want")"
    note="move [$cur]->[$want]"; moved=$((moved+1))
    [ "$APPLY" = 1 ] && api PUT "/v1/feeds/$id" "$(jq -nc --argjson c "$cid" '{category_id:$c}')" >/dev/null
  else
    ok=$((ok+1))
  fi

  h="$(hygiene_json "$url" "$keeplist")"
  if [ "$h" != "{}" ]; then
    note="${note:+$note, }hygiene"
    [ "$APPLY" = 1 ] && api PUT "/v1/feeds/$id" "$h" >/dev/null && hyg=$((hyg+1))
  fi
  [ -n "$note" ] && echo "  -> $note  $title"
done <<EOF
$feeds_tsv
EOF

while IFS="$TAB" read -r u c kl; do
  [ -n "${u:-}" ] || continue
  if grep -qxF "$u" "$SEEN"; then continue; fi
  echo "  ++ missing (in csv, not subscribed) [$c]  $u"
  missing=$((missing+1))
done < "$WANT"

echo
if [ "$APPLY" = 1 ]; then pfx="APPLIED"; else pfx="DRY RUN"; fi
echo ">> $pfx: moved=$moved hygiene=$hyg ok=$ok orphan=$orphan superseded=$superseded missing=$missing"
[ "$APPLY" = 1 ] || echo ">> re-run with --apply to execute. add missing feeds with: bash add-feeds.sh"
