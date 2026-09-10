# Peladn → Evo-X2 Failover (n8n-triggered, GitOps)

Restore Peladn's guests (Talos CP **201**, media-ai **202**, home-ops **203**) onto the
always-on **Evo-X2** if Peladn dies. The PBS backups already live on Evo-X2 (the 26 TB +
CT200 were migrated there — see `../../` Phase 21 docs), so failover is just **restore
locally → start**. No WOL, no sync, no second PBS.

**Step-by-step recovery runbook: [`CONTROL-PLANE-RECOVERY.md`](CONTROL-PLANE-RECOVERY.md)**
(the older narrative `Phase-21-Part-2 - failover-runbook-evox2.md` in the docs root
predates the merged workflow and the etcd-snapshot layer).

## Files

| File | Runs on | Purpose |
|---|---|---|
| `peladn-failover.sh` | Evo-X2 host | Restores 201/202/203 from `pbs-local` and starts them. Has a split-brain guard (refuses if Peladn API is still reachable; `FORCE=1` to override). |
| `evox2-readiness.sh` | Evo-X2 host | Idempotent prep: adds `pbs-local` storage, creates stub dirs, installs the failover script. |
| `CONTROL-PLANE-RECOVERY.md` | — | The runbook: detection → restore VM 201 (Path A) or etcd-snapshot recover (Path B) → verify → fail back. |
| `.env` (gitignored) | — | `PBS_TOKEN_SECRET=…` for `evox2-readiness.sh` (only when adding `pbs-local` on a fresh host). |

> The n8n side is **one** workflow — `peladn-failover` (`j4GQTKqjKS9EJGQD`) —
> exported at [`../../kubernetes/apps/n8n/workflows/peladn-failover.json`](../../kubernetes/apps/n8n/workflows/peladn-failover.json).
> It merges the old `n8n-peladn-watchdog.json` + `n8n-peladn-failover.json` (both
> deleted); see "n8n workflow" below.

## How the GitOps retrieval works

`peladn-failover.sh` is the **single source of truth** in this repo. The n8n failover
workflow pulls the latest copy from the public repo at trigger time and runs it:

```bash
curl -fsSL https://raw.githubusercontent.com/DeepAchut/Homelab-ops/main/infrastructure/failover/peladn-failover.sh \
  -o /usr/local/bin/peladn-failover.sh 2>/dev/null || true   # latest from GitOps (best-effort)
chmod +x /usr/local/bin/peladn-failover.sh
bash /usr/local/bin/peladn-failover.sh                        # local copy = offline fallback
```

So a `git push` to `main` is the deploy — n8n always runs the current version, and the
on-host copy (installed by `evox2-readiness.sh`) is the offline fallback if GitHub is
unreachable during an outage.

## n8n workflow

One merged workflow, `peladn-failover` (`j4GQTKqjKS9EJGQD`), exported to
`../../kubernetes/apps/n8n/workflows/peladn-failover.json`. Two independent flows:

1. **Watchdog** — `Every 10 min` → GETs `https://192.168.4.150:8006/api2/json/version`
   (direct LAN Proxmox API, *not* an NPM-proxied hostname, so "Peladn down" ≠ "NPM down") →
   3-strike counter → **one Gmail alert** at ~30 min. Detection only; it does **not**
   auto-fail-over. The alert email carries the exact `curl` command to trigger failover.
2. **Failover** — webhook `POST /webhook/peladn-failover`, **Basic Auth** (cred
   "Failover n8n Webhook credentials", user `n8n`; password in Vaultwarden — keep an
   offline copy, Vaultwarden is on Peladn). On trigger: Gmail "STARTED" → SSH the
   `Evo-x2 proxmox credential` key → `curl` the latest `peladn-failover.sh` from GitHub
   raw + run it → Gmail "FINISHED" with script output. `continueOnFail` on the SSH node
   so you always get the result email.

Trigger it from the alert email:

