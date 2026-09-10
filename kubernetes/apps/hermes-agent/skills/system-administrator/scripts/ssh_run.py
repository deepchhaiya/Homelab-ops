#!/usr/bin/env python3
"""
ssh_run.py — execute a READ-ONLY command on a homelab host via SSH.

Allowlist-enforced. Use this FIRST whenever the user asks for the state of a
host — output of kubectl, systemctl, docker, journalctl, file inspection, etc.
For commands that mutate state (restart, kill, delete, mkfs), use ssh_exec.py
which gates on user confirmation.

Reads (from env):
  HERMES_SSH_KEY          default: /opt/data/.ssh/id_ed25519 (copied + chowned by init container)
  HERMES_SSH_KNOWN_HOSTS  default: /opt/data/.ssh/known_hosts (override when running outside the pod)

Usage:
  ssh_run.py <host> "<command>"
  ssh_run.py peladn "kubectl -n n8n get pods"
  ssh_run.py evox2  "systemctl status ollama"
  ssh_run.py peladn "pct exec 202 -- docker logs jellyfin --tail=20"
  ssh_run.py peladn "pct exec 203 -- docker logs homeassistant --tail=20"
  ssh_run.py peladn "pct exec 202 -- docker exec immich-server cat /usr/src/app/.env"
  ssh_run.py evox2  "pct exec 200 -- proxmox-backup-manager datastore list"
  ssh_run.py peladn "curl -s http://192.168.4.141:30800/health"

Hosts (only the two Proxmox VE nodes — for HA/PBS/any LXC, use `pct exec` from
the Proxmox host they live on):
  peladn  -> root@192.168.4.150  (hosts CT202 media-ai-ops, CT203 home-ops/HA)
  evox2   -> root@192.168.4.84   (hosts CT200 PBS, CT405 observability, etc.)

Add --json for raw subprocess output as JSON.
"""
import argparse, ipaddress, json, os, shlex, subprocess, sys, urllib.parse

SSH_KEY = os.environ.get("HERMES_SSH_KEY", "/opt/data/.ssh/id_ed25519")
KNOWN_HOSTS = os.environ.get("HERMES_SSH_KNOWN_HOSTS", "/opt/data/.ssh/known_hosts")

HOSTS = {
    "peladn": "192.168.4.150",
    "evox2":  "192.168.4.84",
}
# HA (CT203 @ .13) and PBS (CT200 @ .27) intentionally NOT direct SSH targets —
# reach them via `ssh_run.py peladn "pct exec 203 -- ..."` and
# `ssh_run.py evox2  "pct exec 200 -- ..."` respectively. The Proxmox host
# already trusts our key; pct exec drops into the LXC; the recursive allowlist
# check still validates the inner command. `docker exec <ctr> <cmd>` nests the
# same way (and composes: `pct exec NN -- docker exec <ctr> <cmd>`) — the
# in-container command is re-validated against this same allowlist, and no
# `docker exec` option flags are accepted. One key, two hosts, all LXCs covered.

# Allowed binaries → list of allowed verbs (the FIRST non-flag token after the binary).
# Use ["*"] for binaries where any usage is read-only (ls, cat, df, etc.).
ALLOWED = {
    # K8s read-only
    "kubectl":  ["get", "describe", "logs", "explain", "top", "version",
                 "config", "api-resources", "api-versions", "cluster-info", "auth", "diff"],
    # Docker read-only
    "docker":   ["ps", "logs", "inspect", "version", "images", "stats",
                 "network", "volume", "info", "container", "system"],
    # Filesystem (any usage is read-only)
    "ls": ["*"], "cat": ["*"], "head": ["*"], "tail": ["*"], "stat": ["*"],
    "file": ["*"], "wc": ["*"], "du": ["*"], "df": ["*"], "find": ["*"],
    "tree": ["*"], "readlink": ["*"], "realpath": ["*"], "basename": ["*"],
    "dirname": ["*"], "md5sum": ["*"], "sha256sum": ["*"],
    # System / process info
    "ps": ["*"], "top": ["*"], "uptime": ["*"], "free": ["*"], "uname": ["*"],
    "id": ["*"], "whoami": ["*"], "date": ["*"], "hostname": ["*"],
    "printenv": ["*"], "env": ["*"], "mount": ["*"], "lsmod": ["*"],
    # Logs
    "journalctl": ["*"], "dmesg": ["*"], "last": ["*"],
    # Network read-only
    "ss": ["*"], "netstat": ["*"], "ip": ["*"], "ping": ["*"],
    "nslookup": ["*"], "dig": ["*"], "showmount": ["*"], "arp": ["*"],
    "traceroute": ["*"],
    # systemd read-only
    "systemctl": ["status", "is-active", "is-enabled", "is-failed",
                  "list-units", "list-unit-files", "list-jobs",
                  "list-timers", "list-dependencies", "show", "cat"],
    # Proxmox read-only
    "pct":      ["config", "list", "status", "exec"],
    "qm":       ["config", "list", "status"],
    "pveversion": ["*"],
    "pvesm":    ["status", "list", "path"],
    "pvesh":    ["get"],
    "vzdump":   [],  # explicitly empty — vzdump is mutating
    # Hardware / SMART / power
    "smartctl": ["*"], "lsblk": ["*"], "nvme": ["*"], "sensors": ["*"],
    "lsusb": ["*"], "lspci": ["*"], "lscpu": ["*"], "lsmem": ["*"],
    "hdparm": ["*"], "rocm-smi": ["*"], "nvidia-smi": ["*"], "amd-smi": ["*"],
    "fdisk": ["-l"],  # only -l (list)
    # HTTP — URL is RFC1918-checked in validate()
    "curl":     ["*"],
    "wget":     ["*"],
    # Misc
    "echo":     ["*"], "true": ["*"], "false": ["*"], "which": ["*"],
    "type": ["*"], "command": ["*"],
}

