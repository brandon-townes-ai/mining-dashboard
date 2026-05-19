#!/usr/bin/env python3
"""
Find open VSTAB Off Road Epics with no actionable child items.

Only considers epics that are themselves open (statusCategory != Done).
An epic is "empty" if it has no children, or every child's status is
one of: Closed, Unable to Reproduce, Resolved, Done.

Usage:
    python find_empty_epics.py
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import date

from dotenv import load_dotenv

load_dotenv()

_TERMINAL = {"closed", "unable to reproduce", "resolved", "done"}

_EPICS_JQL = (
    'project = VSTAB AND issuetype = "Epic" AND component = "Off Road" '
    "AND statusCategory != Done "
    "ORDER BY status ASC"
)


def _load_secrets_from_gcp():
    project_id = os.environ.get("PROJECT_ID")
    if not project_id:
        return
    if os.environ.get("JIRA_BASE_URL"):
        return
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        service = os.environ.get("K_SERVICE", "mining-dashboard")
        for env_var, secret_suffix in [
            ("JIRA_BASE_URL", "jira-base-url"),
            ("JIRA_EMAIL", "jira-email"),
            ("JIRA_API_TOKEN", "jira-api-token"),
        ]:
            name = f"projects/{project_id}/secrets/{service}-{secret_suffix}/versions/latest"
            resp = client.access_secret_version(request={"name": name})
            os.environ[env_var] = resp.payload.data.decode("utf-8")
    except Exception as e:
        print(f"[warning] could not load secrets from GCP: {e}", file=sys.stderr)


def _status_name(issue: dict) -> str:
    return ((issue.get("fields") or {}).get("status") or {}).get("name", "")


def find_empty_epics(jira_client) -> list[dict]:
    epics = jira_client.search_issues(_EPICS_JQL, fields=["summary", "status"], max_results=500)
    if not epics:
        return []

    keys_in = ", ".join(e["key"] for e in epics)
    children = jira_client.search_issues(
        f"parent in ({keys_in}) ORDER BY status ASC",
        fields=["summary", "status", "parent"],
        max_results=2000,
    )

    children_by_parent: dict[str, list[dict]] = defaultdict(list)
    for child in children:
        parent_key = ((child.get("fields") or {}).get("parent") or {}).get("key", "")
        if parent_key:
            children_by_parent[parent_key].append(child)

    empty = []
    for epic in epics:
        key = epic["key"]
        kids = children_by_parent.get(key, [])
        if not kids:
            empty.append({
                "key": key,
                "summary": (epic.get("fields") or {}).get("summary", ""),
                "status": _status_name(epic),
                "child_statuses": [],
            })
        elif all(_status_name(c).lower() in _TERMINAL for c in kids):
            empty.append({
                "key": key,
                "summary": (epic.get("fields") or {}).get("summary", ""),
                "status": _status_name(epic),
                "child_statuses": [_status_name(c) for c in kids],
            })

    return empty


def _summarise_statuses(statuses: list[str]) -> str:
    if not statuses:
        return "no children"
    counts: dict[str, int] = defaultdict(int)
    for s in statuses:
        counts[s] += 1
    return ", ".join(f"{s} ×{n}" if n > 1 else s for s, n in counts.items())


def main():
    _load_secrets_from_gcp()

    from src.jira_client import JiraClient, JiraConfigError

    try:
        client = JiraClient(
            base_url=os.environ["JIRA_BASE_URL"],
            email=os.environ["JIRA_EMAIL"],
            api_token=os.environ["JIRA_API_TOKEN"],
        )
    except (JiraConfigError, KeyError) as e:
        print(f"Error: {e}\nEnsure JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN are in .env", file=sys.stderr)
        sys.exit(1)

    print("Querying Jira...", file=sys.stderr)
    epics = find_empty_epics(client)

    today = date.today().isoformat()
    print(f"\n🔍 Empty VSTAB Epics  ({today})")
    print("─" * 76)

    if not epics:
        print("None found — all epics have open children.")
        return

    base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
    for e in epics:
        key_col = e["key"].ljust(12)
        status_col = e["status"].ljust(16)
        summary_col = e["summary"][:38].ljust(38)
        children_col = _summarise_statuses(e["child_statuses"])
        print(f"{key_col}  {status_col}  {summary_col}  [{children_col}]")

    print(f"\n{len(epics)} epic(s) found.")
    if base_url and epics:
        print(f"Open first: {base_url}/browse/{epics[0]['key']}")


if __name__ == "__main__":
    main()
