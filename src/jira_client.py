from __future__ import annotations

import requests
from requests.auth import HTTPBasicAuth


class JiraConfigError(Exception):
    """Raised when Jira credentials are missing or invalid."""


def _text_to_adf(text: str) -> dict:
    """Wrap plain text as an Atlassian Document Format doc (required on v3)."""
    return {"version": 1, "type": "doc",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def _format_jira_error(resp: requests.Response) -> str:
    """Turn a Jira error response into a human-readable message.

    Jira returns {"errorMessages": [...], "errors": {"<fieldId>": "<msg>"}}.
    Surface both so the actual failing field/validator is visible instead of
    a raw JSON dump.
    """
    try:
        data = resp.json()
    except ValueError:
        return f"Jira rejected request: {resp.text[:300]}"
    parts = list(data.get("errorMessages") or [])
    for field_id, msg in (data.get("errors") or {}).items():
        parts.append(f"{field_id}: {msg}")
    detail = "; ".join(parts) if parts else (resp.text[:300] or f"HTTP {resp.status_code}")
    return f"Jira rejected request: {detail}"


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, verbose: bool = False):
        if not (base_url and email and api_token):
            raise JiraConfigError("Jira credentials are not configured.")
        self._base = base_url.rstrip("/")
        self._verbose = verbose
        self._session = requests.Session()
        self._session.auth = HTTPBasicAuth(email, api_token)
        self._session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    @property
    def base_url(self) -> str:
        return self._base

    def _url(self, path: str) -> str:
        return f"{self._base}/rest/api/3/{path.lstrip('/')}"

    def _log(self, msg: str):
        if self._verbose:
            print(f"[jira] {msg}")

    def _get(self, path: str, **params) -> dict | list:
        url = self._url(path)
        self._log(f"GET {url} params={params}")
        resp = self._session.get(url, params=params or None)
        if resp.status_code == 400:
            raise ValueError(_format_jira_error(resp))
        if resp.status_code == 401:
            raise JiraConfigError("Jira auth failed (401) — check email and API token.")
        if resp.status_code == 403:
            raise JiraConfigError("Jira permission denied (403).")
        if resp.status_code == 404:
            raise ValueError(f"Not found: {path}")
        resp.raise_for_status()
        return resp.json()

    def search_issues(self, jql: str, fields: list[str], max_results: int = 100) -> list[dict]:
        issues: list[dict] = []
        next_token: str | None = None
        while len(issues) < max_results:
            params: dict = {"jql": jql, "fields": ",".join(fields),
                            "maxResults": min(100, max_results - len(issues))}
            if next_token:
                params["nextPageToken"] = next_token
            data = self._get("search/jql", **params)
            issues.extend(data.get("issues", []))
            next_token = data.get("nextPageToken")
            if data.get("isLast") or not next_token:
                break
        return issues

    def get_issue_fields(self, issue_key: str, fields: list[str]) -> dict:
        data = self._get(f"issue/{issue_key}", fields=",".join(fields))
        return data.get("fields", {})

    def list_fields(self) -> list[dict]:
        return self._get("field")  # type: ignore[return-value]

    def list_projects(self) -> list[dict]:
        data = self._get("project/search", maxResults=200)
        return data.get("values", [])

    def list_components(self, project_key: str) -> list[dict]:
        return self._get(f"project/{project_key}/components")  # type: ignore[return-value]

    def verify_auth(self) -> dict:
        return self._get("myself")  # type: ignore[return-value]

    def _post(self, path: str, payload: dict) -> dict:
        url = self._url(path)
        self._log(f"POST {url}")
        resp = self._session.post(url, json=payload)
        if resp.status_code == 400:
            raise ValueError(_format_jira_error(resp))
        if resp.status_code == 401:
            raise JiraConfigError("Jira auth failed (401) — check email and API token.")
        if resp.status_code == 403:
            raise JiraConfigError("Jira permission denied (403).")
        if resp.status_code == 404:
            raise ValueError(f"Not found: {path}")
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def get_comments(self, issue_key: str, max_results: int = 50) -> list[dict]:
        data = self._get(f"issue/{issue_key}/comment", maxResults=max_results, orderBy="-created", expand="properties")
        return data.get("comments", [])

    def search_user(self, email: str) -> dict:
        results = self._get("user/search", query=email, maxResults=1)
        return results[0] if results else {}  # type: ignore[index]

    def set_comment_property(self, comment_id: str, key: str, value: dict) -> None:
        self._put(f"comment/{comment_id}/properties/{key}", value)

    def get_transitions(self, issue_key: str) -> list[dict]:
        data = self._get(f"issue/{issue_key}/transitions", expand="transitions.fields")
        return data.get("transitions", [])

    def do_transition(self, issue_key: str, transition_id: str, fields: dict | None = None,
                      comment: str | dict | None = None) -> None:
        payload: dict = {"transition": {"id": transition_id}}
        fields = dict(fields) if fields else {}
        # A comment is never settable through `fields` — Jira requires it in the
        # `update` section as an `add` op with an ADF body. Some workflows enforce
        # a comment via a validator that is NOT advertised in the transition
        # metadata (no `comment` field, not flagged required); it surfaces only as
        # a "Please Enter Comment" error on POST. Accept a comment from either the
        # dedicated arg or a stray `comment` screen field and route it correctly.
        comment = comment or fields.pop("comment", None)
        if fields:
            payload["fields"] = fields
        if comment:
            body = comment if isinstance(comment, dict) else _text_to_adf(comment)
            payload["update"] = {"comment": [{"add": {"body": body}}]}
        self._post(f"issue/{issue_key}/transitions", payload)

    def _put(self, path: str, payload: dict) -> dict:
        url = self._url(path)
        self._log(f"PUT {url}")
        resp = self._session.put(url, json=payload)
        if resp.status_code == 400:
            raise ValueError(_format_jira_error(resp))
        if resp.status_code == 401:
            raise JiraConfigError("Jira auth failed (401) — check email and API token.")
        if resp.status_code == 403:
            raise JiraConfigError("Jira permission denied (403).")
        if resp.status_code == 404:
            raise ValueError(f"Not found: {path}")
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def list_priorities(self) -> list[dict]:
        """Return all priorities available in this Jira instance."""
        return self._get("priority")  # type: ignore[return-value]

    def update_priority(self, issue_key: str, priority_name: str) -> None:
        """Set the priority field on an issue by priority name."""
        self._put(f"issue/{issue_key}", {"fields": {"priority": {"name": priority_name}}})

    def get_assignable_users(self, project_key: str, query: str = "", max_results: int = 100) -> list[dict]:
        params: dict = {"project": project_key, "maxResults": max_results}
        if query:
            params["query"] = query
        return self._get("user/assignable/search", **params)  # type: ignore[return-value]

    def update_assignee(self, issue_key: str, account_id: str | None) -> None:
        value = {"accountId": account_id} if account_id else None
        self._put(f"issue/{issue_key}", {"fields": {"assignee": value}})

    def update_duedate(self, issue_key: str, date_str: str | None) -> None:
        self._put(f"issue/{issue_key}", {"fields": {"duedate": date_str or None}})

    def _delete(self, path: str) -> None:
        url = self._url(path)
        self._log(f"DELETE {url}")
        resp = self._session.delete(url)
        if resp.status_code == 401:
            raise JiraConfigError("Jira auth failed (401).")
        if resp.status_code == 403:
            raise JiraConfigError("Jira permission denied (403).")
        if resp.status_code == 404:
            raise ValueError(f"Not found: {path}")
        resp.raise_for_status()

    def delete_comment(self, issue_key: str, comment_id: str) -> None:
        self._delete(f"issue/{issue_key}/comment/{comment_id}")

    def update_comment(self, issue_key: str, comment_id: str, text: str = "", segments: list[dict] | None = None) -> dict:
        if segments:
            content = []
            for seg in segments:
                if seg.get("type") == "mention":
                    content.append({"type": "mention",
                        "attrs": {"id": seg["id"], "text": seg["text"], "accessLevel": ""}})
                elif seg.get("text"):
                    content.append({"type": "text", "text": seg["text"]})
        else:
            content = [{"type": "text", "text": text}]
        adf = {"version": 1, "type": "doc",
               "content": [{"type": "paragraph", "content": content}]}
        return self._put(f"issue/{issue_key}/comment/{comment_id}", {"body": adf})

    def add_comment(self, issue_key: str, text: str = "", segments: list[dict] | None = None) -> dict:
        if segments:
            content = []
            for seg in segments:
                if seg.get("type") == "mention":
                    content.append({"type": "mention",
                        "attrs": {"id": seg["id"], "text": seg["text"], "accessLevel": ""}})
                elif seg.get("text"):
                    content.append({"type": "text", "text": seg["text"]})
        else:
            content = [{"type": "text", "text": text}]
        adf = {"version": 1, "type": "doc",
               "content": [{"type": "paragraph", "content": content}]}
        return self._post(f"issue/{issue_key}/comment", {"body": adf})
