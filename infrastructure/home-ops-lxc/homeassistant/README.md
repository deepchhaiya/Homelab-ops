# Home Assistant config (CT 203 · home-ops LXC)

Config-as-code snapshot of the Home Assistant instance running in the home-ops
LXC (`/var/lib/homeassistant/config` on the host, `/config` in the container).
**Nothing here is applied automatically** — HA owns the live files. These are
committed so the automations and the Reolink blueprint are reviewable and
restorable, because they were previously untracked and existed only on the host.

| File | What it is |
|---|---|
| `configuration.yaml` | Main config: Envisalink alarm, Reolink `shell_command` helpers (GIF/MP4 via ffmpeg, retention cleanups), `allowlist_external_dirs`, media dirs |
| `automations.yaml` | **20 automations** as currently running (UI-managed file); 3 are instances of the Reolink blueprint |
| `blueprints/automation/reolink_motion_notify_gif.yaml` | The custom Reolink motion → snapshot/GIF/MP4 notification blueprint (Phase 13). The critical piece: the three camera automations are useless without it |
| `aarlo.yaml` | Arlo (`aarlo`) tuning — timeouts, reconnect intervals, media retry. No credentials (those live in `.storage` via the config flow) |
| `scenes.yaml` | Empty (no scenes defined) |
| `secrets.yaml.example` | Keys the redacted `configuration.yaml` expects |

## ⚠️ Alarm credentials are redacted here — and inline in the live config

The committed `configuration.yaml` references `!secret envisalink_user`,
`!secret envisalink_password` and `!secret envisalink_code`. **The live file still
has these inline as plaintext**, including the alarm disarm code. That is why this
file could not be committed verbatim to a public repo.

To fix it on the host:

```bash
# in CT 203, /var/lib/homeassistant/config/secrets.yaml
envisalink_user: "…"
envisalink_password: "…"
envisalink_code: "…"
```

then replace the three inline values in `configuration.yaml` with the same
`!secret` references and restart HA. Until that is done, this repo's copy and the
live copy differ by design.

## Deliberately not committed

| Path | Why |
|---|---|
| `.storage/` | OAuth tokens, long-lived access tokens, credentials, entity/device registry. **Never commit.** |
| `secrets.yaml` | Secrets by definition |
| `home-assistant_v2.db*` | ~443 MB recorder database |
| `custom_components/` | HACS-managed third-party integrations; reinstall via HACS |
| `blueprints/automation/SgtBatten/`, `blueprints/{script,template}/homeassistant/` | Third-party and stock blueprints. The SgtBatten Frigate blueprint is referenced by **zero** automations (Frigate was removed in Phase 13); stock blueprints ship with HA |
| `backups/`, `themes/`, logs, `*.bak*` | Regenerated, or large, or machine-local |

Note `configuration.yaml` does `frontend: themes: !include_dir_merge_named themes`,
so a restored config needs a `themes/` directory to exist (it may be empty).

## Restore outline

1. Recreate the container per `infrastructure/home-ops-lxc/docker-compose.yml`.
2. Drop these files into `/var/lib/homeassistant/config/`, create `themes/`, and
   write a real `secrets.yaml` from the example.
3. Reinstall HACS integrations, then re-link Arlo/Reolink via their config flows
   (`.storage` is not in this repo, so device auth must be redone).
4. Start HA and confirm the three Reolink automations resolve the blueprint.
