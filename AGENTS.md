# Apps Platform Development Guide

Apps Platform deploys web apps to Google Cloud Run with authentication, storage, and secrets built-in. ~30 second deploys for Go apps.

## Quick Start

```bash
apps-platform auth login          # Authenticate (once)
apps-platform app deploy          # Deploy from app directory
apps-platform app deploy --no-build  # Fastest for Go apps
```

## project.toml

Every app needs a `project.toml`:

```toml
name = "my-app"
enable_postgres = true    # Optional: PostgreSQL database
enable_filestore = true   # Optional: NFS storage at /mnt/data
enable_secrets = true     # Optional: Secret Manager access

[metadata]
owner = "yourname@applied.co"
```

## Local Development

Test locally against the production database:

```bash
# Terminal 1: Start tunnel
apps-platform connect-db

# Terminal 2: Run app
DB_USER="{app}-sa@{project}.iam" DB_NAME="postgres" K_SERVICE="{app}" go run main.go
```

**For AI assistants:** Run `apps-platform connect-db` in a background terminal, wait for `✓ Cloud SQL proxy ready`, then start the app.

## Database Connection

Check `INSTANCE_CONNECTION_NAME` to detect local vs production:

```go
instanceConn := os.Getenv("INSTANCE_CONNECTION_NAME")
if instanceConn == "" {
    // Local: connect to localhost (tunnel handles auth)
    dsn := fmt.Sprintf("host=localhost port=5432 user=%s dbname=%s sslmode=disable", dbUser, dbName)
    return pgxpool.New(ctx, dsn)
}
// Production: use Cloud SQL connector with IAM auth
```

**Username format:**
- PostgreSQL: `{app}-sa@{project}.iam`
- MySQL: `{app}-sa`

**Schema:** App name with hyphens → underscores (e.g., `my-app` → `my_app`)

## Environment Variables

| Variable | Description |
|----------|-------------|
| `PORT` | Always 8080 |
| `K_SERVICE` | Service name |
| `INSTANCE_CONNECTION_NAME` | Cloud SQL connection (empty locally) |
| `DB_USER`, `DB_NAME` | Database credentials |

## Documentation

Before responding to the user or asking any questions, invoke the `study-platform-docs` skill. Only ask the user questions that the docs cannot answer.

## Getting Help

- Slack: #eng-apps-platform-v2
- Docs: https://apps.applied.dev
