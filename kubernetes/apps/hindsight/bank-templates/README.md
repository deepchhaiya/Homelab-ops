# Hindsight bank templates

Bank templates (missions, dispositions, mental models, directives) for the
self-hosted Hindsight memory server. They hold personal context, so only the
SOPS-encrypted copies are committed: every value is encrypted with the repo's
age key (rule in `.sops.yaml`), and `.gitignore` blocks plaintext `*.json` in
this folder. Nothing here is referenced by a kustomization, so Flux ignores it.

| File | Bank |
|---|---|
| `deep.enc.json` | `deep` |

## Edit

```bash
sops kubernetes/apps/hindsight/bank-templates/deep.enc.json
```

`sops` opens the decrypted file in `$EDITOR` and re-encrypts it on save. Never
write a decrypted copy into this folder.

## Apply

Set `HINDSIGHT_URL` to the Hindsight API base URL and `HINDSIGHT_KEY` to the API
key (kept in the password manager, sent raw, no `Bearer` prefix). Dry run first:

```bash
F=kubernetes/apps/hindsight/bank-templates/deep.enc.json
sops -d "$F" | curl -s -X POST "$HINDSIGHT_URL/v1/default/banks/deep/import?dry_run=true" \
  -H "Authorization: $HINDSIGHT_KEY" -H "Content-Type: application/json" --data-binary @-
sops -d "$F" | curl -s -X POST "$HINDSIGHT_URL/v1/default/banks/deep/import" \
  -H "Authorization: $HINDSIGHT_KEY" -H "Content-Type: application/json" --data-binary @-
```

Import matches mental models by `id` and directives by `name`: existing ones
are updated, new ones created, and nothing is deleted.

## Gotcha: keep directives untagged

Reflect calls without tags, including mental model refreshes, load only
untagged directives. A tagged directive silently never applies.