# Substrings that, if present anywhere in the command, hard-block.
DENIED_SUBSTRINGS = [
    " rm ", " rmdir ", " mv ", " cp ", " chmod ", " chown ", " chgrp ",
    " kill ", " killall ", " pkill ",
    " mkfs", " dd if=", " dd of=", " tee ", " nft ", " iptables ",
    " umount ",
    " > /", " >> /", " 2> /", " 2>> /",
    "; sudo", "&& sudo", "| sudo",
    " su ", " su -",
    "$(", "`",
    " ssh ", " scp ", " rsync ",
    " --force", " -rf ", " -fr ",
    " systemctl stop ", " systemctl restart ", " systemctl start ",
    " systemctl reload ", " systemctl disable ", " systemctl enable ",
    " systemctl mask ", " systemctl unmask ", " systemctl reset-failed",
    " docker stop", " docker rm", " docker restart", " docker kill",
    " docker run", " docker pull", " docker build",
    " docker create", " docker cp", " docker commit", " docker update",
    " docker system prune", " docker container rm", " docker container stop",
    " docker container kill", " docker container prune",
    " docker network create", " docker network rm", " docker network prune",
    " docker volume create", " docker volume rm", " docker volume prune",
    " docker image rm", " docker image prune",
    " kubectl delete", " kubectl create", " kubectl apply",
    " kubectl patch", " kubectl edit", " kubectl scale",
    " kubectl rollout", " kubectl replace", " kubectl annotate",
    " kubectl label", " kubectl cordon", " kubectl drain", " kubectl uncordon",
    " kubectl taint", " kubectl run",
    " pct stop", " pct destroy", " pct create", " pct set",
    " pct start", " pct reboot", " pct shutdown",
    " qm stop", " qm destroy", " qm create", " qm set",
    " qm start", " qm reboot", " qm shutdown",
    " apt ", " apt-get ", " dpkg ", " pip install", " pip3 install",
    " curl -X POST", " curl -X PUT", " curl -X DELETE", " curl -X PATCH",
    " curl --data", " curl -d ", " curl --upload-file",
    " wget --post-data", " wget -O ",
]

# Flags that take a value (so we skip BOTH the flag AND the next token when
# searching for the command verb).
FLAG_TAKES_VALUE = {
    "-n", "--namespace", "-c", "--container", "-o", "--output",
    "-l", "--label-selector", "--context", "--cluster", "--user",
    "-f", "--filename", "--field-selector", "-H", "--host",
    "--since", "--tail", "--limit", "--server", "--token",
    "--kubeconfig", "--request-timeout", "--all-namespaces=true",
    "--all-namespaces=false", "--for", "--selector", "--node-name",
    "--node",
    "--unit", "-u",  # journalctl
    "-N", "-A",  # ss -N takes value, but -A is boolean — risk of overconsuming; OK for our use
    "--type", "--state",
    "-d",  # smartctl -d sat
    "--repeat-count",
    "--api-token", "--api-key",
    "--user-agent", "-A",
}


