# Smoke tests (manual, pre-deploy)

Scripts here are NOT run by CI. They hit external services with live
credentials and are meant to be run locally before a production
deploy.

## Credentials

Credentials live in `test-local/smoke/.env.smoke` (gitignored). The
smoke scripts auto-source it if present; otherwise they read from the
current shell environment.

First-time setup:

```bash
cp test-local/smoke/.env.smoke.example test-local/smoke/.env.smoke
$EDITOR test-local/smoke/.env.smoke
# fill in TELEGRAM_BOT_TOKEN and TELEGRAM_TEST_SUPERGROUP_ID
```

After that, run any smoke script directly — no `export` needed:

```bash
bash test-local/smoke/test_telegram_engagement.sh
```
