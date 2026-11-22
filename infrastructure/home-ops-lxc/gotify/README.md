# Gotify (CT203): apps and retention

Gotify keeps every message forever. On 2026-11-22 it held 20,604 messages (about 145 a
day), 77% of them from the three Miniflux apps. That's why the web UI and phone app were
slow to load. `prune.py` deletes old messages per app through the Gotify API, and a
systemd timer runs it daily at 04:30 Central.

## Retention (edit `RETENTION_DAYS` in `prune.py`)

| Days | Apps |
|---|---|
| 7 | Miniflux Curator Deals, Filtered Deals, Miniflux Randoms |
| 14 | miniflux-curator (curator summaries) |
| 30 | Beszel-Monitoring, Hindsight Memory Notifications, n8n HA workflows, RSS Feeds, Hermes ssh_exec, Job Application |
| 90 | Grafana · * alert apps, n8n Errors, n8n Backup Worflow Notifications, Gmail Summaries, and any app not listed |

Retention matches on the **exact app name**. If you rename an app in Gotify, rename it
here too, or the app falls back to 90 days.

## Grafana alert apps

Create these in **Gotify › Apps** with the **default priority** shown. Grafana's webhook
sends no priority, so the app default decides how loud the phone gets. Put each token in
`infrastructure/observability-lxc/.env.enc.yaml`.

| App | Default priority | Token variable | Receives |
|---|---|---|---|
| Grafana · Critical | 8 | `GOTIFY_TOKEN_CRITICAL` | every `severity=critical` alert, any time |
| Grafana · Security | 7 | `GOTIFY_TOKEN_SECURITY` | Suricata IDS, any time |
| Grafana · Kubernetes | 5 | `GOTIFY_TOKEN_KUBERNETES` | K8s/Flux warnings, muted 23:00–07:00 |
| Grafana · Network | 5 | `GOTIFY_TOKEN_NETWORK` | OPNsense warnings, muted 23:00–07:00 |
| Grafana · Infrastructure | 4 | `GOTIFY_TOKEN_INFRA` | host/exporter warnings, muted 23:00–07:00 |

The old **Grafana-alerts** app (0 messages) can be deleted once the new ones work. Grafana's
old token matched no Gotify app, so nothing was ever delivered to it.

## Install / update (CT203)

1. Gotify UI › **Clients** › create `prune-job`. Add its token to
   `infrastructure/home-ops-lxc/.env.enc.yaml` as `GOTIFY_PRUNE_TOKEN` (`sops edit` on the
   Mac), then refresh `.env` on CT203 the same way as on CT405 (decrypt on the Mac, pipe
   it through `pct exec 203`).
2. Dry run first. It deletes nothing:
   ```bash
   cd /root/Homelab-ops && git pull
   set -a; . infrastructure/home-ops-lxc/.env; set +a
   python3 infrastructure/home-ops-lxc/gotify/prune.py
   ```
3. Install the timer:
   ```bash
   ln -sf /root/Homelab-ops/infrastructure/home-ops-lxc/gotify/gotify-prune.{service,timer} /etc/systemd/system/
   systemctl daemon-reload && systemctl enable --now gotify-prune.timer
   systemctl start gotify-prune.service && journalctl -u gotify-prune -n 30   # first run: ~15k deletes
   ```
