import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# In Cloud Run, secrets are stored in Secret Manager (not auto-mounted as env vars).
# Read them at startup using the service's own service account.
def _load_secrets_from_gcp():
    project_id = os.environ.get("PROJECT_ID")
    if not project_id:
        return  # local dev — rely on .env
    if os.environ.get("JIRA_BASE_URL"):
        return  # already set (e.g. from .env during local testing against prod)
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        service = os.environ.get("K_SERVICE", "mining-dashboard")
        for env_var, secret_suffix in [
            ("JIRA_BASE_URL",   "jira-base-url"),
            ("JIRA_EMAIL",      "jira-email"),
            ("JIRA_API_TOKEN",  "jira-api-token"),
        ]:
            name = f"projects/{project_id}/secrets/{service}-{secret_suffix}/versions/latest"
            resp = client.access_secret_version(request={"name": name})
            os.environ[env_var] = resp.payload.data.decode("utf-8")
    except Exception as e:
        print(f"[warning] could not load secrets from Secret Manager: {e}")

_load_secrets_from_gcp()

from src.config import ConfigStore
from src.dashboard import create_app

_data_dir = Path("/mnt/data") if Path("/mnt/data").is_dir() else Path(".")
store = ConfigStore(path=_data_dir / "mining_dashboard.config.json")
app = create_app(store)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("ENV") == "dev", use_reloader=False)
