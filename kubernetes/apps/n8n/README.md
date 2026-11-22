# n8n — Workflow Automation

Self-hosted workflow automation platform. Used for AI pipeline orchestration, WOL (Wake-on-LAN) automation, home automation integrations, and scheduled data tasks.

## Key Workflows

| Workflow | Trigger | Purpose |
| -------- | ------- | ------- |
| WOL — NUC | RPi4 RAM > 75% | Wake Intel NUC K8s worker via UpSnap |
| WOL — i9 GPU | Webhook / Cron | Wake i9 burst node for GPU workloads |
| mem0 sync | Webhook | Push facts to mem0 from external tools |
| Beszel alerts | Webhook | Parse Beszel metric alerts, notify via Gotify |
| Monitoring Watchdog (`workflows/monitoring-watchdog.json`) | Every 5 min | Probes Grafana, VictoriaMetrics, Loki (CT405) and Gotify from outside. Down after 2 misses (~10 min): Gotify app *Homelab Watchdog*, or Gmail if Gotify is the one down. Sends a reminder every ~6 h and a message on recovery. Grafana watches back via `up{job="n8n"}` (`N8N_METRICS=true`). |

## Configuration

| Variable | Notes |
| -------- | ----- |
| `N8N_DOMAIN` | External URL — set in `deployment.yaml` env `WEBHOOK_URL` |
| Postgres secret | Encrypted in `secret.enc.yaml` via SOPS |

**Storage:** n8n data lives on an NFS-backed PVC (`pvc.yaml`). NFS server IP: see `cluster.env.example`.

## Access

n8n is exposed via NodePort and proxied through Nginx Proxy Manager with SSL. Default internal port: `5678`.

## Troubleshooting

**Workflows not triggering webhooks** — Check `WEBHOOK_URL` env var matches your external domain. n8n uses this for self-referencing webhook URLs.

**Execution data lost after restart** — Confirm NFS PVC is mounted and healthy: `kubectl describe pvc -n n8n`.
