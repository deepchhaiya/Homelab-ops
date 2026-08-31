# miniflux/curation — AI feed triage + feed taxonomy (Phase 29)

Turns the Miniflux firehose into a few ranked Gotify pushes, and keeps the
subscription list organised. Full writeup:
[`Phase-29 - miniflux-ai-feed-curation.md`](../../../../Phase-29%20-%20miniflux-ai-feed-curation.md).

> **Repo is public.** The real scoring profile (role, goals, employer, dates)
> lives only in the SOPS secret (`n8n-secrets` key `CURATOR_PROFILE`) and in the
> git-ignored `interest-profile.local.md`. Nothing personal goes in the committed
> files or the workflow JSON.

| File | What it is |
| ---- | ---------- |
| `interest-profile.md` | **Generic template** (safe to commit). Explains the scoring rubric and the generic fallback prompt. |
| `interest-profile.local.md` | **Git-ignored.** Your real, personalised profile. Copy from `interest-profile.local.md.example` and fill in. |
| `sync-profile.sh` | Reads `interest-profile.local.md` and writes it into the `CURATOR_PROFILE` key of `../../n8n/secret.enc.yaml` via `sops --set` — no plaintext on disk. |
| `feeds.csv` | Desired-state list, `feed_url,category[,keeplist_regex]` (10 categories). Source for both scripts. Column 3 is an optional Miniflux keep-list regex (title match, no commas). |
| `feeds-merged.opml` | The same list as OPML with per-feed `miniflux:userAgent` / `disableHTTP2` for Reddit + HN. Import path; keep-list rules are script-only. |
| `add-feeds.sh` | Subscribe every CSV feed + create categories + apply per-feed hygiene (reddit browser-UA + `disable_http2`, keep-list from column 3, `crawler:false`). Idempotent — re-syncs hygiene on feeds that already exist. |
| `recategorize.sh` | Move feeds **already** subscribed to their `feeds.csv` category (OPML import won't re-file them) and, with `--apply`, push the same hygiene. Dry-run by default. |

## Categories

`Career-FDE` · `Applied-AI-Agents` · `AI-Infra-Inference` · `Object-Storage-Data`
· `Cloud-DevOps` · `Homelab-K8s` · `Security-SRE` · `HN-Trending` · `Digests` ·
`Deals`

## First-time / re-org flow

Scripts need `curl` + `jq` (`brew install jq`). Invoke with `bash <script>` —
this dir is a Nextcloud sync mount where the execute bit doesn't stick, and the
scripts are written for macOS Bash 3.2.

```bash
export MINIFLUX_URL="http://192.168.4.141:30080"     # or https://miniflux.dkghar.duckdns.org
export MINIFLUX_TOKEN="<Settings -> API Keys>"
curl -sS -i -H "X-Auth-Token: $MINIFLUX_TOKEN" "$MINIFLUX_URL/v1/me"   # expect HTTP 200

# 1. Import feeds-merged.opml via the Miniflux UI (Settings -> Import).
#    New feeds land in the right category; existing feeds are skipped (not moved).

# 2. Re-file the pre-existing feeds + drop the superseded Reddit .rss ones:
bash recategorize.sh                              # preview
bash recategorize.sh --apply --supersede-reddit   # execute

# 3. Backfill anything still missing:
bash add-feeds.sh
```

`recategorize.sh --prune` also deletes feeds that aren't in `feeds.csv` — review
the dry-run first.

If you skip the OPML entirely, `bash add-feeds.sh` alone does everything for new
feeds (subscribe + categories + hygiene); `recategorize.sh` is only needed to
re-file feeds you were already subscribed to before Phase 29.

## Per-feed hygiene — handled by the scripts

No UI clicking needed. Both scripts set, per feed:

- **reddit** → browser `user_agent` + `disable_http2` (the default UA gets 403).
- **keep-list regex** (column 3 of `feeds.csv`) → `keeplist_rules`; non-matching
  titles are dropped at ingest so the AI scorer never sees them. Currently set on
  `r/LocalLLaMA`, `r/selfhosted`, `r/homelab`, `r/Proxmox`, `r/aws`,
  `AWS Recent Announcements`, `HN frontpage`.
- **all feeds** → `crawler:false` (store excerpt only, keeps Postgres small).

Optional durability extra: also set `HTTP_CLIENT_USER_AGENT` globally on the
Miniflux Deployment (Phase 29 §A.3).

## Scoring profile

```bash
cd kubernetes/apps/miniflux/curation
cp interest-profile.local.md.example interest-profile.local.md   # first time
$EDITOR interest-profile.local.md                                # fill in the <placeholders>
export SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt
bash sync-profile.sh                                             # -> CURATOR_PROFILE in secret.enc.yaml
git add ../../n8n/secret.enc.yaml && git commit -m "chore: update curator profile"
```

The n8n `Prep entries` node reads `$env.CURATOR_PROFILE`; if unset it falls back
to a generic prompt baked into the workflow. Edit → `sync-profile.sh` → restart
n8n (or `kubectl rollout restart deploy/n8n -n n8n`) to pick up the new value.

## n8n side

Workflows `miniflux-curator` + `miniflux-curator-digest` live in
[`../../n8n/workflows/`](../../n8n/workflows/). Env on the n8n Deployment:
`MINIFLUX_URL` (use in-cluster `http://miniflux.miniflux.svc.cluster.local:8080`),
`MINIFLUX_TOKEN`, `OLLAMA_URL`, `OLLAMA_MODEL`, `GOTIFY_URL`,
`GOTIFY_CURATOR_TOKEN`, `CURATOR_PROFILE`, `CURATOR_DIGEST_PROFILE`. All from
`kubernetes/apps/n8n/secret.enc.yaml`.
