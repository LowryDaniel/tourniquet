# Deployment

Tourniquet runs on Fly.io. Two apps share one Postgres cluster.

## Estimated monthly cost

| Resource | Cost |
|---|---|
| `tourniquet-web` (shared-cpu-1x, 256MB) | ~£5 |
| `tourniquet-worker` (shared-cpu-1x, 256MB) | ~£2 |
| Fly Postgres (free tier 3GB) | £0 |
| Domain (`tourniquet.ai`) | ~£1 amortised |
| Resend (3K emails/mo free tier) | £0 |
| Sentry (5K events/mo free tier) | £0 |
| **Total** | **~£8–10/mo** |

## First deploy

```bash
# Install Fly CLI
curl -L https://fly.io/install.sh | sh

# Authenticate
flyctl auth login

# Create apps
flyctl apps create tourniquet-web
flyctl apps create tourniquet-worker

# Create Postgres cluster (shared between both apps)
flyctl postgres create --name tourniquet-db --vm-size shared-cpu-1x --volume-size 3

# Attach Postgres to web app
flyctl postgres attach tourniquet-db --app tourniquet-web

# Set secrets (web app)
flyctl secrets set \
    FERNET_KEY="..." \
    SECRET_KEY="..." \
    RESEND_API_KEY="re_..." \
    SENTRY_DSN="..." \
    APP_BASE_URL="https://tourniquet.ai" \
    --app tourniquet-web

# Set secrets (worker — shares FERNET_KEY and DATABASE_URL)
flyctl secrets set \
    FERNET_KEY="..." \
    RESEND_API_KEY="re_..." \
    --app tourniquet-worker

# Deploy web app
flyctl deploy --app tourniquet-web --config fly.toml

# Deploy worker
flyctl deploy --app tourniquet-worker --config fly.worker.toml

# Run migrations (one-off)
flyctl ssh console --app tourniquet-web --command "alembic upgrade head"
```

## Domain setup

```bash
# Point tourniquet.ai at the web app
flyctl certs create tourniquet.ai --app tourniquet-web
# Follow DNS instructions from Fly
```

## Secrets reference

| Secret | Description |
|---|---|
| `DATABASE_URL` | Injected automatically by `flyctl postgres attach` |
| `FERNET_KEY` | Symmetric encryption key for Anthropic keys at rest |
| `SECRET_KEY` | Signing key for magic-link tokens |
| `RESEND_API_KEY` | Resend transactional email API key |
| `RESEND_FROM_EMAIL` | Sender address (default: `alerts@tourniquet.ai`) |
| `SENTRY_DSN` | Sentry project DSN (leave empty to disable) |
| `APP_BASE_URL` | Public URL used in magic-link emails |

## Continuous deployment

GitHub Actions workflow (`.github/workflows/deploy.yml`) deploys to Fly on every push to `main` after CI passes. Requires `FLY_API_TOKEN` in GitHub repo secrets.

## Monitoring

- **Uptime:** Better Stack free tier (10 monitors) — ping `/health`
- **Errors:** Sentry free tier
- **Logs:** `flyctl logs --app tourniquet-web`
- **Metrics:** `flyctl dashboard` (CPU, memory, request rate)

## Rollback

```bash
# List recent releases
flyctl releases list --app tourniquet-web

# Roll back to previous version
flyctl deploy --image <previous-image-digest> --app tourniquet-web
```
