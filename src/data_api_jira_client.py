"""Per-user Jira write client.

Instead of authenticating to Jira with one shared service credential (which makes
Jira attribute every write to a single account), this client routes writes through
the Apps Platform **Data API** reverse proxy at ``/api/data/jira/...``. The Data API
resolves the *logged-in user's own* Jira OAuth token (via Nango, keyed by the
``X-Request-Token`` that Trident injected into the inbound IAP request) and authors
the write as that user.

One instance is created per HTTP request and carries that request's ``X-Request-Token``.

See the platform docs: ``data-api``, ``data-api/architecture``, ``user-identity``.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time

import requests

from .jira_client import build_comment_adf, _format_jira_error


class JiraNotConnectedError(Exception):
    """The logged-in user has not connected their Jira account (no per-user OAuth
    token resolvable by the Data API), so a write cannot be attributed to them.

    The dashboard maps this to HTTP 409 with ``code: "jira_not_connected"`` so the
    frontend can prompt the user to connect instead of showing a generic error.
    """


class DataApiJiraConfigError(Exception):
    """Raised when the Data API itself is misconfigured/unreachable (not a per-user
    connection problem)."""


def data_api_base() -> str:
    """Base URL of the Data API for this environment.

    Production: ``https://dataapi.{URL_BASE}`` (URL_BASE is set on the Cloud Run
    service, e.g. ``experimental.apps.applied.dev``). The audience for the minted
    Google ID token is this exact base URL.
    """
    base = os.environ.get("URL_BASE", "").strip()
    if base:
        return f"https://dataapi.{base}"
    # Local dev has no Data API; the per-request writer should not construct this
    # client locally (see ClientHolder._writer fallback). Kept for completeness.
    return "http://localhost:8080"


# ── Google ID token minting (cached per-audience until shortly before expiry) ──
_token_lock = threading.Lock()
_token_cache: dict[str, tuple[str, float]] = {}  # audience -> (token, expiry_epoch)


def _decode_jwt_exp(token: str) -> float:
    """Best-effort read of a JWT's ``exp`` claim without verifying the signature."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # pad to a multiple of 4
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        return float(claims.get("exp", 0))
    except Exception:
        return 0.0


def mint_id_token(audience: str) -> str:
    """Mint (and cache) a Google ID token for ``audience`` using the service
    account identity (metadata server in Cloud Run)."""
    now = time.time()
    with _token_lock:
        cached = _token_cache.get(audience)
        if cached and cached[1] - 60 > now:
            return cached[0]
    # Import lazily so local/test paths that never call the Data API don't require GCP.
    import google.auth.transport.requests
    import google.oauth2.id_token

    token = google.oauth2.id_token.fetch_id_token(
        google.auth.transport.requests.Request(), audience
    )
    exp = _decode_jwt_exp(token) or (now + 3000)  # default ~50m if exp unreadable
    with _token_lock:
        _token_cache[audience] = (token, exp)
    return token


def _data_api_headers(request_token: str) -> dict:
    base = data_api_base()
    return {
        "Authorization": f"Bearer {mint_id_token(base)}",
        "X-Request-Token": request_token,
        "Accept": "application/json",
    }


def start_oauth(request_token: str, integration: str = "jira") -> str:
    """Begin a one-time per-user OAuth connect. Returns the Connect UI URL the
    browser should open in a popup. (Data API: ``POST /api/data/oauth/start``)."""
    base = data_api_base()
    resp = requests.post(f"{base}/api/data/oauth/start",
                         params={"integration": integration},
                         headers=_data_api_headers(request_token), timeout=30)
    resp.raise_for_status()
    return (resp.json() or {}).get("url", "")


def list_connections(request_token: str) -> list:
    """List the current user's connected integrations (Data API:
    ``GET /api/data/connections``)."""
    base = data_api_base()
    resp = requests.get(f"{base}/api/data/connections",
                        headers=_data_api_headers(request_token), timeout=30)
    resp.raise_for_status()
    data = resp.json() or {}
    if isinstance(data, list):
        return data
    return data.get("connections", [])