```bash
curl -sS -u 'n8n:<PASSWORD>' -X POST https://n8n.dkghar.duckdns.org/webhook/peladn-failover
```

> ⚠️ **Notification channel must NOT live on Peladn.** Gotify runs in CT203 (home-ops) on
> Peladn — it's **down during a Peladn failure**. This workflow uses **Gmail** (external,
> Peladn-independent), which satisfies that. Keep Gotify for normal/backup-success notices
> that fire while Peladn is up.
>
> ⚠️ **No clickable link.** The webhook is POST + Basic Auth; a browser click is an
> unauthenticated GET (won't fire) and mail scanners *prefetch* links — dangerous for a
> destructive action. The email carries a `curl` one-liner instead. A genuine one-click
> trigger would need a `?token=` secret + a GET confirmation page that POSTs.

## Secrets & infra details (what's public vs private)

Nothing sensitive is committed. The split:

- **Committed (public — this is the showcase):** all *logic* — the script, the n8n workflows, the readiness flow, this README. Values are referenced as variables.
- **Private (gitignored `./.env`, or SOPS `./.env.enc.yaml`):** environment-specific values — `PBS_SERVER` (host IP), `PBS_FINGERPRINT` (PBS cert SHA-256), `PBS_TOKEN_SECRET` (the only real credential). See `.env.example`.

`peladn-failover.sh` needs **no** secrets at all (the PBS token lives in PVE's
`/etc/pve/priv/storage/pbs-local.pw` on Evo-X2). `evox2-readiness.sh` reads the `.env`
values **only** when first adding `pbs-local`:

```bash
# on Evo-X2, in this dir
cp .env.example .env && $EDITOR .env      # or: sops -d .env.enc.yaml > .env
./evox2-readiness.sh
rm -f .env
```

> The fingerprint is the SHA-256 of the PBS *public* TLS cert (a pinning value, like an SSH
> host-key fingerprint) — not a credential. It's kept in `.env` purely to keep infra details
> out of the public repo, not because it grants access.

## Current state (2026-09-10)

- ✅ `evox2-readiness.sh` applied to Evo-X2 — `pbs-local` active, stub dirs present,
  `peladn-failover.sh` installed at `/usr/local/bin/`.
- ✅ VM 201 (Talos CP) + CT 202/203 backed up to `pbs-local` **weekly** (latest visible
  `2026-09-07`). This is the RPO of Path A in the runbook.
- ✅ **etcd-snapshot layer** — `talos-backup.sh` (see `../peladn-host/backup-scripts/`)
  mirrors a `talosctl etcd snapshot` to `evox2:/mnt/backup-hdd/talos-etcd-snapshots/`
  every 12 h. Low-RPO input for Path B. (Was silently broken 2026-05 → 2026-09-10.)
- ✅ `peladn-failover` workflow **published/active** — watchdog + failover flows,
  webhook Basic Auth, alert email carries the trigger `curl`. Schedule set to 12 h.
- ✅ `talosctl` **v1.12.6 installed** on the Evo-X2 host (`/usr/local/bin/talosctl`) for Path B.
- ✅ Offline decrypted CP config at **`evox2:/root/dr/controlplane.yaml`** (mode 0400,
  `talosctl validate --mode metal` passes) — usable in Path B without Vaultwarden.
- ⬜ OPNsense reservation for the CP MAC `BC:24:11:C1:FB:D7 → 192.168.4.172` (so a
  restored VM 201 on Evo-X2 keeps the endpoint IP).
- ⬜ First real drill — see the runbook's "Drill" section. Planned for a weekend window.
- ⬜ Refresh `evox2:/root/dr/controlplane.yaml` whenever the machine config changes
  (`sops -d kubernetes/talos/controlplane.enc.yaml`).
- ⬜ Watchdog cadence is 10 min → ~30 min to first alert, and it alerts only once
  (no re-alert if it stays down). Tune if you want faster / repeated notice.
