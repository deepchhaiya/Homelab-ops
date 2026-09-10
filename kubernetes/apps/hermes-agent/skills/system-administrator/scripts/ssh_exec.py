#!/usr/bin/env python3
"""
ssh_exec.py — execute a state-changing command on a homelab host.

REQUIRES EXPLICIT USER CONFIRMATION in the same conversation turn before
executing. The model must:
  1. First call with --dry-run to show the user what command would run.
  2. Only call without --dry-run AFTER the user types explicit approval
     ("yes", "go", "approved", "do it") in their immediately-following message.
  3. Pass --confirmation-quote with a short excerpt of the user's approval
     so this tool can log the audit trail.

Reads (from env):
  HERMES_SSH_KEY          default: /opt/data/.ssh/id_ed25519
  HERMES_SSH_KNOWN_HOSTS  default: /tmp/hermes-known-hosts (override when running outside the pod)
  HERMES_AUDIT_LOG        default: /opt/data/logs/ssh-exec-audit.log (override when running outside the pod)
  GOTIFY_URL / GOTIFY_TOKEN / LOKI_URL   best-effort audit fan-out

Usage (dry-run, always start here):
  ssh_exec.py peladn "systemctl restart nfs-server" --dry-run

After user approves:
  ssh_exec.py peladn "systemctl restart nfs-server" --confirmation-quote "yes do it"

Hosts (Proxmox VE nodes only — LXCs reached via `pct exec`):
  peladn  -> root@192.168.4.150
  evox2   -> root@192.168.4.84

Every executed command is appended to $HERMES_AUDIT_LOG (default
/opt/data/logs/ssh-exec-audit.log) with:
  timestamp, host, command, user-confirmation-quote, exit-code.
"""
from __future__ import annotations  # allow `str | None` hints on Python < 3.10 (e.g. host system python)
import argparse, json, os, subprocess, sys, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

SSH_KEY = os.environ.get("HERMES_SSH_KEY", "/opt/data/.ssh/id_ed25519")
KNOWN_HOSTS = os.environ.get("HERMES_SSH_KNOWN_HOSTS", "/tmp/hermes-known-hosts")
AUDIT_LOG = Path(os.environ.get("HERMES_AUDIT_LOG", "/opt/data/logs/ssh-exec-audit.log"))

# Gotify push notification on every actual execution (best-effort).
GOTIFY_URL = os.environ.get("GOTIFY_URL", "").rstrip("/")
GOTIFY_TOKEN = os.environ.get("GOTIFY_TOKEN", "").strip()

# Loki push for every audit entry (best-effort). Ships the same JSON we
# write to the local audit file, but with proper stream labels for LogQL.
# Bypasses the Alloy stdout pipeline so Hermes doesn't have to surface the
# audit entry in the chat transcript.
LOKI_URL = os.environ.get("LOKI_URL", "http://192.168.4.66:3100").rstrip("/")
HOSTNAME = os.environ.get("HOSTNAME", "hermes-pod")

HOSTS = {
    "peladn": "192.168.4.150",
    "evox2":  "192.168.4.84",
}
# HA (CT203) and PBS (CT200) are reached via the Proxmox host they live on:
#   ssh_exec.py peladn "pct exec 203 -- <state-changing-cmd-on-HA>"
#   ssh_exec.py evox2  "pct exec 200 -- <state-changing-cmd-on-PBS>"
# Two-step confirmation pattern still required; same NEVER_RUN hard blocks
# apply since the wrapping pct exec command is what's audited.