class DataApiJiraClient:
    """Per-request Jira *write* client that proxies through the Data API and acts
    as the logged-in IAP user. Mirrors the write surface of ``JiraClient``."""

    def __init__(self, request_token: str, *, base: str | None = None, verbose: bool = False):
        if not request_token:
            raise JiraNotConnectedError("Missing request token; cannot act as the user.")
        self._token = request_token
        self._base = (base or data_api_base()).rstrip("/")
        self._verbose = verbose

    # ── internals ────────────────────────────────────────────────────────────
    def _url(self, path: str) -> str:
        return f"{self._base}/api/data/jira/rest/api/3/{path.lstrip('/')}"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {mint_id_token(self._base)}",
            "X-Request-Token": self._token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _log(self, msg: str):
        if self._verbose:
            print(f"[dataapi-jira] {msg}")

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = self._url(path)
        self._log(f"{method} {url}")
        try:
            resp = requests.request(method, url, headers=self._headers(),
                                    json=payload if payload is not None else None,
                                    timeout=30)
        except Exception as exc:  # network / token-mint failure
            raise DataApiJiraConfigError(f"Data API request failed: {exc}") from exc
        # 401 from the Data API == no resolvable per-user credential == not connected.
        # NOTE: confirm exact not-connected status/body with #eng-apps-platform-v2;
        # widen this mapping if the Data API signals it differently.
        if resp.status_code == 401:
            raise JiraNotConnectedError(
                "Your Jira account isn't connected. Click “Connect Jira” to authorize "
                "the dashboard to comment and edit as you.")
        if resp.status_code == 403:
            raise JiraNotConnectedError(
                "Jira permission denied. Reconnect your Jira account (“Connect Jira”) "
                "or check your Jira permissions.")
        if resp.status_code == 400:
            raise ValueError(_format_jira_error(resp))
        if resp.status_code == 404:
            raise ValueError(f"Not found: {path}")
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    # ── identity ─────────────────────────────────────────────────────────────
    def verify_auth(self) -> dict:
        return self._request("GET", "myself")

    # ── comments ─────────────────────────────────────────────────────────────
    def add_comment(self, issue_key: str, text: str = "", segments: list[dict] | None = None) -> dict:
        return self._request("POST", f"issue/{issue_key}/comment",
                             {"body": build_comment_adf(text, segments)})

    def update_comment(self, issue_key: str, comment_id: str, text: str = "", segments: list[dict] | None = None) -> dict:
        return self._request("PUT", f"issue/{issue_key}/comment/{comment_id}",
                             {"body": build_comment_adf(text, segments)})

    def delete_comment(self, issue_key: str, comment_id: str) -> None:
        self._request("DELETE", f"issue/{issue_key}/comment/{comment_id}")

    # ── transitions & fields ─────────────────────────────────────────────────
    def do_transition(self, issue_key: str, transition_id: str, fields: dict | None = None,
                      comment: str | dict | None = None) -> None:
        payload: dict = {"transition": {"id": transition_id}}
        fields = dict(fields) if fields else {}
        comment = comment or fields.pop("comment", None)
        if fields:
            payload["fields"] = fields
        if comment:
            body = comment if isinstance(comment, dict) else build_comment_adf(text=comment)
            payload["update"] = {"comment": [{"add": {"body": body}}]}
        self._request("POST", f"issue/{issue_key}/transitions", payload)

    def update_priority(self, issue_key: str, priority_name: str) -> None:
        self._request("PUT", f"issue/{issue_key}", {"fields": {"priority": {"name": priority_name}}})

    def update_assignee(self, issue_key: str, account_id: str | None) -> None:
        value = {"accountId": account_id} if account_id else None
        self._request("PUT", f"issue/{issue_key}", {"fields": {"assignee": value}})

    def update_duedate(self, issue_key: str, date_str: str | None) -> None:
        self._request("PUT", f"issue/{issue_key}", {"fields": {"duedate": date_str or None}})
