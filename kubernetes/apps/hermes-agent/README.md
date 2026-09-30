# hermes-agent

[Hermes Agent](https://hermes-agent.nousresearch.com/) (Nous Research) — a multi-LLM agent that runs as a single K8s pod on the Evo-X2 worker. Exposes an **OpenAI-compatible HTTP API** on port 8642 so Open WebUI (and anything else that speaks OpenAI's API) can talk to it. The lab brain.

## What's in here

| File | What |
|---|---|
| `namespace.yaml` | Namespace with `pod-security.kubernetes.io/enforce: baseline` (Hermes runs as root + needs unrestricted shell for subagents) |
| `rbac.yaml` | ServiceAccount + ClusterRole `hermes-agent-reader` — read-only across pods/services/nodes/events/configmaps/deployments etc. |
| `pvc.yaml` | 10 Gi `hermes-data` PVC (local-path on the Evo-X2 worker). Holds config, sessions, skills, memories. |
| `secret.enc.yaml` | SOPS-encrypted credentials (API key, LLM provider keys, HA token, Proxmox token, Beszel + Grafana access) |
| `secret.example.yaml` | Plaintext template documenting every key |
| `configmap-hermes.yaml` | Seed `~/.hermes/config.yaml` — model wiring, fallback chain, platforms |
| `deployment.yaml` | Single replica, `nodeSelector: tier=ai-worker`, init container syncs config + skill on every rollout |
| `service.yaml` | ClusterIP `:8642` for in-cluster OpenAI-compat API |
| `kustomization.yaml` | Includes everything + auto-generates skill ConfigMaps from `skills/system-administrator/` files |
| `skills/system-administrator/` | The lab's brain — `SKILL.md` topology doc + 7 helper scripts |

## Models

| Slot | Model | How it's reached |
|---|---|---|
| Primary | `qwen3.6:35b-a3b` (Q4 MoE) | Ollama on the Evo-X2 **host** at `http://192.168.4.84:11434/v1` (Phase 22a — not in K8s) |
| Fallback (on error) | `gemini-3.7-flash` | Gemini API (`GEMINI_API_KEY`) |
| Compression (auxiliary) | `gemini-3.5-flash-lite` | Gemini API (`GEMINI_API_KEY`) |

No paid-Claude fallback: Claude Code is the only paid agent, and free agents must not fall
back to it. `gemini-2.5-flash` was dropped on 2026-09-30 (the API returns 404 for it).

In-session mid-conversation overrides via `/model <provider>:<model>` — see Hermes docs.

## The `system-administrator` skill

The skill is the homelab brain. Its `SKILL.md` embeds the **complete lab topology** — every host, LXC, VM, K8s namespace, service URL, port, IP, secret reference, plus a hard-pitfalls list (do NOT `kubectl delete` hindsight/n8n/flux-system/kube-system without same-message confirmation, etc.).

Seven helper scripts (pure-stdlib Python, read-only):

| Script | What it queries | Auth |
|---|---|---|
| `k8s_status.py` | K8s API: pods, namespaces, restart counts, by node/name | in-cluster ServiceAccount |
| `service_health.py` | HTTP `/health` of known services (Ollama, Hindsight, VM, Grafana, Loki, PBS, Gotify, NPM, Telegraf, Hermes self) | none |
| `vm_query.py` | VictoriaMetrics — PromQL/MetricsQL queries, `--suggest` for starter queries | none |
| `loki_query.py` | Loki — LogQL queries, `--suggest`, `--labels`, `--label-values` | none |
| `proxmox_status.py` | Proxmox VE — node + VM/LXC status on Peladn + Evo-X2 | `PROXMOX_TOKEN_ID/VALUE` (PVEAuditor role) |
| `beszel_query.py` | Beszel server (PocketBase) — systems, alerts, system_stats, container_stats | `BESZEL_USER/PASSWORD` (dedicated `hermes-reader` user) |
| `grafana_query.py` | Grafana API — dashboards, alerts, alert rules, datasources, folders, annotations | `GRAFANA_TOKEN` (service-account, Viewer role) |

## Quick start

### 0. Generate API key

```bash
openssl rand -hex 32   # save this; goes in BOTH hermes-credentials and open-webui-credentials Secrets
```

### 1. Fill in real values (do not commit plaintext)

```bash
cp secret.example.yaml secrets.yaml
# edit secrets.yaml — at minimum set API_SERVER_KEY; the rest unlocks more features
```

| Set this | To unlock |
|---|---|
| `API_SERVER_KEY` | **required** — Hermes refuses to bind 0.0.0.0 without it; same value as Open WebUI's `OPENAI_API_KEY` |
| `GEMINI_API_KEY` | Gemini fallback + compression |
| `HASS_TOKEN` | `ha_*` tools (4) — list entities, get/set state, call services |
| `PROXMOX_TOKEN_ID/VALUE` | `proxmox_status.py` |
| `BESZEL_USER/PASSWORD` | `beszel_query.py` |
| `GRAFANA_TOKEN` | `grafana_query.py` |

### 2. Encrypt + commit

```bash
cp secrets.yaml secret.enc.yaml
sops --encrypt --in-place secret.enc.yaml
git add secret.enc.yaml
# DO NOT commit secrets.yaml
```

### 3. Push — Flux applies

```bash
git commit -m "hermes-agent: rotate credentials"
git push
kubectl annotate -n flux-system kustomization/apps reconcile.fluxcd.io/requestedAt=$(date +%s) --overwrite
```

## Operate

| Want | How |
|---|---|
| Use Hermes from a browser | `kubectl -n open-webui port-forward svc/open-webui 8080:80` → http://localhost:8080 → pick `hermes-agent` |
| Interactive CLI (Git Bash) | `kubectl -n hermes-agent exec -it deploy/hermes-agent -c hermes -- sh -c hermes` |
| Interactive CLI (PowerShell) | `kubectl -n hermes-agent exec -it deploy/hermes-agent -c hermes -- hermes` |
| Tail logs | `kubectl -n hermes-agent logs -f deploy/hermes-agent -c hermes` |
| Rollout-restart (after skill edits) | `kubectl -n hermes-agent rollout restart deploy/hermes-agent` |
| Test the API directly | `curl -H "Authorization: Bearer $KEY" http://hermes-agent.hermes-agent.svc:8642/v1/models` |

## How it all wires together

```
Browser
  ↓ port-forward
Open WebUI (Evo-X2)
  ├─→ Ollama @ host (qwen3.6:35b-a3b)       — direct chat with the model
  └─→ Hermes API (this app)                  — agent mode (skills + tools)
        ↓
   system-administrator skill
        ├─→ k8s_status.py       (in-cluster API)
        ├─→ service_health.py   (12 services)
        ├─→ vm_query.py         (VictoriaMetrics @ 192.168.4.66:8428)
        ├─→ loki_query.py       (Loki @ 192.168.4.66:3100)
        ├─→ proxmox_status.py   (Peladn / Evo-X2 PVE APIs)
        ├─→ beszel_query.py     (Beszel @ in-cluster:8090)
        └─→ grafana_query.py    (Grafana @ 192.168.4.66:3000)

  Inbound webhooks (e.g. Beszel alerts) → n8n → Hermes /v1/chat/completions
```

## Notes / gotchas

- The image's default entrypoint launches the **interactive CLI**, which exits immediately on no-TTY (`fd=0 is not a terminal`). Always run with `args: ["gateway", "run"]` (set in `deployment.yaml`).
- `gateway run` refuses to start if any messaging platform is **enabled but not configured**. WhatsApp/Signal/Telegram/Discord are intentionally `enabled: false` in the configmap — Open WebUI is the user-facing surface; messaging is optional.
- Probes are **exec-based** (`pgrep -f "hermes gateway run"`) because the HTTP API only opens on `:8642` when `API_SERVER_ENABLED=true`. We set that, so HTTP probes would also work — but exec probes survive any future config-state changes.
- Field-manager ownership: don't `kubectl apply` this Secret manually. Flux owns it. If you did, delete the Secret first then let Flux re-apply it from `secret.enc.yaml`.
- Init container **always overwrites** `/opt/data/config.yaml` and the skill files. Hermes' own runtime `/model` mutations get lost on rollout — by design, GitOps edits propagate cleanly.

## See also

- [Phase 22 implementation doc](../../../../Phase-22-Part-1%20-%20hermes-agent-design-research.md) — design rationale + alternatives considered
- [Ollama-on-host README](../../../infrastructure/ollama-host/README.md) — the LLM backend
- [Beszel-Hermes integration doc](../../../../Beszel-Hermes-Integration.md) — the Beszel API tool + n8n webhook bridge
