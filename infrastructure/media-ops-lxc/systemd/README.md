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
