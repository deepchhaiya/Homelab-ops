# Nextcloud background jobs (CT202)

Nextcloud runs in `cron` background-job mode. The timer runs `cron.php` inside the
`nextcloud` container every 5 minutes. Until 2026-10-11 the mode was `ajax`, so jobs only
ran while someone had the web UI open, and they had stalled for 2 days.

Install on CT202:

```bash
cp nextcloud-cron.service nextcloud-cron.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now nextcloud-cron.timer
docker exec -u www-data nextcloud php occ background:cron
```

Heavy daily jobs run in the maintenance window: `maintenance_window_start` is 8 (08:00 UTC,
3 AM CDT).

## Ollama on Vulkan (CT202)

`ollama-override.conf` is the live `/etc/systemd/system/ollama.service.d/override.conf`.
Since 2026-10-27 Ollama runs on the 780M through Vulkan (Mesa RADV), not ROCm. Under ROCm,
KFD compute-queue eviction on this gfx1103 APU hangs the MES firmware
(`MES failed to respond to msg=REMOVE_QUEUE ... Failed to evict process queues`) and the
kernel resets the GPU. About 1 in 4 fresh model loads did this (ROCm/TheRock#9285), and each
reset also killed webtop's X server, which draws on the same GPU. Vulkan doesn't use KFD. A
newer MES firmware alone (pve-firmware 3.18-7) is reported upstream as still affected.

Install on CT202:

```bash
apt-get update && apt-get install -y --no-install-recommends mesa-vulkan-drivers
cp ollama-override.conf /etc/systemd/system/ollama.service.d/override.conf
systemctl daemon-reload && systemctl restart ollama
journalctl -u ollama -b | grep 'inference compute'   # expect library=Vulkan ... RADV PHOENIX
```

qwen3:4b-instruct, ~700-token prompt: ROCm 504 tok/s prompt / 22 tok/s generation; Vulkan
~340 / ~28. Validated with 20 fresh-load cycles: no GPU resets, no runner crashes.

Rollback to ROCm: drop the `OLLAMA_VULKAN` and `ROCR_VISIBLE_DEVICES` lines and restart.
If GPU resets ever come back, the upstream workaround is the kernel parameter
`amdgpu.cwsr_enable=0` on Peladn (needs a reboot).