def is_private_url(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname
        if not host:
            return False
        if host in ("localhost",):
            return True
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback
    except (ValueError, TypeError):
        return False


def tokenize(cmd):
    try:
        return shlex.split(cmd)
    except ValueError as e:
        print(f"  could not parse command: {e}", file=sys.stderr)
        sys.exit(2)


def find_verb(tokens, start=1):
    """Find the first non-flag token after `start`. Skip flags + their values."""
    i = start
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("-"):
            # Compound form --flag=value
            if "=" in t:
                i += 1
                continue
            if t in FLAG_TAKES_VALUE:
                i += 2
            else:
                i += 1
        else:
            return t, i
    return None, i


def is_docker_exec(tokens):
    """True for `docker exec ...` and the `docker container exec ...` alias."""
    return (
        (len(tokens) >= 2 and tokens[1] == "exec")
        or (len(tokens) >= 3 and tokens[1] == "container" and tokens[2] == "exec")
    )


def validate(cmd):
    """Return (allowed: bool, reason: str)."""
    if not cmd.strip():
        return False, "empty command"

    padded = " " + cmd + " "
    for substr in DENIED_SUBSTRINGS:
        if substr in padded:
            return False, f"contains denied substring {substr.strip()!r}"

    tokens = tokenize(cmd)
    if not tokens:
        return False, "empty after tokenize"

    binary = tokens[0]

    # curl/wget special-case: enforce URL is private + only GET methods
    if binary in ("curl", "wget"):
        for t in tokens:
            if t.startswith(("http://", "https://")):
                if not is_private_url(t):
                    return False, f"URL {t} is not RFC1918/loopback"
        if binary == "curl":
            for i, t in enumerate(tokens):
                if t in ("-X", "--request") and i + 1 < len(tokens):
                    if tokens[i + 1].upper() not in ("GET", "HEAD"):
                        return False, "non-GET method for curl"

    # pct exec NN -- <inner-cmd>: nested allowlist check
    if binary == "pct" and len(tokens) >= 2 and tokens[1] == "exec":
        try:
            idx = tokens.index("--")
            inner = " ".join(shlex.quote(t) for t in tokens[idx + 1:])
            ok, reason = validate(inner)
            if not ok:
                return False, f"pct exec inner cmd rejected: {reason}"
            return True, "ok"
        except ValueError:
            return False, "pct exec requires `--` separator and an inner command"

    # docker exec <container> <inner-cmd>: same nested allowlist model as
    # `pct exec`. NO option flags are accepted — privilege-escalation
    # (-u/--user/--privileged), tty (-i/-t) and detach (-d/--detach) flags,
    # including bundled short forms like -it / -itd / -uroot, are all rejected
    # because no read-only use case needs them and each widens the blast radius
    # (docker.sock is mounted in some containers => root on the Docker host).
    # The command run *inside* the container is validated recursively here.
    if binary == "docker" and is_docker_exec(tokens):
        rest = tokens[3:] if tokens[1] == "container" else tokens[2:]
        if not rest:
            return False, "docker exec requires <container> <command>"
        if rest[0].startswith("-"):
            return False, (
                f"docker exec flag {rest[0]!r} not allowed — no privilege "
                f"(-u/--user/--privileged), tty (-i/-t) or detach (-d/--detach) "
                f"flags, including bundled forms like -it/-itd/-uroot; use "
                f"`docker exec <container> <read-only cmd>`"
            )
        inner_tokens = rest[1:]  # rest[0] is the container name
        if not inner_tokens:
            return False, "docker exec requires a command after the container name"
        inner = " ".join(shlex.quote(t) for t in inner_tokens)
        ok, reason = validate(inner)
        if not ok:
            return False, f"docker exec inner cmd rejected: {reason}"
        return True, "ok"

    # Lookup binary
    if binary not in ALLOWED:
        return False, f"binary {binary!r} not in allowlist"

    allowed_verbs = ALLOWED[binary]
    if not allowed_verbs:
        return False, f"binary {binary!r} explicitly disabled"
    if "*" in allowed_verbs:
        return True, "ok"

    verb, _ = find_verb(tokens, 1)
    if verb is None:
        return False, f"{binary} requires a subcommand from {allowed_verbs}"
    if verb in allowed_verbs:
        return True, "ok"
    return False, f"verb {verb!r} not allowed for {binary} (allowed: {allowed_verbs})"


def run_ssh(host_alias, cmd, as_json):
    if host_alias not in HOSTS:
        print(f"  unknown host {host_alias!r}. known: {', '.join(HOSTS)}", file=sys.stderr)
        sys.exit(2)
    ip = HOSTS[host_alias]
    ssh_argv = [
        "ssh", "-i", SSH_KEY,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        f"root@{ip}", cmd,
    ]
    try:
        r = subprocess.run(ssh_argv, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        print("  ssh timeout after 60s", file=sys.stderr)
        sys.exit(124)
    if as_json:
        print(json.dumps({"host": host_alias, "cmd": cmd, "exit": r.returncode,
                          "stdout": r.stdout, "stderr": r.stderr}, indent=2))
    else:
        if r.stdout:
            sys.stdout.write(r.stdout)
        if r.stderr:
            sys.stderr.write(r.stderr)
    sys.exit(r.returncode)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("host", help=f"host alias: {', '.join(HOSTS)}")
    p.add_argument("command", help="read-only command to run on the host")
    p.add_argument("--json", action="store_true", help="output raw subprocess result as JSON")
    p.add_argument("--check", action="store_true", help="only validate, do not run")
    a = p.parse_args()

    ok, reason = validate(a.command)
    if not ok:
        print(f"  REJECTED: {reason}", file=sys.stderr)
        print(f"  command: {a.command}", file=sys.stderr)
        print(f"  for state-changing commands use ssh_exec.py (requires user confirmation)", file=sys.stderr)
        sys.exit(3)
    if a.check:
        print("  OK (allowlist match)")
        return
    run_ssh(a.host, a.command, a.json)


if __name__ == "__main__":
    main()
