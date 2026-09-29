# hermes-family

A second Hermes Agent instance, parallel to [`hermes-agent`](../hermes-agent/), dedicated to family use through Open WebUI. Isolated memory, no admin tools, friendlier defaults.

## Why a separate instance vs. multi-user in one?

Hermes v0.16 doesn't yet support per-user memory isolation within a single process ([issue #11430](https://github.com/NousResearch/hermes-agent/issues/11430)). Family conversations would otherwise cross-contaminate with admin work. Also: the admin Hermes carries `ssh_run.py`, `ssh_exec.py`, `repo_read.py`, `git_propose.py` — none of which family should be able to invoke. Separate deployment = clean isolation at the deployment layer, no risk of clever prompts triggering admin operations.

## Differences from `hermes-agent`

| Aspect | hermes-agent (admin) | hermes-family |
|---|---|---|
| Namespace | `hermes-agent` | `hermes-family` |
| Default model | `qwen3.6:35b-a3b` (heavy reasoning) | `qwen3.6:35b-a3b` (same model; gemma4:e4b until 2026-09-29) |
| Hindsight bank | `deep` | `family` |
| Skills loaded | `system-administrator` (SSH, repo, git, observability) | None (plain chat + web search + image gen via toolsets) |
| Resources | 8 GiB RAM ceiling | 4 GiB RAM ceiling |
| PVC size | 10 Gi | 5 Gi |
| SSH key mounted | Yes (root on Peladn + Evo-X2) | **No** |
| Gotify push | Yes | **No** (would spam admin) |
| Proxmox / Grafana / Beszel / HA tokens | Yes | **None** |
| K8s ServiceAccount | Read-only across cluster | None (token not mounted) |
| Open WebUI exposure | All users (currently single-user) | Family Open WebUI accounts only |

## What family users CAN do

- General chat (questions, writing help, brainstorming, recipes, etc.)
- Vision queries (send an image, ask about it — qwen3.6 has vision)
- Web search (via SearXNG — same instance the admin Hermes uses, no extra cost)
- Image generation (Gemini image API via fallback provider — free tier)
- Memory: shared family pool. What one family member tells Hermes ("we're vegetarian", "Saturday is laundry day") is accessible to others.

## What family users CANNOT do

- SSH to any lab host (no key mounted)
- Run any admin script (none in this skill set)
- Read your homelab repo, propose patches, etc. (skills not loaded)
- Trigger Hermes admin's Gotify push (no token)
- Access mem0 entries under `user_id=deep` (mem0 enforces per-user namespacing)

## Deployment

### 1. Create the secret

```bash
cd Homelab-ops/kubernetes/apps/hermes-family
cp secret.example.yaml secret.enc.yaml
# Edit secret.enc.yaml — fill API_SERVER_KEY (openssl rand -hex 32) and GEMINI_API_KEY
sops --encrypt --in-place secret.enc.yaml
```

### 2. Commit + reconcile

```bash
git add kubernetes/apps/hermes-family/ kubernetes/apps/kustomization.yaml
git commit -m "hermes-family: second instance for family use (gemma4:e4b, mem0 user_id=family)"
git push
kubectl -n flux-system annotate kustomization/apps reconcile.fluxcd.io/requestedAt="$(date +%s)" --overwrite
kubectl -n hermes-family get pods -w
```

Expected: `hermes-family-...` pod reaches `1/1 Running` in ~60s.

### 3. Wire into Open WebUI

Open WebUI → **Admin Panel** → **Settings** → **Connections** → **+ Add**:

| Field | Value |
|---|---|
| Type | OpenAI |
| Base URL | `http://hermes-family.hermes-family.svc.cluster.local:8642/v1` |
| API Key | (the API_SERVER_KEY value from the secret you encrypted) |
| Display Name | `Hermes (family)` |

Save → the model should appear in the Model dropdown for users you grant access to.

### 4. Open WebUI multi-user setup

If you haven't already enabled signups:

Admin Panel → **Settings** → **Authentication**:

- **Enable Sign-Up**: ON (or leave off and manually create accounts via Admin → Users → +)
- **Default User Role**: `user` (NOT `admin`; admins can change settings)

Add accounts for each family member.

### 5. Restrict who sees which Hermes

Admin Panel → **Models** → click on each Hermes connection → **Permissions**:

- `Hermes (family)` → set to: **`user`** group (or specific family user IDs)
- `Hermes (admin)` → set to: **`admin`** group (just you)

This ensures family users in the dropdown only see `Hermes (family)`, not your admin Hermes.

## Verification — test memory isolation

After the family pod is up:

```bash
# Write a fact under family's user_id
curl -s -X POST http://192.168.4.141:30800/v1/memories \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"family pizza day is friday"}],"user_id":"family"}'

# Recall as family — should return the fact
curl -s -X POST http://192.168.4.141:30800/v1/memories/search \
  -H "Content-Type: application/json" \
  -d '{"query":"pizza","user_id":"family"}'

# Recall as deep — should NOT return the family fact
curl -s -X POST http://192.168.4.141:30800/v1/memories/search \
  -H "Content-Type: application/json" \
  -d '{"query":"pizza","user_id":"deep"}'
```

If the deep query doesn't surface "pizza day is friday", memory isolation is working.

## Operate

| Want | How |
|---|---|
| Tail logs | `kubectl -n hermes-family logs -f deploy/hermes-family` |
| Update config | edit `configmap-hermes.yaml` → commit → `kubectl -n hermes-family rollout restart deploy/hermes-family` |
| Rollback | `kubectl -n hermes-family rollout undo deploy/hermes-family` |
| Stop temporarily | `kubectl -n hermes-family scale deploy/hermes-family --replicas=0` |
| Remove entirely | Remove from `apps/kustomization.yaml`, commit, Flux will clean up |

## Phase 2 ideas (deferred)

- Per-person mem0 user_ids (if family wants individual private memory) — requires a small proxy that reads Open WebUI user from the request and rewrites MEM0_USER_ID. Open question whether worth the wiring.
- Wyoming voice via Home Assistant for family voice queries (no keyboard needed).
- Calendar/reminders skill if family wants shared task management.
- Dedicated quota on Gemini API per Hermes instance (admin Hermes vs family Hermes don't share — currently same key, free tier easily covers both).