# Hard blocks: things we never run, even with confirmation. Extending
# this list is cheap and one-way safe — only add patterns that have no
# legitimate use case for an automated agent.
NEVER_RUN = [
    # destructive filesystem ops
    "rm -rf /",
    "rm -rf /etc",
    "rm -rf /var",
    "rm -rf /opt",
    "rm -rf /usr",
    "rm -rf /home",
    "rm -rf /mnt",
    "rm -rf /root",
    "find / -delete",
    "find /etc -delete",
    "shred",
    # block-device destruction
    "mkfs",
    "wipefs",
    "dd if=/dev/zero of=/dev/sd",
    "dd if=/dev/zero of=/dev/nvme",
    "dd if=/dev/zero of=/dev/vd",
    "dd if=/dev/random of=/dev/sd",
    "dd if=/dev/random of=/dev/nvme",
    "dd if=/dev/urandom of=/dev/sd",
    "dd if=/dev/urandom of=/dev/nvme",
    # LVM / cryptsetup — destroys volumes
    "lvremove",
    "vgremove",
    "pvremove",
    "cryptsetup luksFormat",
    "cryptsetup erase",
    # swap
    "mkswap",
    "swapoff -a",
    # immutability — could lock the user out
    "chattr +i /etc",
    "chattr +i /boot",
    "chattr +i /root",
    "chattr +a /etc",
    # firewall reset (would lock out of the host)
    "iptables -F",
    "iptables -X",
    "iptables --flush",
    "ip6tables -F",
    "ip6tables -X",
    "nft flush ruleset",
    "nft delete table",
    "ufw reset",
    "ufw disable",
    "firewall-cmd --reload",
    # cron destruction
    "crontab -r",
    "rm /etc/crontab",
    "rm /var/spool/cron",
    # loopback / mount destruction
    "losetup -d /dev/loop",
    "umount -l /",
    "umount -l /opt",
    "umount -l /mnt",
    # power off / reboot
    ":(){:|:&};:",  # fork bomb
    "shutdown -h now",
    "shutdown -P now",
    "shutdown -r now",
    "poweroff",
    "halt",
    "reboot",
    "init 0",
    "init 6",
    "systemctl poweroff",
    "systemctl reboot",
    "systemctl halt",
    "systemctl emergency",
    "systemctl rescue",
    # K8s mass destruction
    "kubectl delete namespace hindsight",
    "kubectl delete namespace flux-system",
    "kubectl delete namespace kube-system",
    "kubectl delete namespace n8n",
    "kubectl delete namespace miniflux",
    "kubectl delete namespace hermes-agent",
    "kubectl delete pvc",
    "kubectl delete pv",
    "kubectl delete --all",
    # Proxmox destruction
    "pct destroy",
    "qm destroy",
    # SSH key trust destruction
    "rm ~/.ssh",
    "rm -rf ~/.ssh",
    "rm /root/.ssh",
    "truncate -s 0 /etc",
    "truncate -s 0 /root/.ssh",
    # passwd / shadow tampering
    "rm /etc/passwd",
    "rm /etc/shadow",
    "passwd -d root",
    "userdel root",
]


def is_never_run(cmd: str) -> str | None:
    """Return the matched forbidden pattern, or None."""
    low = cmd.lower().strip()
    for pat in NEVER_RUN:
        if pat.lower() in low:
            return pat
    return None


def audit(host_alias: str, cmd: str, conf_quote: str, exit_code: int, dry: bool):
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "host": host_alias,
        "cmd": cmd,
        "confirmation": conf_quote,
        "exit_code": exit_code,
        "dry_run": dry,
    }
    with AUDIT_LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    loki_push(rec)


