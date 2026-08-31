#!/usr/bin/env bash
#
# Subscribe every feed in feeds.csv to Miniflux, create categories, and apply
# per-feed hygiene:
#   * reddit feeds  -> browser user-agent + disable_http2  (default UA gets 403)
#   * column 3      -> keeplist_rules regex (drop non-matching titles at ingest)
#   * all feeds     -> crawler:false (store excerpt only, keep Postgres small)
#
# Idempotent: existing categories are reused; feeds that already exist get their
# hygiene re-synced (PUT) instead of being skipped. 5xx gets one retry.
#
# Portable: macOS Bash 3.2 (no associative arrays). Needs curl + jq.
#
# Usage:
#   export MINIFLUX_URL="http://192.168.4.141:30080"
#   export MINIFLUX_TOKEN="xxxx"
#   bash add-feeds.sh [feeds.csv]
set -eu

CSV="${1:-$(dirname "$0")/feeds.csv}"
: "${MINIFLUX_URL:?set MINIFLUX_URL}"
: "${MINIFLUX_TOKEN:?set MINIFLUX_TOKEN}"
MINIFLUX_URL="${MINIFLUX_URL%/}"
command -v jq >/dev/null || { echo "!! jq not found (brew install jq)"; exit 1; }

UA_BROWSER="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
CATCACHE="$(mktemp)"; FEEDMAP="$(mktemp)"
trap 'rm -f "$CATCACHE" "$FEEDMAP"' EXIT

api() {  # api METHOD PATH [json] -> body then trailing line with HTTP code
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

echo ">> Miniflux: ${MINIFLUX_URL}"
me="$(api GET /v1/me)"
[ "$(code "$me")" = "200" ] || { echo "!! auth failed (HTTP $(code "$me")): $(body "$me")"; exit 1; }
echo ">> authenticated as: $(body "$me" | jq -r .username)"

api GET /v1/categories | sed '$d' | jq -r '.[] | "\(.id)\t\(.title)"' > "$CATCACHE"
api GET /v1/feeds      | sed '$d' | jq -r '.[] | "\(.feed_url)\t\(.id)"'  > "$FEEDMAP"

get_cat_id() {
  t="$1"
  id="$(awk -F'\t' -v t="$t" '$2==t{print $1; exit}' "$CATCACHE")"
  if [ -n "$id" ]; then printf '%s' "$id"; return; fi
  resp="$(api POST /v1/categories "$(jq -nc --arg t "$t" '{title:$t}')")"
  if [ "$(code "$resp")" = "201" ]; then id="$(body "$resp" | jq -r .id)"
  else id="$(api GET /v1/categories | sed '$d' | jq -r --arg t "$t" '.[]|select(.title==$t)|.id')"; fi
  [ -n "$id" ] && printf '%s\t%s\n' "$id" "$t" >> "$CATCACHE"
  printf '%s' "$id"
}
feed_id_for() { awk -F'\t' -v u="$1" '$1==u{print $2; exit}' "$FEEDMAP"; }

# hygiene_json <url> <keeplist>  -> JSON object with only the hygiene keys that apply
hygiene_json() {
  u="$1"; kl="$2"; reddit=0
  case "$u" in *reddit.com/*) reddit=1 ;; esac
  jq -nc --arg ua "$UA_BROWSER" --arg kl "$kl" --argjson r "$reddit" '
    {crawler:false}
    + (if $r==1 then {user_agent:$ua, disable_http2:true} else {} end)
    + (if ($kl|length)>0 then {keeplist_rules:$kl} else {} end)'
}

post_feed() {  # <json> -> "code<TAB>msg", one retry on 5xx
  b="$1"; r="$(api POST /v1/feeds "$b")"; c="$(code "$r")"
  case "$c" in 5*) sleep 3; r="$(api POST /v1/feeds "$b")"; c="$(code "$r")" ;; esac
  printf '%s\t%s' "$c" "$(body "$r" | jq -r '.error_message // .feed_id // empty' 2>/dev/null || true)"
}

added=0; synced=0; skipped=0; failed=0
while IFS=',' read -r feed_url category keeplist || [ -n "${feed_url:-}" ]; do
  case "$feed_url" in ''|\#*|feed_url) continue ;; esac
  feed_url="$(trim "$feed_url")"; category="$(trim "${category:-Uncategorized}")"
  keeplist="$(trim "${keeplist:-}")"
  [ -n "$feed_url" ] || continue

  cat_id="$(get_cat_id "$category")"
  [ -n "$cat_id" ] || { echo "  !! no category id for '$category' -- $feed_url"; failed=$((failed+1)); continue; }
  hyg="$(hygiene_json "$feed_url" "$keeplist")"
  tag=''; [ -n "$keeplist" ] && tag=' +keeplist'
  case "$feed_url" in *reddit.com/*) tag="$tag +UA" ;; esac

  fid="$(feed_id_for "$feed_url")"
  if [ -n "$fid" ]; then
    rc="$(code "$(api PUT "/v1/feeds/$fid" "$(printf '%s' "$hyg" | jq -c --argjson c "$cat_id" '. + {category_id:$c}')")")"
    if [ "$rc" = "201" ] || [ "$rc" = "200" ] || [ "$rc" = "204" ]; then
      echo "  == [$category] $feed_url (synced$tag)"; synced=$((synced+1))
    else
      echo "  !! [$category] $feed_url (PUT HTTP $rc)"; failed=$((failed+1))
    fi
    sleep 0.3; continue
  fi

  reqbody="$(printf '%s' "$hyg" | jq -c --arg u "$feed_url" --argjson c "$cat_id" '. + {feed_url:$u, category_id:$c}')"
  res="$(post_feed "$reqbody")"; c="${res%%	*}"; msg="${res#*	}"
  case "$c" in
    201) echo "  ++ [$category] $feed_url$tag"; added=$((added+1)) ;;
    4*)  case "$msg" in
           *already*exist*|*duplicat*) echo "  == [$category] $feed_url (already there)"; skipped=$((skipped+1)) ;;
           *) echo "  !! [$category] $feed_url  (HTTP $c: ${msg:-unknown})"; failed=$((failed+1)) ;;
         esac ;;
    *)   echo "  !! [$category] $feed_url  (HTTP $c: ${msg:-unknown})"; failed=$((failed+1)) ;;
  esac
  sleep 0.5
done < "$CSV"

echo
echo ">> done. added=$added  synced=$synced  skipped=$skipped  failed=$failed"
[ "$failed" -eq 0 ]
