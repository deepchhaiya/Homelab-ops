# n8n workflow exports

Reference copies of the n8n workflows that run this homelab's feed and email
pipelines. **These are not deployed by Flux** — n8n owns the live definitions in
its Postgres database. They are committed so changes are reviewable in git and
so a workflow can be rebuilt after a restore.

| File | Workflow | Schedule |
|---|---|---|
| `miniflux-curator.json` | Scores Miniflux entries for the morning brief | every 3 h, 06:00–21:00 CT |
| `miniflux-curator-brief-v2.json` | Builds the sectioned brief, uploads to Nextcloud | 07:00 and 17:00 CT |
| `miniflux-deals-router.json` | Routes deal feeds to Gotify, drains the firehose | every 2 h, 07:00–21:00 CT |
| `miniflux-curator-digest.json` | Weekly synthesis of starred items | Sundays 08:00 CT |
| `Email Summary with Ollama.json` | Triages both Gmail inboxes, pushes a digest | daily 07:30 CT |
| `peladn-failover.json` | Manual failover runbook trigger | webhook |

## What is and isn't in these files

* **Credentials are references only** — `{id, name}` pairs. No tokens, keys or
  passwords are exported. Importing these requires recreating the credentials.
* **The reader profile in `miniflux-curator-digest.json` is redacted.** The live
  prompt carries a personal profile (role, target role, goals); the committed copy
  has a placeholder. Edit it in n8n, not here.
* Internal endpoints (`192.168.4.84`, `192.168.4.12` Ollama; the Miniflux and
  Nextcloud hosts) appear as-is — they are LAN addresses, already documented
  elsewhere in this repo.
* Newer exports omit `meta.instanceId`; older ones still contain it.

## Re-exporting

These were produced from the n8n Postgres, keeping only the importable fields:

```sql
select json_build_object('id', id, 'name', name, 'active', active,
       'versionId', "versionId", 'nodes', nodes, 'connections', connections,
       'settings', settings)
from workflow_entity where name = '<workflow name>' and "isArchived" = false;
```

Remember n8n keeps a **draft** and an **active** version: `nodes` here is the
draft. Publish in n8n after importing, or the schedule keeps running the old one.