def loki_push(rec: dict):
    """Push an audit record to Loki. Best-effort — never blocks the actual
    SSH execution if Loki is down. The Alloy DaemonSet already ships our
    container stdout; this pushes the file-based audit log separately with
    structured labels so LogQL queries are clean."""
    if not LOKI_URL:
        return
    # Loki wants nanosecond-precision Unix timestamps as strings.
    ts_ns = str(int(datetime.now(timezone.utc).timestamp() * 1e9))
    payload = {
        "streams": [{
            "stream": {
                "job":         "hermes-ssh-exec",
                "namespace":   "hermes-agent",
                "pod":         HOSTNAME,
                "host":        rec["host"],
                "exit_status": "ok" if rec["exit_code"] == 0 else "fail",
                "dry_run":     str(rec["dry_run"]).lower(),
            },
            "values": [[ts_ns, json.dumps(rec)]],
        }]
    }
    req = urllib.request.Request(
        f"{LOKI_URL}/loki/api/v1/push",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            if r.status >= 400:
                print(f"  warn: loki push returned HTTP {r.status}", file=sys.stderr)
    except Exception as e:
        print(f"  warn: loki push failed (continuing): {e}", file=sys.stderr)


def gotify_notify(host_alias: str, cmd: str, conf_quote: str, exit_code: int):
    """Push a notification to the user's Gotify instance. Best-effort —
    a failure here NEVER blocks the actual SSH execution; we just log to
    stderr so the user knows visibility was lost for this call."""
    if not GOTIFY_URL or not GOTIFY_TOKEN:
        # Not configured — silent. User can wire GOTIFY_URL/GOTIFY_TOKEN
        # via the hermes-credentials Secret if they want real-time alerts.
        return
    status_word = "OK" if exit_code == 0 else f"FAIL exit={exit_code}"
    title = f"Hermes ssh_exec on {host_alias} — {status_word}"
    # Truncate long commands so the push body stays readable
    short_cmd = cmd if len(cmd) <= 400 else cmd[:397] + "..."
    short_quote = conf_quote if len(conf_quote) <= 200 else conf_quote[:197] + "..."
    body = (
        f"host: {host_alias}\n"
        f"cmd:  {short_cmd}\n"
        f"approval: {short_quote!r}\n"
        f"exit: {exit_code}\n"
        f"ts:   {datetime.now(timezone.utc).isoformat()}"
    )
    payload = urllib.parse.urlencode({
        "title": title,
        "message": body,
        # priority 5 = default; 8+ would override DnD on the user's phone.
        # Failed executions get bumped so the user notices.
        "priority": "8" if exit_code != 0 else "5",
    }).encode()
    url = f"{GOTIFY_URL}/message?token={urllib.parse.quote(GOTIFY_TOKEN)}"
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            if r.status >= 400:
                print(f"  warn: gotify returned HTTP {r.status}", file=sys.stderr)
    except Exception as e:
        # Never block on push failure; just surface it.
        print(f"  warn: gotify notify failed (continuing): {e}", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("host", help=f"host alias: {', '.join(HOSTS)}")
    p.add_argument("command", help="command to execute (state-changing OK)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would run; do not execute. ALWAYS use this first.")
    p.add_argument("--confirmation-quote",
                   help="Short excerpt of the user's approval message. Required for actual execution.")
    p.add_argument("--json", action="store_true", help="output as JSON")
    a = p.parse_args()

    if a.host not in HOSTS:
        print(f"  unknown host {a.host!r}", file=sys.stderr)
        sys.exit(2)
    ip = HOSTS[a.host]

    forbidden = is_never_run(a.command)
    if forbidden:
        print(f"  HARD-BLOCKED: command matches NEVER_RUN pattern {forbidden!r}", file=sys.stderr)
        print(f"  this is unrecoverable; ssh_exec will not run it under any circumstance.", file=sys.stderr)
        sys.exit(4)

    if a.dry_run:
        print(f"DRY-RUN — would execute on root@{ip} ({a.host}):")
        print(f"  {a.command}")
        print()
        print(f"After user explicit approval, re-run WITHOUT --dry-run and WITH --confirmation-quote 'their words'")
        audit(a.host, a.command, "(dry-run)", 0, dry=True)
        return

    if not a.confirmation_quote or len(a.confirmation_quote.strip()) < 2:
        print("  REFUSED: missing --confirmation-quote.", file=sys.stderr)
        print("  ssh_exec requires you to quote the user's explicit approval message.", file=sys.stderr)
        print("  Call --dry-run first; ask the user to confirm; pass their words as --confirmation-quote.", file=sys.stderr)
        sys.exit(3)

    ssh_argv = [
        "ssh", "-i", SSH_KEY,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes",
        f"root@{ip}", a.command,
    ]
    try:
        r = subprocess.run(ssh_argv, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print(f"  ssh timeout after 120s", file=sys.stderr)
        audit(a.host, a.command, a.confirmation_quote, 124, dry=False)
        gotify_notify(a.host, a.command, a.confirmation_quote, 124)
        sys.exit(124)

    audit(a.host, a.command, a.confirmation_quote, r.returncode, dry=False)
    gotify_notify(a.host, a.command, a.confirmation_quote, r.returncode)

    if a.json:
        print(json.dumps({"host": a.host, "cmd": a.command, "exit": r.returncode,
                          "stdout": r.stdout, "stderr": r.stderr}, indent=2))
    else:
        if r.stdout:
            sys.stdout.write(r.stdout)
        if r.stderr:
            sys.stderr.write(r.stderr)
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
