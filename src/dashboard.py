from __future__ import annotations

import os
import pathlib
import re
from datetime import datetime, timezone, timedelta
from threading import RLock

from flask import Flask, jsonify, render_template_string, request

_STATIC_DIR = pathlib.Path(__file__).parent.parent / "static"


from .config import APP_VERSION as _APP_VERSION, ConfigStore
from .jira_client import JiraClient, JiraConfigError


class ClientHolder:
    """Owns the live JiraClient. Reads credentials from environment."""

    def __init__(self):
        self._client: JiraClient | None = None
        self._lock = RLock()

    def get(self) -> JiraClient:
        with self._lock:
            if self._client is None:
                self._client = JiraClient(
                    os.environ.get("JIRA_BASE_URL", ""),
                    os.environ.get("JIRA_EMAIL", ""),
                    os.environ.get("JIRA_API_TOKEN", ""),
                )
            return self._client


def create_app(store: ConfigStore) -> Flask:
    app = Flask(__name__, static_folder=str(_STATIC_DIR))
    holder = ClientHolder()

    @app.errorhandler(JiraConfigError)
    def _config_err(exc):
        return jsonify({"error": str(exc), "code": "config"}), 412

    @app.errorhandler(ValueError)
    def _value_err(exc):
        return jsonify({"error": str(exc), "code": "bad_request"}), 400

    @app.get("/")
    def index():
        version = os.environ.get("APP_VERSION", _APP_VERSION)
        email = os.environ.get("JIRA_EMAIL", "")
        user_id = email.split("@")[0] if "@" in email else ""
        profile_url = (
            f"https://anaheim.applied.co/anaheim/appliedistan/about?userId={user_id}"
            if user_id else ""
        )
        return render_template_string(DASHBOARD_HTML, version=version, profile_url=profile_url)

    @app.get("/api/config")
    def api_config_get():
        return jsonify(store.public_dict())

    @app.get("/api/jira/status")
    def api_jira_status():
        try:
            client = holder.get()
            me = client.verify_auth()
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc),
                            "base_url": os.environ.get("JIRA_BASE_URL", "")}), 200
        return jsonify({
            "ok": True,
            "base_url": client.base_url,
            "user": me.get("displayName") or me.get("emailAddress"),
        })

    @app.get("/api/views")
    def api_views_list():
        return jsonify({"active": store.get_active(), "views": store.list_views()})

    @app.put("/api/views/<name>")
    def api_views_upsert(name: str):
        body = request.get_json(silent=True) or {}
        store.upsert_view(name, body)
        return jsonify({"ok": True, "view": store.get_view(name)})

    @app.delete("/api/views/<name>")
    def api_views_delete(name: str):
        ok = store.delete_view(name)
        if not ok:
            return jsonify({"error": "cannot delete (last view, or not found)"}), 400
        return jsonify({"ok": True})

    @app.post("/api/active_view")
    def api_active_view():
        body = request.get_json(silent=True) or {}
        name = body.get("name", "")
        if not store.set_active(name):
            return jsonify({"error": "view not found"}), 404
        return jsonify({"ok": True})

    @app.get("/api/jira/fields")
    def api_jira_fields():
        fields = holder.get().list_fields()
        return jsonify(fields)

    @app.get("/api/jira/projects")
    def api_jira_projects():
        return jsonify(holder.get().list_projects())

    @app.get("/api/jira/components")
    def api_jira_components():
        project = (request.args.get("project") or "").strip()
        if not project:
            return jsonify({"error": "project query param required"}), 400
        return jsonify(holder.get().list_components(project))

    @app.get("/api/children/<issue_key>")
    def api_children(issue_key: str):
        columns = request.args.get("columns")
        view_name = request.args.get("view") or store.get_active()
        view = store.get_view(view_name) or {}
        column_list = [c.strip() for c in columns.split(",")] if columns else list(view.get("columns", []))
        request_fields = _expand_request_fields(column_list)
        client = holder.get()
        jql = f"parent = {issue_key} ORDER BY status ASC"
        issues = client.search_issues(jql, request_fields, max_results=50)
        tickets = [_shape_issue(i["key"], i.get("fields", {}), column_list, client.base_url)
                   for i in issues]
        return jsonify({"jira_base_url": client.base_url, "columns": column_list, "tickets": tickets})

    @app.get("/api/stale-children")
    def api_stale_children():
        view_name = request.args.get("view") or store.get_active()
        view = store.get_view(view_name)
        if not view:
            return jsonify({"stale": [], "jira_base_url": ""})
        client = holder.get()
        mode = view["mode"]
        if mode == "jql":
            jql = view.get("jql", "").strip()
            if not jql:
                return jsonify({"stale": [], "jira_base_url": client.base_url})
            jql = _resolve_jql(jql, client)
            parent_issues = client.search_issues(jql, ["summary", "issuetype"], max_results=100)
        else:
            parent_issues = [{"key": k, "fields": {"summary": ""}} for k in (view.get("keys") or [])]
        if not parent_issues:
            return jsonify({"stale": [], "jira_base_url": client.base_url})
        # Only fetch children of epics — non-epic top-level tickets appear directly in the table
        epic_issues = [i for i in parent_issues
                       if (i.get("fields") or {}).get("issuetype", {}).get("name", "").lower() == "epic"]
        if not epic_issues:
            return jsonify({"stale": [], "jira_base_url": client.base_url})
        parent_keys_in = ", ".join(i["key"] for i in epic_issues)
        parent_summary = {i["key"]: (i.get("fields") or {}).get("summary", "") for i in epic_issues}
        child_fields = ["summary", "status", "updated", "parent", "priority", "assignee"]
        children = client.search_issues(
            f"parent in ({parent_keys_in}) ORDER BY updated ASC", child_fields, max_results=500
        )
        stale = []
        for c in children:
            fields = c.get("fields") or {}
            if _is_stale(fields):
                parent_key = (fields.get("parent") or {}).get("key", "")
                stale.append({
                    "key": c["key"],
                    "summary": fields.get("summary") or "",
                    "updated": fields.get("updated") or "",
                    "status": (fields.get("status") or {}).get("name", ""),
                    "priority": (fields.get("priority") or {}).get("name", ""),
                    "assignee": (fields.get("assignee") or {}).get("displayName", ""),
                    "parent_key": parent_key,
                    "parent_summary": parent_summary.get(parent_key, ""),
                })
        return jsonify({"stale": stale, "jira_base_url": client.base_url})

    @app.get("/api/tickets")
    def api_tickets():
        view_name = request.args.get("view") or store.get_active()
        view = store.get_view(view_name)
        if not view:
            return jsonify({"error": f"view not found: {view_name}"}), 404

        mode = request.args.get("mode") or view["mode"]
        jql = request.args.get("jql") if request.args.get("jql") is not None else view["jql"]
        keys = (request.args.get("keys") or "").strip()
        if not keys and view.get("keys"):
            keys = ",".join(view["keys"])
        columns = request.args.get("columns")
        column_list = [c.strip() for c in columns.split(",")] if columns else list(view["columns"])

        request_fields = _expand_request_fields(column_list)

        client = holder.get()
        if mode == "jql":
            if not jql.strip():
                return jsonify({"error": "JQL is empty"}), 400
            jql = _resolve_jql(jql, client)
            issues = client.search_issues(jql, request_fields)
            tickets = [_shape_issue(i["key"], i.get("fields", {}), column_list, client.base_url)
                       for i in issues]
            # Also fetch child epics one level down (sub-epics of the returned epics)
            level1_epic_keys = [i["key"] for i in issues
                                if (i.get("fields") or {}).get("issuetype", {}).get("name", "").lower() == "epic"]
            if level1_epic_keys:
                try:
                    keys_in = ", ".join(level1_epic_keys)
                    child_epic_issues = client.search_issues(
                        f"issuetype = Epic AND parent in ({keys_in})", request_fields
                    )
                    existing_keys = {t["key"] for t in tickets}
                    for ci in child_epic_issues:
                        if ci["key"] not in existing_keys:
                            tickets.append(_shape_issue(ci["key"], ci.get("fields", {}), column_list, client.base_url))
                except Exception:
                    pass
            # Also fetch parent epics one level up (the containers that hold the returned epics)
            parent_keys = {t.get("parent_key") for t in tickets if t.get("parent_key")}
            existing_keys = {t["key"] for t in tickets}
            parent_keys -= existing_keys
            if parent_keys:
                try:
                    keys_in = ", ".join(parent_keys)
                    parent_epic_issues = client.search_issues(
                        f"issuetype = Epic AND key in ({keys_in})", request_fields
                    )
                    for pi in parent_epic_issues:
                        if pi["key"] not in existing_keys:
                            tickets.append(_shape_issue(pi["key"], pi.get("fields", {}), column_list, client.base_url))
                except Exception:
                    pass
        else:
            keylist = [k.strip() for k in keys.replace(",", " ").split() if k.strip()]
            if not keylist:
                return jsonify({"error": "no issue keys provided"}), 400
            tickets = []
            for k in keylist:
                try:
                    f = client.get_issue_fields(k, request_fields)
                    tickets.append(_shape_issue(k, f, column_list, client.base_url))
                except (ValueError, JiraConfigError) as exc:
                    tickets.append({"key": k, "_error": str(exc),
                                    "summary": str(exc), "values": {}})
        # Deduplicate: drop non-epic tickets whose parent is also in the result (prevents double-rendering
        # child issues that appear both standalone and as expanded children). Epics are always kept
        # even if nested, since sub-epics are intentionally included at the top level.
        top_level_keys = {t["key"] for t in tickets}
        tickets = [t for t in tickets
                   if t.get("is_epic") or not t.get("parent_key") or t["parent_key"] not in top_level_keys]

        # One batch query to find which tickets have children
        keys_with_children: set[str] = set()
        if tickets:
            keys_in = ", ".join(t["key"] for t in tickets)
            try:
                child_hits = client.search_issues(
                    f"parent in ({keys_in})", ["parent"], max_results=500
                )
                for hit in child_hits:
                    pk = ((hit.get("fields") or {}).get("parent") or {}).get("key")
                    if pk:
                        keys_with_children.add(pk)
            except Exception:
                pass

        for t in tickets:
            t["has_children"] = t["key"] in keys_with_children

        return jsonify({"jira_base_url": client.base_url, "columns": column_list, "tickets": tickets})

    return app


def _resolve_jql(jql: str, client: JiraClient) -> str:
    """Expand parent in childIssuesOf("KEY") into a concrete two-level parent in (...) query."""
    m = re.match(
        r'parent\s+in\s+childIssuesOf\(["\']?([A-Z]+-\d+)["\']?\)(.*)',
        jql.strip(), re.IGNORECASE
    )
    if not m:
        return jql
    parent_key, rest = m.group(1), m.group(2).strip()
    intermediate = client.search_issues(
        f"parent = {parent_key}", ["summary"], max_results=200
    )
    if not intermediate:
        return jql
    keys_in = ", ".join(i["key"] for i in intermediate)
    resolved = f"parent in ({keys_in})"
    return f"{resolved} {rest}".strip() if rest else resolved


SPECIAL_COLUMNS = {"key"}
COLUMN_TO_FIELD = {"epic": "parent", "occurrence_count": "subtasks"}


def _expand_request_fields(columns: list[str]) -> list[str]:
    fields: set[str] = {"updated", "status", "issuetype", "parent"}
    for c in columns:
        if c in SPECIAL_COLUMNS:
            continue
        fields.add(COLUMN_TO_FIELD.get(c, c))
    fields.add("summary")
    return sorted(fields)


_STALE_THRESHOLD = timedelta(hours=24)
_STALE_EXCLUDE_NAMES = {"blocked", "containment"}


def _is_stale(fields: dict) -> bool:
    status = fields.get("status") or {}
    category = (status.get("statusCategory") or {}).get("key", "")
    name = (status.get("name") or "").lower()
    if category == "done" or name in _STALE_EXCLUDE_NAMES:
        return False
    updated_str = fields.get("updated")
    if not updated_str:
        return False
    try:
        ts = re.sub(r"\.\d+", "", updated_str).replace("Z", "+00:00")
        ts = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", ts)
        updated = datetime.fromisoformat(ts)
        return datetime.now(timezone.utc) - updated > _STALE_THRESHOLD
    except (ValueError, TypeError):
        return False


def _shape_issue(key: str, fields: dict, columns: list[str], base_url: str) -> dict:
    values: dict = {}
    for col in columns:
        if col == "key":
            continue
        field_id = COLUMN_TO_FIELD.get(col, col)
        values[col] = _render_field(field_id, fields.get(field_id), base_url)
    return {"key": key, "summary": fields.get("summary") or "", "values": values,
            "is_stale": _is_stale(fields),
            "is_epic": (fields.get("issuetype") or {}).get("name", "").lower() == "epic",
            "parent_key": (fields.get("parent") or {}).get("key", "")}


def _render_field(field_id: str, raw, base_url: str):
    if raw is None or raw == "" or raw == []:
        return {"type": "empty", "display": "", "sort": ""}

    if isinstance(raw, dict):
        if "statusCategory" in raw:
            cat = (raw.get("statusCategory") or {}).get("key") or "undefined"
            name = raw.get("name") or ""
            return {"type": "status", "display": name, "sort": name, "category": cat}
        if "displayName" in raw or "emailAddress" in raw or "accountId" in raw:
            name = raw.get("displayName") or raw.get("emailAddress") or ""
            return {"type": "user", "display": name, "sort": name,
                    "avatar": (raw.get("avatarUrls") or {}).get("24x24")}
        if "name" in raw and "iconUrl" in raw and "id" in raw:
            return {"type": "option", "display": raw.get("name") or "", "sort": raw.get("name") or "",
                    "icon": raw.get("iconUrl")}
        if "key" in raw and "fields" in raw:
            pkey = raw.get("key")
            psummary = (raw.get("fields") or {}).get("summary") or ""
            return {"type": "issue", "display": pkey, "summary": psummary, "sort": pkey,
                    "url": f"{base_url}/browse/{pkey}"}
        if "key" in raw and "name" in raw:
            return {"type": "option", "display": raw.get("name") or raw.get("key") or "",
                    "sort": raw.get("name") or ""}
        if "value" in raw:
            return {"type": "option", "display": str(raw.get("value")), "sort": str(raw.get("value"))}
        if "name" in raw:
            return {"type": "option", "display": raw.get("name") or "", "sort": raw.get("name") or ""}
        return {"type": "json", "display": _short_json(raw), "sort": _short_json(raw)}

    if isinstance(raw, list):
        if field_id == "subtasks":
            count = len(raw)
            return {"type": "number", "display": str(count), "sort": count}
        items = [_render_field(field_id, x, base_url) for x in raw]
        labels = [i.get("display") for i in items if i.get("display")]
        return {"type": "list", "items": items, "display": ", ".join(labels), "sort": ", ".join(labels)}

    if isinstance(raw, bool):
        s = "yes" if raw else "no"
        return {"type": "bool", "display": s, "sort": s}

    if isinstance(raw, (int, float)):
        return {"type": "number", "display": str(raw), "sort": raw}

    if isinstance(raw, str):
        if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-" and (raw[10:11] in ("T", " ", "")):
            return {"type": "date", "display": raw[:10], "sort": raw}
        return {"type": "text", "display": raw, "sort": raw}

    return {"type": "json", "display": str(raw), "sort": str(raw)}


def _short_json(d) -> str:
    import json as _json
    s = _json.dumps(d, default=str)
    return s if len(s) <= 80 else s[:77] + "…"


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Mining Dashboard</title>
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=DM+Mono:ital,wght@0,400;0,500;1,400&family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600&display=swap" rel="stylesheet">
<style>
  /* ═══ TOKENS ═══════════════════════════════════════════════════ */
  :root {
    color-scheme: light;
    --bg:           #f0f1f5;
    --bg-card:      #ffffff;
    --bg-subtle:    #f7f8fa;
    --bg-hover:     #eef0f5;
    --bg-row-hover: #f5f6f9;
    --bg-input:     #ffffff;
    --bg-sidebar:   var(--bg-card);
    --text:         #0d1117;
    --text-secondary: #5C637A;
    --text-sidebar: var(--text-secondary);
    --sb-divider:       var(--border);
    --sb-brand-text:    var(--text);
    --sb-brand-sub:     var(--muted);
    --sb-label:         var(--muted);
    --sb-item-hover:    var(--bg-hover);
    --sb-item-active:   rgba(15,106,242,0.08);
    --sb-item-dot:      var(--border);
    --sb-new-border:    var(--border);
    --sb-new-text:      var(--muted);
    --sb-footer-text:   var(--muted);
    --sb-scrollbar:     var(--border);
    --border:        #D8D9DF;
    --border-subtle: #eaecf2;
    --muted:  #9A9EAD;
    --accent: #0F6AF2;
    --done:       #389F3D;
    --inprog:     #0F6AF2;
    --todo:       #9A9EAD;
    --warn:       #FA9005;
    --blocked:    #EB3737;
    --triage:     #FCC90D;
    --containment:#8B4AF5;
    --corrective: #22B8D3;
    --waiting:    #FA9005;
    --badge-todo-bg:    #eaecf2; --badge-todo-fg:    #5C637A;
    --badge-inprog-bg:  #dbeafe; --badge-inprog-fg:  #0F6AF2;
    --badge-done-bg:    #dcfce7; --badge-done-fg:    #389F3D;
    --badge-blocked-bg: #fee2e2; --badge-blocked-fg: #EB3737;
    --toast-bg: #0d1117;
    --sidebar-w: 244px;
    --panel-w:   380px;
    --topbar-h:  50px;
    --shadow-sm: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
    --shadow-md: 0 4px 12px rgba(0,0,0,0.08), 0 2px 4px rgba(0,0,0,0.04);
    --shadow-lg: 0 16px 40px rgba(0,0,0,0.14), 0 4px 8px rgba(0,0,0,0.06);
    --r: 8px; --r-sm: 5px;
    --t: 0.18s ease;
  }

  [data-theme="dark"] {
    color-scheme: dark;
    --bg:           #080b10;
    --bg-card:      #0f1318;
    --bg-subtle:    #161b22;
    --bg-hover:     #1c2128;
    --bg-row-hover: #181d24;
    --bg-input:     #0d1117;
    --bg-sidebar:   #060810;
    --text:         #e6edf3;
    --text-secondary: #8b949e;
    --text-sidebar: rgba(255,255,255,0.65);
    --sb-divider:       rgba(255,255,255,0.06);
    --sb-brand-text:    #ffffff;
    --sb-brand-sub:     rgba(255,255,255,0.3);
    --sb-label:         rgba(255,255,255,0.25);
    --sb-item-hover:    rgba(255,255,255,0.05);
    --sb-item-active:   rgba(15,106,242,0.18);
    --sb-item-dot:      rgba(255,255,255,0.12);
    --sb-new-border:    rgba(255,255,255,0.1);
    --sb-new-text:      rgba(255,255,255,0.32);
    --sb-footer-text:   rgba(255,255,255,0.2);
    --sb-scrollbar:     rgba(255,255,255,0.08);
    --border:        #21262d;
    --border-subtle: #161b22;
    --muted:         #6e7681;
    --accent:        #58a6ff;
    --done:          #3fb950;
    --inprog:        #58a6ff;
    --todo:          #6e7681;
    --warn:          #d29922;
    --waiting:       #d29922;
    --triage:        #e3b341;
    --badge-todo-bg:    #2d333b; --badge-todo-fg:    #adbac7;
    --badge-inprog-bg:  #0c2d6b; --badge-inprog-fg:  #58a6ff;
    --badge-done-bg:    #0d3b1e; --badge-done-fg:    #3fb950;
    --badge-blocked-bg: #420c0c; --badge-blocked-fg: #f87171;
    --toast-bg: #2d333b;
  }

  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --bg: #080b10; --bg-card: #0f1318; --bg-subtle: #161b22; --bg-hover: #1c2128;
      --bg-row-hover: #181d24; --bg-input: #0d1117; --bg-sidebar: #060810;
      --text: #e6edf3; --text-secondary: #8b949e; --text-sidebar: rgba(255,255,255,0.65);
      --border: #21262d; --border-subtle: #161b22; --muted: #6e7681;
      --accent: #58a6ff; --done: #3fb950; --inprog: #58a6ff; --todo: #6e7681;
      --warn: #d29922; --waiting: #d29922; --triage: #e3b341;
      --badge-todo-bg: #2d333b; --badge-todo-fg: #adbac7;
      --badge-inprog-bg: #0c2d6b; --badge-inprog-fg: #58a6ff;
      --badge-done-bg: #0d3b1e; --badge-done-fg: #3fb950;
      --badge-blocked-bg: #420c0c; --badge-blocked-fg: #f87171;
      --toast-bg: #2d333b;
      --sb-divider: rgba(255,255,255,0.06); --sb-brand-text: #ffffff;
      --sb-brand-sub: rgba(255,255,255,0.3); --sb-label: rgba(255,255,255,0.25);
      --sb-item-hover: rgba(255,255,255,0.05); --sb-item-active: rgba(15,106,242,0.18);
      --sb-item-dot: rgba(255,255,255,0.12); --sb-new-border: rgba(255,255,255,0.1);
      --sb-new-text: rgba(255,255,255,0.32); --sb-footer-text: rgba(255,255,255,0.2);
      --sb-scrollbar: rgba(255,255,255,0.08);
    }
  }

  /* ═══ RESET ══════════════════════════════════════════════════════ */
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif;
    font-size: 14px;
    line-height: 1.5;
    background: var(--bg);
    color: var(--text);
    display: flex;
    height: 100vh;
    overflow: hidden;
  }

  /* ═══ SIDEBAR ═══════════════════════════════════════════════════ */
  .sidebar {
    width: var(--sidebar-w);
    background: var(--bg-sidebar);
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
    transition: width var(--t);
    overflow: hidden;
    z-index: 20;
  }
  .sidebar.collapsed { width: 0; }

  .sidebar-brand {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 18px 16px 14px;
    border-bottom: 1px solid var(--sb-divider);
    flex-shrink: 0;
  }

  .brand-text {
    font-family: 'Syne', sans-serif;
    font-weight: 800;
    font-size: 12.5px;
    line-height: 1.25;
    color: var(--sb-brand-text);
    white-space: nowrap;
  }
  .brand-text small {
    display: block;
    font-family: 'DM Sans', sans-serif;
    font-weight: 400;
    font-size: 10px;
    color: var(--sb-brand-sub);
    letter-spacing: 0.07em;
    text-transform: uppercase;
    margin-top: 1px;
  }

  .sidebar-body {
    flex: 1;
    padding: 10px 8px;
    overflow-y: auto;
  }
  .sidebar-body::-webkit-scrollbar { width: 3px; }
  .sidebar-body::-webkit-scrollbar-thumb { background: var(--sb-scrollbar); border-radius: 2px; }

  .sidebar-section-label {
    font-size: 9.5px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--sb-label);
    padding: 4px 8px 7px;
    white-space: nowrap;
  }

  .view-item {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    text-align: left;
    padding: 7px 10px;
    border: none;
    background: none;
    border-radius: 6px;
    cursor: pointer;
    color: var(--text-sidebar);
    font: inherit;
    font-size: 13px;
    transition: background var(--t), color var(--t);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-bottom: 1px;
  }
  .view-item::before {
    content: '';
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: var(--sb-item-dot);
    flex-shrink: 0;
    transition: background var(--t);
  }
  .view-item:hover { background: var(--sb-item-hover); color: var(--sb-brand-text); }
  .view-item.active { background: var(--sb-item-active); color: var(--sb-brand-text); }
  .view-item.active::before { background: var(--accent); }
  .view-lock { font-size: 9px; opacity: 0.45; margin-left: auto; flex-shrink: 0; }

  .sidebar-new-view {
    width: 100%;
    text-align: left;
    padding: 6px 10px;
    border: 1px dashed var(--sb-new-border);
    background: none;
    border-radius: 6px;
    cursor: pointer;
    color: var(--sb-new-text);
    font: inherit;
    font-size: 12px;
    transition: all var(--t);
    margin-top: 6px;
    white-space: nowrap;
  }
  .sidebar-new-view:hover { border-color: var(--sb-label); color: var(--text-sidebar); }

  .sidebar-footer {
    padding: 10px 14px;
    border-top: 1px solid var(--sb-divider);
    flex-shrink: 0;
  }
  .sidebar-footer-meta {
    font-size: 10px;
    color: var(--sb-footer-text);
    white-space: nowrap;
    font-family: 'DM Mono', monospace;
  }

  /* ═══ MAIN ═══════════════════════════════════════════════════════ */
  .main {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    min-width: 0;
  }

  /* ─── TOP BAR ─────────────────────────────────── */
  .topbar {
    height: var(--topbar-h);
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0 16px;
    background: var(--bg-card);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }

  .topbar-menu {
    background: none;
    border: none;
    cursor: pointer;
    padding: 5px 7px;
    border-radius: var(--r-sm);
    color: var(--muted);
    font-size: 15px;
    line-height: 1;
    transition: background var(--t), color var(--t);
  }
  .topbar-menu:hover { background: var(--bg-hover); color: var(--text); }

  .active-view-label {
    font-size: 12px;
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 180px;
  }

  .grow { flex: 1; min-width: 0; }

  .status-chip {
    font-size: 11.5px;
    color: var(--muted);
    font-family: 'DM Mono', monospace;
    white-space: nowrap;
  }

  .topbar-btn {
    background: none;
    border: 1px solid var(--border);
    cursor: pointer;
    padding: 5px 10px;
    border-radius: var(--r-sm);
    color: var(--text-secondary);
    font: inherit;
    font-size: 12px;
    transition: all var(--t);
    white-space: nowrap;
    flex-shrink: 0;
  }
  .topbar-btn:hover { background: var(--bg-hover); color: var(--text); border-color: var(--muted); }
  .topbar-btn.accent { background: var(--accent); color: #fff; border-color: var(--accent); }
  .topbar-btn.accent:hover { opacity: 0.88; }

  /* ─── HERO ──────────────────────────────────────── */
  .hero {
    background: var(--bg-card);
    border-bottom: 1px solid var(--border);
    padding: 22px 24px 32px;
    flex-shrink: 0;
    position: relative;
    overflow: visible;
  }
  .hero::after {
    content: '';
    position: absolute;
    top: -40px; right: -40px;
    width: 280px; height: 180px;
    background: radial-gradient(ellipse, rgba(15,106,242,0.07) 0%, transparent 68%);
    pointer-events: none;
  }

  .hero-title {
    font-family: 'Syne', sans-serif;
    font-weight: 800;
    font-size: clamp(22px, 3vw, 36px);
    letter-spacing: -0.025em;
    line-height: 1.3;
    padding-bottom: 3px;
    color: var(--text);
    position: relative;
  }
  .hero-title em {
    font-style: normal;
    color: var(--accent);
  }
  .hero-icon {
    display: inline-block;
    vertical-align: middle;
    font-size: 0.8em;
    line-height: 1;
    margin-right: 0.1em;
    position: relative;
    top: -0.05em;
  }

  .hero-meta {
    display: flex;
    align-items: center;
    gap: 14px;
    margin-top: 7px;
    flex-wrap: wrap;
  }

  .hero-jira {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--muted);
  }

  .hero-user-link {
    color: inherit;
    text-decoration: none;
    border-bottom: 1px solid currentColor;
    opacity: 0.7;
    transition: opacity var(--t);
  }
  .hero-user-link:hover { opacity: 1; }

  .status-dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    background: var(--muted);
    flex-shrink: 0;
    transition: background var(--t);
  }
  .status-dot.ok  { background: var(--done); }
  .status-dot.err { background: var(--blocked); }

  /* ─── SCROLLABLE CONTENT ─────────────────────── */
  .content {
    flex: 1;
    overflow-y: auto;
    padding: 18px 20px 28px;
  }
  .content::-webkit-scrollbar { width: 5px; }
  .content::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  /* ─── ALERT BAR ─────────────────────────────────── */
  .alert-bar {
    background: #fffbeb;
    border-bottom: 1px solid #fcd34d;
    border-left: 4px solid #dc2626;
    padding: 8px 14px;
    display: flex;
    align-items: center;
    gap: 10px;
    flex-shrink: 0;
  }
  [data-theme="dark"] .alert-bar { background: #1a1200; border-bottom-color: #78350f; }
  [data-theme="dark"] .alert-item { background: #271d07; border-color: #78350f; }
  [data-theme="dark"] .alert-bar-label { color: #fbbf24; }
  [data-theme="dark"] .alert-dismiss { color: #fbbf24; }
  .alert-bar.hidden { display: none; }
  .alert-bar-label {
    font-size: 10.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: #b45309;
    white-space: nowrap;
    flex-shrink: 0;
  }
  .alert-items {
    display: flex;
    gap: 6px;
    flex: 1;
    overflow-x: auto;
    flex-wrap: nowrap;
  }
  .alert-items::-webkit-scrollbar { height: 3px; }
  .alert-items::-webkit-scrollbar-thumb { background: #fcd34d; border-radius: 2px; }
  .alert-item {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    background: #fff;
    border: 1px solid #fde68a;
    border-radius: var(--r-sm);
    padding: 3px 8px;
    font-size: 12px;
    white-space: nowrap;
    flex-shrink: 0;
    transition: border-color var(--t);
  }
  .alert-item:hover { border-color: #f59e0b; }
  .alert-priority-badge {
    font-size: 10px;
    font-weight: 700;
    padding: 1px 5px;
    border-radius: 3px;
  }
  .alert-priority-badge.p0, .alert-priority-badge.critical  { background: #fee2e2; color: #991b1b; }
  .alert-priority-badge.p1, .alert-priority-badge.high      { background: #ffedd5; color: #9a3412; }
  .alert-priority-badge.p2, .alert-priority-badge.medium    { background: #dbeafe; color: #1e40af; }
  .alert-priority-badge.p3, .alert-priority-badge.low       { background: #f0fdf4; color: #166534; }
  .alert-priority-badge.p4, .alert-priority-badge.lowest    { background: var(--bg-subtle); color: var(--muted); }
  .alert-key { font-family: 'DM Mono', monospace; font-size: 11px; color: var(--accent); text-decoration: none; }
  .alert-key:hover { text-decoration: underline; }
  .alert-summary { max-width: 180px; overflow: hidden; text-overflow: ellipsis; color: var(--text-secondary); font-size: 12px; }
  .alert-occ {
    font-size: 10px;
    font-weight: 700;
    background: #dc2626;
    color: #fff;
    border-radius: 10px;
    padding: 1px 6px;
    flex-shrink: 0;
  }
  .alert-dismiss {
    background: none;
    border: none;
    cursor: pointer;
    color: #b45309;
    font-size: 16px;
    padding: 0 2px;
    flex-shrink: 0;
    line-height: 1;
    opacity: 0.6;
    transition: opacity var(--t);
  }
  .alert-dismiss:hover { opacity: 1; }


  /* ─── TOOLBAR ────────────────────────────────── */
  .toolbar {
    display: flex;
    gap: 8px;
    align-items: center;
    padding: 10px 12px;
    margin-bottom: 14px;
    flex-wrap: wrap;
  }

  .toolbar-mode {
    font: inherit; font-size: 13px;
    padding: 6px 10px;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    background: var(--bg-input);
    color: var(--text);
    cursor: pointer;
    flex-shrink: 0;
  }

  .toolbar-query {
    flex: 1; min-width: 200px;
    font-family: 'DM Mono', monospace;
    font-size: 12.5px;
    padding: 6px 12px;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    background: var(--bg-input);
    color: var(--text);
    transition: border-color var(--t);
  }
  .toolbar-query:focus { outline: none; border-color: var(--accent); }
  .toolbar-query::placeholder { color: var(--muted); }

  .btn-primary {
    font: inherit; font-size: 13px; font-weight: 500;
    padding: 6px 16px;
    border: none; border-radius: var(--r-sm);
    background: var(--accent); color: #fff;
    cursor: pointer;
    transition: opacity var(--t), transform var(--t);
    flex-shrink: 0;
  }
  .btn-primary:hover { opacity: 0.86; }
  .btn-primary:active { transform: scale(0.97); }

  .btn-ghost {
    font: inherit; font-size: 13px;
    padding: 6px 12px;
    border: 1px solid var(--border); border-radius: var(--r-sm);
    background: none; color: var(--text-secondary);
    cursor: pointer;
    transition: all var(--t);
    flex-shrink: 0;
  }
  .btn-ghost:hover { background: var(--bg-hover); color: var(--text); }
  .btn-ghost.active { background: var(--bg-hover); color: var(--text); border-color: var(--muted); font-weight: 600; }

  /* ─── CARD ─────────────────────────────────────── */
  .card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--r);
    box-shadow: var(--shadow-sm);
  }

  /* ─── KPI TILES ─────────────────────────────────── */
  .kpis {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
    gap: 10px;
    margin-bottom: 14px;
  }

  .kpi {
    position: relative;
    padding: 13px 14px 12px 18px;
    overflow: hidden;
    transition: transform var(--t), box-shadow var(--t);
    cursor: default;
  }
  .kpi:hover { transform: translateY(-1px); box-shadow: var(--shadow-md); }

  .kpi::before {
    content: '';
    position: absolute;
    left: 0; top: 0; bottom: 0;
    width: 3px;
    border-radius: var(--r) 0 0 var(--r);
    background: var(--muted);
  }
  .kpi.done::before        { background: var(--done); }
  .kpi.inprog::before      { background: var(--inprog); }
  .kpi.todo::before        { background: var(--todo); }
  .kpi.blocked::before     { background: var(--blocked); }
  .kpi.triage::before      { background: var(--triage); }
  .kpi.waiting::before     { background: var(--waiting); }
  .kpi.containment::before { background: var(--containment); }
  .kpi.corrective::before  { background: var(--corrective); }
  .kpi.stale::before       { background: var(--warn); }

  .kpi-label {
    font-size: 9.5px;
    font-weight: 700;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 5px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .kpi-value {
    font-family: 'Syne', sans-serif;
    font-weight: 700;
    font-size: 26px;
    line-height: 1;
    color: var(--text);
  }
  .kpi.done .kpi-value        { color: var(--done); }
  .kpi.inprog .kpi-value      { color: var(--inprog); }
  .kpi.blocked .kpi-value     { color: var(--blocked); }
  .kpi.triage .kpi-value      { color: var(--triage); }
  .kpi.containment .kpi-value { color: var(--containment); }
  .kpi.corrective .kpi-value  { color: var(--corrective); }
  .kpi.waiting .kpi-value     { color: var(--waiting); }
  .kpi.stale .kpi-value       { color: var(--warn); }

  .kpi-sub {
    font-size: 10.5px;
    color: var(--muted);
    margin-top: 4px;
    font-family: 'DM Mono', monospace;
  }

  /* ─── CHARTS SECTION ──────────────────────────── */
  .charts-section { margin-bottom: 14px; }

  .charts-toggle {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    padding: 8px 12px;
    cursor: pointer;
    color: var(--text-secondary);
    font: inherit;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    transition: background var(--t), color var(--t), border-color var(--t);
    box-shadow: var(--shadow-sm);
  }
  .charts-toggle:hover { background: var(--bg-hover); color: var(--text); border-color: var(--muted); }

  .charts-toggle-line {
    flex: 1;
    height: 1px;
    background: var(--border);
  }

  .charts-icon {
    font-size: 14px;
    line-height: 1;
    transition: transform var(--t);
    color: var(--muted);
  }
  .charts-icon.collapsed { transform: rotate(-90deg); }

  .charts {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 10px;
  }
  .charts.hidden { display: none; }

  .chart-card { padding: 12px 14px; }
  .chart-card h3 {
    font-size: 9.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    margin-bottom: 10px;
  }
  .canvas-wrap { position: relative; height: 120px; }

  .chart-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 5px 12px;
    margin-top: 10px;
    padding-top: 8px;
    border-top: 1px solid var(--border-subtle);
  }
  .chart-legend-item {
    display: flex;
    align-items: center;
    gap: 5px;
    font-size: 11px;
    color: var(--text-secondary);
    white-space: nowrap;
    cursor: pointer;
    transition: opacity var(--t);
    user-select: none;
  }
  .chart-legend-item:hover { opacity: 0.75; }
  .chart-legend-item.legend-hidden { opacity: 0.35; text-decoration: line-through; }
  .chart-legend-swatch {
    width: 8px;
    height: 8px;
    border-radius: 2px;
    flex-shrink: 0;
  }

  /* ─── TABLE ─────────────────────────────────────── */
  .table-wrap { overflow-x: auto; }

  table { width: 100%; border-collapse: collapse; }

  th, td {
    text-align: left;
    padding: 8px 12px;
    border-bottom: 1px solid var(--border-subtle);
    font-size: 13px;
    vertical-align: middle;
  }

  th {
    background: var(--bg-subtle);
    cursor: pointer;
    user-select: none;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--muted);
    white-space: nowrap;
    transition: color var(--t);
    position: sticky;
    top: 0;
    z-index: 2;
  }
  th:hover { color: var(--text); }
  th.sorted-asc::after  { content: " ↑"; color: var(--accent); }
  th.sorted-desc::after { content: " ↓"; color: var(--accent); }

  tr:last-child td { border-bottom: none; }
  tr:hover td { background: var(--bg-row-hover); }

  tr.child-row td {
    background: var(--bg-subtle);
    border-bottom: 1px solid var(--border-subtle);
    font-size: 12px;
  }
  tr.child-row td:first-child { padding-left: 2.4rem; }
  tr.child-row:hover td { background: var(--bg-hover); }
  tr.child-loading td { color: var(--muted); font-size: 12px; padding-left: 2.4rem; }

  /* ─── STALE HIGHLIGHTING ────────────────────────────── */
  tr.stale td                    { background: rgba(250,144,5,0.08); }
  tr.stale td:first-child        { box-shadow: inset 3px 0 0 var(--warn); }
  tr.stale:hover td              { background: rgba(250,144,5,0.14); }
  tr.child-row.stale td          { background: rgba(250,144,5,0.10); }
  tr.child-row.stale td:first-child { box-shadow: inset 3px 0 0 var(--warn); }
  tr.child-row.stale:hover td    { background: rgba(250,144,5,0.18); }
  .stale-badge {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 999px;
    font-size: 10px;
    font-weight: 700;
    background: rgba(250,144,5,0.15);
    color: var(--warn);
    margin-left: 5px;
    vertical-align: middle;
    letter-spacing: 0.04em;
  }

  /* ─── STALE CALLOUT SECTION ─────────────────────── */
  .stale-callout {
    position: relative;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--r);
    box-shadow: var(--shadow-sm);
    margin-bottom: 14px;
    overflow: hidden;
  }
  .stale-callout::before {
    content: '';
    position: absolute;
    left: 0; top: 0; bottom: 0;
    width: 3px;
    background: var(--warn);
    border-radius: var(--r) 0 0 var(--r);
  }
  .stale-callout-header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 14px 10px 18px;
    cursor: pointer;
    user-select: none;
    transition: background var(--t);
  }
  .stale-callout-header:hover { background: var(--bg-subtle); }
  .stale-callout-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    background: var(--warn);
    flex-shrink: 0;
  }
  .stale-callout-title {
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: var(--warn);
    white-space: nowrap;
  }
  .stale-callout-count {
    font-family: 'Syne', sans-serif;
    font-size: 13px;
    font-weight: 700;
    color: var(--warn);
    background: rgba(250,144,5,0.12);
    padding: 1px 7px;
    border-radius: 999px;
    flex-shrink: 0;
  }
  .stale-callout-desc {
    font-size: 12px;
    color: var(--muted);
    white-space: nowrap;
  }
  .stale-callout-line { flex: 1; height: 1px; background: var(--border); }
  .stale-callout-icon {
    font-size: 13px;
    color: var(--muted);
    transition: transform var(--t);
    flex-shrink: 0;
  }
  .stale-callout-icon.collapsed { transform: rotate(-90deg); }
  .stale-callout-body { padding: 0 14px 8px 18px; }
  .stale-callout-body.hidden { display: none; }

  .stale-item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 5px 0;
    border-bottom: 1px solid var(--border-subtle);
  }
  .stale-item:last-child { border-bottom: none; }
  .stale-idle-tag {
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    font-weight: 500;
    color: var(--warn);
    background: rgba(250,144,5,0.12);
    padding: 2px 6px;
    border-radius: 4px;
    white-space: nowrap;
    flex-shrink: 0;
    min-width: 56px;
    text-align: center;
  }
  .stale-item-key {
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    color: var(--accent);
    text-decoration: none;
    flex-shrink: 0;
  }
  .stale-item-key:hover { text-decoration: underline; }
  .stale-item-summary {
    font-size: 12px;
    color: var(--text);
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .stale-parent-chip {
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    color: var(--text-secondary);
    background: var(--bg-subtle);
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    padding: 1px 6px;
    white-space: nowrap;
    flex-shrink: 0;
  }
  .stale-assignee {
    font-size: 11px;
    color: var(--text-secondary);
    white-space: nowrap;
    flex-shrink: 0;
  }

  a.key {
    color: var(--accent);
    text-decoration: none;
    font-family: 'DM Mono', monospace;
    font-size: 12px;
  }
  a.key:hover { text-decoration: underline; }

  a.link { color: var(--accent); text-decoration: none; font-size: 12px; }
  a.link:hover { text-decoration: underline; }

  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
  }
  .cat-new, .cat-undefined { background: var(--badge-todo-bg); color: var(--badge-todo-fg); }
  .cat-indeterminate       { background: var(--badge-inprog-bg); color: var(--badge-inprog-fg); }
  .cat-done                { background: var(--badge-done-bg); color: var(--badge-done-fg); }

  .chip {
    display: inline-block;
    padding: 1px 6px;
    margin: 1px 2px 1px 0;
    background: var(--bg-subtle);
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 4px;
    font-size: 11px;
  }

  .avatar {
    width: 18px; height: 18px;
    border-radius: 50%;
    vertical-align: middle;
    margin-right: 5px;
  }

  .expand-btn {
    background: none; border: none;
    padding: 0 4px 0 0;
    cursor: pointer;
    color: var(--muted);
    font-size: 8px;
    line-height: 1;
    vertical-align: middle;
    transition: transform var(--t), color var(--t);
    display: inline-block;
  }
  .expand-btn:hover { color: var(--accent); }
  .expand-btn.open  { transform: rotate(90deg); }

  .meta { color: var(--muted); font-size: 12px; }
  .err  { color: #cf222e; }

  /* ─── RIGHT PANEL ────────────────────────────── */
  .panel-overlay {
    position: fixed; inset: 0;
    background: rgba(0,0,0,0.38);
    display: none; z-index: 40;
    backdrop-filter: blur(2px);
  }
  .panel-overlay.open { display: block; }

  .panel {
    position: fixed;
    right: 0; top: 0; bottom: 0;
    width: var(--panel-w);
    max-width: 100vw;
    background: var(--bg-card);
    border-left: 1px solid var(--border);
    box-shadow: var(--shadow-lg);
    display: flex;
    flex-direction: column;
    transform: translateX(100%);
    transition: transform 0.22s cubic-bezier(0.4, 0, 0.2, 1);
    z-index: 50;
  }
  .panel.open { transform: translateX(0); }

  .panel-header {
    display: flex;
    align-items: center;
    padding: 14px 18px;
    border-bottom: 1px solid var(--border);
    gap: 8px;
    flex-shrink: 0;
  }

  .panel-title {
    font-family: 'Syne', sans-serif;
    font-weight: 700;
    font-size: 14px;
    flex: 1;
  }

  .panel-close {
    background: none;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    cursor: pointer;
    padding: 4px 9px;
    color: var(--muted);
    font: inherit; font-size: 12px;
    transition: all var(--t);
  }
  .panel-close:hover { background: var(--bg-hover); color: var(--text); }

  .panel-body {
    flex: 1;
    overflow-y: auto;
    padding: 14px 18px;
  }
  .panel-body::-webkit-scrollbar { width: 4px; }
  .panel-body::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

  .panel-section { margin-bottom: 20px; }

  .panel-section-title {
    font-size: 9.5px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border-subtle);
    margin-bottom: 10px;
  }

  .panel label {
    display: block;
    font-size: 10.5px;
    font-weight: 600;
    color: var(--muted);
    margin: 8px 0 3px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }

  .panel input[type=text],
  .panel input[type=password],
  .panel select,
  .panel textarea {
    width: 100%;
    padding: 7px 10px;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    font: inherit; font-size: 13px;
    background: var(--bg-input);
    color: var(--text);
    transition: border-color var(--t);
  }
  .panel input:focus, .panel textarea:focus, .panel select:focus { outline: none; border-color: var(--accent); }
  .panel textarea { min-height: 60px; resize: vertical; }

  .panel-row {
    display: flex;
    gap: 6px;
    align-items: center;
    flex-wrap: wrap;
  }
  .panel-row > input { flex: 1; min-width: 0; }

  .panel-btn {
    font: inherit; font-size: 12px;
    padding: 6px 10px;
    border: 1px solid var(--border); border-radius: var(--r-sm);
    background: none; color: var(--text-secondary);
    cursor: pointer;
    transition: all var(--t);
    white-space: nowrap; flex-shrink: 0;
  }
  .panel-btn:hover { background: var(--bg-hover); color: var(--text); }
  .panel-btn.danger { color: #cf222e; border-color: rgba(207,34,46,0.25); }
  .panel-btn.danger:hover { background: rgba(207,34,46,0.06); }

  .field-picker {
    border: 1px solid var(--border); border-radius: var(--r-sm);
    max-height: 200px; overflow-y: auto;
    padding: 4px;
    background: var(--bg-subtle);
  }
  .field-picker::-webkit-scrollbar { width: 3px; }
  .field-picker::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
  .field-picker label {
    display: flex; align-items: center; gap: 6px;
    padding: 4px 6px; margin: 0;
    font-size: 12px; font-weight: 400;
    color: var(--text); cursor: pointer;
    border-radius: 4px;
    transition: background var(--t);
    text-transform: none; letter-spacing: 0;
  }
  .field-picker label:hover { background: var(--bg-hover); }

  .selected-cols {
    display: flex; flex-wrap: wrap; gap: 4px; margin-top: 8px;
  }
  .selected-cols .chip { display: inline-flex; align-items: center; gap: 4px; }
  .selected-cols .chip button {
    padding: 0; border: none; background: transparent;
    cursor: pointer; color: var(--muted); font-size: 12px; line-height: 1;
    transition: color var(--t);
  }
  .selected-cols .chip button:hover { color: var(--blocked); }

  /* ─── TOAST ────────────────────────────────────── */
  .toast {
    position: fixed; bottom: 18px; right: 18px;
    padding: 10px 16px;
    background: var(--toast-bg); color: #fff;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: var(--r);
    font-size: 13px; z-index: 200;
    box-shadow: var(--shadow-lg);
    animation: toastin 0.18s ease;
  }
  .toast.err { background: #cf222e; border-color: #cf222e; }
  @keyframes toastin {
    from { opacity: 0; transform: translateY(6px); }
    to   { opacity: 1; transform: translateY(0); }
  }
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
</head>
<body>

<!-- ═══════════════════ LEFT SIDEBAR ═══════════════════════════ -->
<div class="sidebar" id="sidebar">
  <div class="sidebar-brand">
    <div class="brand-text">
      Mining Dashboard
      <small>Jira Analytics</small>
    </div>
  </div>
  <div class="sidebar-body">
    <div class="sidebar-section-label">Views</div>
    <div id="view-list"></div>
    <button class="sidebar-new-view" id="btn-new-view">+ New view</button>
  </div>
  <div class="sidebar-footer">
    <span class="sidebar-footer-meta">v{{ version }}</span>
  </div>
</div>

<!-- ═══════════════════ MAIN AREA ══════════════════════════════ -->
<div class="main" id="main">

  <!-- top bar -->
  <div class="topbar">
    <button class="topbar-menu" id="btn-sidebar-toggle" title="Toggle sidebar">☰</button>
    <span class="active-view-label" id="active-view-label"></span>
    <div class="grow"></div>
    <span class="status-chip" id="status">—</span>
    <button id="btn-refresh" class="topbar-btn">↺ Refresh</button>
    <button id="btn-theme" class="topbar-btn" title="Toggle dark mode">🌙</button>
    <button id="btn-settings" class="topbar-btn">⚙ Settings</button>
  </div>

  <!-- hero -->
  <div class="hero">
    <h1 class="hero-title"><span class="hero-icon">⛏</span> Mining <em>Dashboard</em></h1>
    <div class="hero-meta">
      <div class="hero-jira" id="jira-status-hero">
        <span class="status-dot" id="jira-dot"></span>
        <span id="jira-status-text">checking connection…</span>
      </div>
    </div>
  </div>

  <!-- high sev alert bar -->
  <div id="alert-bar" class="alert-bar hidden">
    <span class="alert-bar-label">⚠ High Sev</span>
    <div class="alert-items" id="alert-items"></div>
    <button class="alert-dismiss" id="btn-alert-dismiss" title="Dismiss">×</button>
  </div>


  <!-- scrollable content -->
  <div class="content">

    <!-- stale children callout -->
    <div class="stale-callout hidden" id="stale-callout">
      <div class="stale-callout-header" id="stale-callout-toggle">
        <span class="stale-callout-dot"></span>
        <span class="stale-callout-title">Stale Issues</span>
        <span class="stale-callout-count" id="stale-callout-count">0</span>
        <span class="stale-callout-desc">child issues without activity for 24h+</span>
        <span class="stale-callout-line"></span>
        <span class="stale-callout-icon" id="stale-callout-icon">▾</span>
      </div>
      <div class="stale-callout-body" id="stale-callout-body"></div>
    </div>

    <!-- charts collapsible -->
    <div class="charts-section" id="charts-section">
      <button class="charts-toggle" id="btn-charts-toggle">
        <span>Charts</span>
        <span class="charts-toggle-line"></span>
        <span class="charts-icon" id="charts-icon">▾</span>
      </button>
      <div class="charts" id="charts"></div>
    </div>

    <!-- kpis -->
    <div class="kpis" id="kpis"></div>

    <!-- toolbar -->
    <div class="toolbar card">
      <select id="mode-select" class="toolbar-mode">
        <option value="jql">JQL</option>
        <option value="keys">Issue keys</option>
      </select>
      <input type="text" id="query" class="toolbar-query" placeholder="JQL e.g. project = ADT ORDER BY updated DESC">
      <button id="btn-load" class="btn-primary">Load</button>
      <button id="btn-save-view" class="btn-ghost">Save view</button>
      <button id="btn-hide-done" class="btn-ghost">Hide Done</button>
    </div>

    <!-- table -->
    <div class="table-wrap card">
      <table id="tbl">
        <thead><tr id="head-row"></tr></thead>
        <tbody></tbody>
      </table>
    </div>

  </div>
</div>

<!-- ═══════════════════ RIGHT PANEL ════════════════════════════ -->
<div class="panel-overlay" id="panel-overlay"></div>
<aside class="panel" id="drawer">

  <div class="panel-header">
    <span class="panel-title">Settings</span>
    <button class="panel-close" id="btn-close-drawer">Close</button>
  </div>

  <div class="panel-body">

    <div class="panel-section">
      <div class="panel-section-title">Jira Connection</div>
      <div class="meta" id="jira-status">checking…</div>
    </div>

    <div class="panel-section">
      <div class="panel-section-title">Active View</div>
      <div class="panel-row">
        <input type="text" id="view-name" placeholder="view name">
        <button class="panel-btn" id="btn-rename-view">Rename</button>
        <button class="panel-btn danger" id="btn-delete-view">Delete</button>
      </div>
    </div>

    <div class="panel-section">
      <div class="panel-section-title">View Settings</div>
      <label>Refresh interval (seconds)</label>
      <input type="text" id="cfg-refresh" value="60">
      <label>Project key (for component picker)</label>
      <input type="text" id="cfg-project" placeholder="ADT">
    </div>

    <div class="panel-section">
      <div class="panel-section-title">Columns</div>
      <div class="panel-row" style="margin-bottom:8px">
        <input type="text" id="field-search" placeholder="search fields…">
        <button class="panel-btn" id="btn-reload-fields">Reload</button>
      </div>
      <div class="field-picker" id="field-picker">loading…</div>
      <label style="margin-top:10px">Selected columns</label>
      <div class="selected-cols" id="selected-cols"></div>
    </div>

  </div>
</aside>

<script>
const $ = sel => document.querySelector(sel);
const fmt = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const PROFILE_URL = "{{ profile_url }}";

const STATE = {
  config: null,
  fields: [],
  fieldById: new Map(),
  workingView: null,
  workingViewName: "default",
  protectedViews: new Set(),
  tickets: [],
  columns: [],
  jiraBase: "",
  sortKey: null,
  sortDir: 1,
  refreshTimer: null,
  hideDone: false,
};

function getVisibleTickets() {
  if (!STATE.hideDone) return STATE.tickets;
  return STATE.tickets.filter(t => (t.values?.status?.category ?? "") !== "done");
}

let alertBarDismissed = false;

function renderAlertBar() {
  if (alertBarDismissed) return;
  const HIGH_PRIO = new Set(["p0", "p1"]);
  const qualifying = getVisibleTickets().filter(t => {
    if ((t.values?.status?.category ?? "") === "done") return false;
    const prio = (t.values?.priority?.display || "").toLowerCase();
    const occ = typeof t.values?.occurrence_count?.sort === "number"
      ? t.values.occurrence_count.sort : 0;
    return HIGH_PRIO.has(prio) || occ > 2;
  });
  const bar = $("#alert-bar");
  if (!qualifying.length) { bar.classList.add("hidden"); return; }
  bar.classList.remove("hidden");
  const prio = t => (t.values?.priority?.display || "").toLowerCase();
  const occ = t => typeof t.values?.occurrence_count?.sort === "number"
    ? t.values.occurrence_count.sort : 0;
  qualifying.sort((a, b) => {
    const pa = prio(a), pb = prio(b);
    if (pa !== pb) {
      if (pa === "p0") return -1; if (pb === "p0") return 1;
      if (pa === "p1") return -1; if (pb === "p1") return 1;
    }
    return occ(b) - occ(a);
  });
  $("#alert-items").innerHTML = qualifying.map(t => {
    const p = prio(t);
    const prioDisplay = fmt(t.values?.priority?.display || "");
    const url = `${STATE.jiraBase}/browse/${encodeURIComponent(t.key)}`;
    const occVal = occ(t);
    const occBadge = occVal > 0 ? `<span class="alert-occ" title="Occurrences">${occVal}</span>` : "";
    const priorityBadge = prioDisplay
      ? `<span class="alert-priority-badge ${p}">${prioDisplay}</span>` : "";
    return `<span class="alert-item">
      ${priorityBadge}
      <a class="alert-key" href="${fmt(url)}" target="_blank" rel="noopener">${fmt(t.key)}</a>
      <span class="alert-summary" title="${fmt(t.summary)}">${fmt(t.summary)}</span>
      ${occBadge}
    </span>`;
  }).join("");
}

function toast(msg, err=false) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2400);
}

async function api(path, opts={}) {
  const r = await fetch(path, {
    headers: opts.body ? {"Content-Type": "application/json"} : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    method: opts.method || (opts.body ? "POST" : "GET"),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(data.error || r.statusText), {status: r.status, code: data.code});
  return data;
}

async function loadConfig() {
  STATE.config = await api("/api/config");
  STATE.workingViewName = STATE.config.active_view;
  STATE.workingView = JSON.parse(JSON.stringify(STATE.config.views[STATE.workingViewName]));
  STATE.protectedViews = new Set(STATE.config.protected_views || []);
  renderViewSelect();
  renderToolbar();
  renderDrawerForView();
}

function renderViewSelect() {
  const list = $("#view-list");
  list.innerHTML = Object.keys(STATE.config.views).map(n => {
    const locked = STATE.protectedViews.has(n) ? ' <span class="view-lock" title="Default view — cannot be deleted">🔒</span>' : '';
    return `<button class="view-item${n === STATE.workingViewName ? ' active' : ''}" data-view="${fmt(n)}">${fmt(n)}${locked}</button>`;
  }).join("");
  list.querySelectorAll(".view-item").forEach(btn => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });
  const lbl = $("#active-view-label");
  if (lbl) lbl.textContent = STATE.workingViewName;
}

function renderToolbar() {
  $("#mode-select").value = STATE.workingView.mode;
  $("#query").value = STATE.workingView.mode === "jql"
    ? STATE.workingView.jql
    : (STATE.workingView.keys || []).join(", ");
  $("#query").placeholder = STATE.workingView.mode === "jql"
    ? "JQL e.g. project = ADT ORDER BY updated DESC"
    : "ADT-201, KOM-101";
}

function renderDrawerForView() {
  $("#view-name").value = STATE.workingViewName;
  $("#cfg-refresh").value = STATE.workingView.refresh_seconds || 60;
  $("#cfg-project").value = STATE.workingView.project_key || "";
  const isProtected = STATE.protectedViews.has(STATE.workingViewName);
  const deleteBtn = $("#btn-delete-view");
  if (deleteBtn) {
    deleteBtn.style.display = isProtected ? "none" : "";
  }
  renderFieldPicker();
  renderSelectedCols();
}

async function loadFields() {
  try {
    STATE.fields = await api("/api/jira/fields");
  } catch (e) {
    if (e.code === "config") {
      $("#field-picker").innerHTML = '<div class="meta">configure Jira credentials first</div>';
      return;
    }
    $("#field-picker").innerHTML = `<div class="err">${fmt(e.message)}</div>`;
    return;
  }
  STATE.fieldById = new Map(STATE.fields.map(f => [f.id, f]));
  renderFieldPicker();
}

function renderFieldPicker() {
  const root = $("#field-picker");
  if (!STATE.fields.length) { root.innerHTML = '<div class="meta">no fields loaded — click Reload</div>'; return; }
  const q = ($("#field-search").value || "").toLowerCase();
  const selected = new Set(STATE.workingView.columns);

  const synthetic = [
    {id: "key", name: "Key", _synth: true},
    {id: "epic", name: "Epic (parent)", _synth: true},
  ];
  const all = [...synthetic, ...STATE.fields];

  const filtered = all.filter(f => {
    if (!q) return true;
    return (f.id + " " + f.name).toLowerCase().includes(q);
  });
  filtered.sort((a, b) => {
    const sa = selected.has(a.id) ? 0 : 1;
    const sb = selected.has(b.id) ? 0 : 1;
    if (sa !== sb) return sa - sb;
    return a.name.localeCompare(b.name);
  });

  root.innerHTML = filtered.slice(0, 200).map(f => {
    const checked = selected.has(f.id) ? "checked" : "";
    const tag = f._synth ? "synthetic" : (f.custom ? "custom" : "system");
    return `<label><input type="checkbox" data-fid="${fmt(f.id)}" ${checked}>
      <span>${fmt(f.name)}</span>
      <span class="meta" style="margin-left:auto; font-size: 0.7rem;">${tag} · ${fmt(f.id)}</span>
    </label>`;
  }).join("");

  root.querySelectorAll("input[type=checkbox]").forEach(cb => {
    cb.addEventListener("change", () => {
      const fid = cb.dataset.fid;
      if (cb.checked) {
        if (!STATE.workingView.columns.includes(fid)) STATE.workingView.columns.push(fid);
      } else {
        STATE.workingView.columns = STATE.workingView.columns.filter(c => c !== fid);
      }
      renderSelectedCols();
    });
  });
}

function renderSelectedCols() {
  $("#selected-cols").innerHTML = STATE.workingView.columns.map(c => {
    const f = STATE.fieldById.get(c);
    const label = c === "key" ? "Key" : c === "epic" ? "Epic" : (f?.name || c);
    return `<span class="chip">${fmt(label)} <button data-rm="${fmt(c)}" title="remove">×</button></span>`;
  }).join("");
  $("#selected-cols").querySelectorAll("button[data-rm]").forEach(b => {
    b.addEventListener("click", () => {
      STATE.workingView.columns = STATE.workingView.columns.filter(c => c !== b.dataset.rm);
      renderSelectedCols();
      renderFieldPicker();
    });
  });
}

function columnLabel(col) {
  if (col === "key") return "Key";
  if (col === "epic") return "Epic";
  if (col === "occurrence_count") return "Occurrences";
  const f = STATE.fieldById.get(col);
  return f?.name || col;
}

const CHART_DIMS = [
  {col: "status",     title: "By status",     extractor: v => v && v.display, colorBy: "category"},
  {col: "assignee",   title: "By assignee",   extractor: v => v && (v.display || "Unassigned")},
  {col: "priority",   title: "By priority",   extractor: v => v && v.display},
  {col: "components", title: "By component",  list: true},
  {col: "reporter",   title: "By reporter",   extractor: v => v && (v.display || "Unknown")},
];

const CATEGORY_COLOR = {
  done: "#1a7f37", indeterminate: "#0550ae", new: "#6e7781", undefined: "#6e7781",
};
const PALETTE = ["#0969da","#1a7f37","#bf8700","#cf222e","#8250df","#0550ae","#6e7781",
                 "#bc4c00","#1f883d","#e85d04","#0a3069","#a40e26","#3192aa"];

const CHART_INSTANCES = new Map();

const ACTION_STATUSES = [
  {match: "needs triage",          label: "Needs Triage",          cls: "triage"},
  {match: "blocked",               label: "Blocked",               cls: "blocked"},
  {match: "waiting for customer",  label: "Waiting for Customer",  cls: "waiting"},
  {match: "containment",           label: "Containment",           cls: "containment"},
  {match: "corrective action",     label: "Corrective Action",     cls: "corrective"},
];

function renderKpis() {
  const total = getVisibleTickets().length;
  let done = 0, inprog = 0, todo = 0, hasStatus = false;
  const actionCounts = new Map(ACTION_STATUSES.map(s => [s.match, 0]));

  for (const t of getVisibleTickets()) {
    const s = t.values?.status;
    if (!s || s.type === "empty") continue;
    hasStatus = true;
    const lower = (s.display || "").toLowerCase();
    if (s.category === "done") done++;
    else if (s.category === "indeterminate") inprog++;
    else todo++;
    for (const as of ACTION_STATUSES) {
      if (lower === as.match) actionCounts.set(as.match, actionCounts.get(as.match) + 1);
    }
  }

  const pct = n => total ? Math.round((n / total) * 100) + "%" : "—";
  const actionTiles = ACTION_STATUSES
    .map(as => ({ cls: as.cls, label: as.label, value: actionCounts.get(as.match), sub: pct(actionCounts.get(as.match)) }))
    .filter(t => t.value > 0);

  const staleCount = getVisibleTickets().filter(t => t.is_stale).length;

  const tiles = [
    {label: "Total", value: total, sub: "tickets"},
    staleCount > 0 && {cls: "stale", label: "Stale", value: staleCount, sub: ">24h inactive"},
    hasStatus && {cls: "done",   label: "Done",        value: done,   sub: pct(done)},
    hasStatus && {cls: "inprog", label: "In Progress",  value: inprog, sub: pct(inprog)},
    hasStatus && {cls: "todo",   label: "To Do",        value: todo,   sub: pct(todo)},
    ...actionTiles,
  ].filter(Boolean);

  $("#kpis").innerHTML = tiles.map(t =>
    `<div class="card kpi ${t.cls || ''}">
      <div class="kpi-label">${fmt(t.label)}</div>
      <div class="kpi-value">${fmt(t.value)}</div>
      <div class="kpi-sub">${fmt(t.sub)}</div>
    </div>`).join("");
}

function aggregateDim(dim) {
  const counts = new Map();
  const meta = new Map();
  for (const t of getVisibleTickets()) {
    const v = t.values?.[dim.col];
    if (!v || v.type === "empty") {
      if (dim.col === "assignee") {
        counts.set("Unassigned", (counts.get("Unassigned") || 0) + 1);
      }
      continue;
    }
    if (dim.list) {
      const items = v.items || [];
      if (!items.length) continue;
      for (const item of items) {
        const label = item.display || "—";
        counts.set(label, (counts.get(label) || 0) + 1);
      }
    } else {
      const label = dim.extractor(v) || "—";
      counts.set(label, (counts.get(label) || 0) + 1);
      if (dim.colorBy === "category" && v.category && !meta.has(label)) {
        meta.set(label, {category: v.category});
      }
    }
  }
  const entries = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  return {labels: entries.map(e => e[0]), data: entries.map(e => e[1]), meta};
}

function colorsFor(dim, agg) {
  if (dim.colorBy === "category") {
    return agg.labels.map(l => CATEGORY_COLOR[agg.meta.get(l)?.category] || PALETTE[0]);
  }
  return agg.labels.map((_, i) => PALETTE[i % PALETTE.length]);
}

function renderCharts() {
  const root = $("#charts");
  const present = CHART_DIMS.filter(d => STATE.columns.includes(d.col));
  if (!present.length) { root.innerHTML = ""; return; }

  for (const [, ch] of CHART_INSTANCES) ch.destroy();
  CHART_INSTANCES.clear();

  root.innerHTML = present.map(d =>
    `<div class="card chart-card" data-col="${fmt(d.col)}">
      <h3>${fmt(d.title)}</h3>
      <div class="canvas-wrap"><canvas></canvas></div>
      <div class="chart-legend"></div>
    </div>`).join("");

  for (const dim of present) {
    const agg = aggregateDim(dim);
    if (!agg.labels.length) continue;
    const cardEl = root.querySelector(`[data-col="${dim.col}"]`);
    const canvas = cardEl.querySelector("canvas");
    const colors = colorsFor(dim, agg);

    const chart = new Chart(canvas, {
      type: "doughnut",
      data: {
        labels: agg.labels,
        datasets: [{ data: agg.data, backgroundColor: colors, borderWidth: 1 }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: ctx => {
                const total = ctx.dataset.data.reduce((a, b) => a + b, 0);
                const pct = total ? Math.round((ctx.raw / total) * 100) : 0;
                return `${ctx.label}: ${ctx.raw} (${pct}%)`;
              }
            }
          }
        },
        cutout: "55%",
      },
    });
    CHART_INSTANCES.set(dim.col, chart);

    // Custom HTML legend — wraps freely, never truncates, click to toggle slice
    const legendEl = cardEl.querySelector(".chart-legend");
    legendEl.innerHTML = agg.labels.map((label, i) =>
      `<span class="chart-legend-item" data-idx="${i}">
        <span class="chart-legend-swatch" style="background:${colors[i]}"></span>
        ${fmt(label)}
      </span>`
    ).join("");
    legendEl.querySelectorAll(".chart-legend-item").forEach(item => {
      item.addEventListener("click", () => {
        const idx = parseInt(item.dataset.idx, 10);
        chart.toggleDataVisibility(idx);
        chart.update();
        item.classList.toggle("legend-hidden");
      });
    });
  }
}

async function loadTickets() {
  STATE.workingView.mode = $("#mode-select").value;
  const q = $("#query").value.trim();
  if (STATE.workingView.mode === "jql") {
    STATE.workingView.jql = q;
  } else {
    STATE.workingView.keys = q.split(/[,\s]+/).filter(Boolean);
  }

  const params = new URLSearchParams({
    mode: STATE.workingView.mode,
    columns: STATE.workingView.columns.join(","),
  });
  if (STATE.workingView.mode === "jql") params.set("jql", STATE.workingView.jql);
  else params.set("keys", STATE.workingView.keys.join(","));

  $("#status").textContent = "loading…";
  try {
    const data = await api("/api/tickets?" + params.toString());
    STATE.tickets = data.tickets;
    STATE.tickets.forEach(t => { if (t.is_epic) t.is_stale = false; });
    STATE.columns = data.columns;
    STATE.jiraBase = data.jira_base_url;
    CHILD_CACHE.clear();
    EXPANDED.clear();
    alertBarDismissed = false;
    renderKpis();
    renderCharts();
    renderTable();
    renderAlertBar();
    loadStaleChildren();
    $("#status").textContent = `${STATE.tickets.length} ticket(s) · ${new Date().toLocaleTimeString()}`;
    scheduleRefresh();
  } catch (e) {
    if (e.code === "config") {
      $("#status").innerHTML = `<span class="err">configure Jira credentials in Settings</span>`;
      openDrawer();
    } else {
      $("#status").innerHTML = `<span class="err">${fmt(e.message)}</span>`;
    }
  }
}

function idleLabel(updatedStr) {
  if (!updatedStr) return "—";
  const hrs = Math.round((Date.now() - new Date(updatedStr).getTime()) / 3600000);
  if (hrs < 24) return `${hrs}h idle`;
  const d = Math.floor(hrs / 24), h = hrs % 24;
  return h ? `${d}d ${h}h idle` : `${d}d idle`;
}

async function loadStaleChildren() {
  const params = new URLSearchParams({view: STATE.workingViewName});
  try {
    const data = await api("/api/stale-children?" + params.toString());
    renderStaleCallout(data.stale || [], data.jira_base_url || STATE.jiraBase);
  } catch (_) { /* non-critical */ }
}

function renderStaleCallout(items, jiraBase) {
  const el = $("#stale-callout");
  if (!items.length) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  $("#stale-callout-count").textContent = items.length;
  $("#stale-callout-body").innerHTML = items.map(item => {
    const url = `${jiraBase}/browse/${encodeURIComponent(item.key)}`;
    const prioClass = (item.priority || "").toLowerCase().replace(/\s+/g, "-");
    const prioBadge = item.priority
      ? `<span class="alert-priority-badge ${fmt(prioClass)}">${fmt(item.priority)}</span>`
      : "";
    const assignee = item.assignee
      ? `<span class="stale-assignee">↳ ${fmt(item.assignee)}</span>`
      : `<span class="stale-assignee" style="color:var(--muted)">↳ Unassigned</span>`;
    const parent = item.parent_key
      ? `<span class="stale-parent-chip" title="${fmt(item.parent_summary)}">${fmt(item.parent_key)}</span>`
      : "";
    return `<div class="stale-item">
      <span class="stale-idle-tag">${fmt(idleLabel(item.updated))}</span>
      ${prioBadge}
      <a class="stale-item-key" href="${fmt(url)}" target="_blank" rel="noopener">${fmt(item.key)}</a>
      <span class="stale-item-summary" title="${fmt(item.summary)}">${fmt(item.summary)}</span>
      ${assignee}
      ${parent}
    </div>`;
  }).join("");
}

function scheduleRefresh() {
  if (STATE.refreshTimer) clearInterval(STATE.refreshTimer);
  const secs = Math.max(5, parseInt(STATE.workingView.refresh_seconds, 10) || 60);
  STATE.refreshTimer = setInterval(loadTickets, secs * 1000);
}

const CHILD_CACHE = new Map();
const EXPANDED = new Set();

function renderTable() {
  const head = $("#head-row");
  head.innerHTML = STATE.columns.map(c =>
    `<th data-key="${fmt(c)}">${fmt(columnLabel(c))}</th>`).join("");
  head.querySelectorAll("th").forEach(th => {
    th.addEventListener("click", () => {
      const k = th.dataset.key;
      if (STATE.sortKey === k) STATE.sortDir = -STATE.sortDir;
      else { STATE.sortKey = k; STATE.sortDir = 1; }
      renderTable();
    });
    if (th.dataset.key === STATE.sortKey) {
      th.classList.add(STATE.sortDir > 0 ? "sorted-asc" : "sorted-desc");
    }
  });

  const rows = [...getVisibleTickets()];
  if (STATE.sortKey) {
    rows.sort((a, b) => {
      const av = sortValueFor(a, STATE.sortKey);
      const bv = sortValueFor(b, STATE.sortKey);
      if (typeof av === "number" && typeof bv === "number") return (av - bv) * STATE.sortDir;
      return String(av).localeCompare(String(bv), undefined, {numeric: true}) * STATE.sortDir;
    });
  }

  const tbody = $("#tbl tbody");
  tbody.innerHTML = "";
  for (const t of rows) {
    const tr = document.createElement("tr");
    tr.dataset.key = t.key;
    if (t.is_stale) tr.classList.add("stale");
    tr.innerHTML = STATE.columns.map(c => `<td>${renderCell(t, c, !!t.has_children)}</td>`).join("");
    tbody.appendChild(tr);

    if (EXPANDED.has(t.key)) {
      appendChildRows(tbody, t.key, STATE.columns);
    }
  }

  tbody.querySelectorAll(".expand-btn").forEach(btn => {
    btn.addEventListener("click", e => {
      e.preventDefault();
      const key = btn.dataset.key;
      if (EXPANDED.has(key)) {
        EXPANDED.delete(key);
        btn.classList.remove("open");
        tbody.querySelectorAll(`tr.child-row[data-parent="${CSS.escape(key)}"]`).forEach(r => r.remove());
      } else {
        EXPANDED.add(key);
        btn.classList.add("open");
        const parentRow = tbody.querySelector(`tr[data-key="${CSS.escape(key)}"]`);
        appendChildRows(tbody, key, STATE.columns, parentRow);
      }
    });
  });
}

function appendChildRows(tbody, key, columns, afterRow) {
  if (CHILD_CACHE.has(key)) {
    insertChildRows(tbody, key, columns, CHILD_CACHE.get(key), afterRow);
    return;
  }
  const loadingTr = document.createElement("tr");
  loadingTr.className = "child-loading";
  loadingTr.dataset.parent = key;
  loadingTr.innerHTML = `<td colspan="${columns.length}">Loading…</td>`;
  const ref = afterRow ? afterRow.nextSibling : null;
  tbody.insertBefore(loadingTr, ref);

  const params = new URLSearchParams({columns: columns.join(",")});
  fetch(`/api/children/${encodeURIComponent(key)}?${params}`)
    .then(r => r.json())
    .then(data => {
      const tickets = data.tickets || [];
      CHILD_CACHE.set(key, tickets);
      loadingTr.remove();
      if (EXPANDED.has(key)) {
        const parentRow = tbody.querySelector(`tr[data-key="${CSS.escape(key)}"]`);
        insertChildRows(tbody, key, columns, tickets, parentRow);
      }
    })
    .catch(() => { loadingTr.remove(); });
}

function insertChildRows(tbody, key, columns, tickets, afterRow) {
  const frag = document.createDocumentFragment();
  for (const t of tickets) {
    const tr = document.createElement("tr");
    tr.className = "child-row";
    if (t.is_stale) tr.classList.add("stale");
    tr.dataset.parent = key;
    tr.innerHTML = columns.map(c => `<td>${renderCell(t, c, false)}</td>`).join("");
    frag.appendChild(tr);
  }
  const ref = afterRow ? afterRow.nextSibling : null;
  tbody.insertBefore(frag, ref);
}

function sortValueFor(ticket, col) {
  if (col === "key") return ticket.key;
  const v = ticket.values?.[col];
  if (!v) return "";
  return v.sort ?? v.display ?? "";
}

function renderCell(ticket, col, expandable) {
  if (col === "key") {
    const url = `${STATE.jiraBase}/browse/${encodeURIComponent(ticket.key)}`;
    const toggle = expandable
      ? `<button class="expand-btn${EXPANDED.has(ticket.key) ? " open" : ""}" data-key="${fmt(ticket.key)}" title="Show child tickets">▶</button>`
      : `<span style="display:inline-block;width:1.1rem"></span>`;
    const staleBadge = ticket.is_stale ? `<span class="stale-badge">Stale</span>` : "";
    return toggle + `<a class="key" href="${fmt(url)}" target="_blank" rel="noopener">${fmt(ticket.key)}</a>` + staleBadge;
  }
  const v = ticket.values?.[col];
  if (!v || v.type === "empty") return "<span class='meta'>—</span>";
  return renderValue(v);
}

function renderValue(v) {
  switch (v.type) {
    case "status":
      return `<span class="badge cat-${fmt(v.category || 'undefined')}">${fmt(v.display)}</span>`;
    case "user":
      return (v.avatar ? `<img class="avatar" src="${fmt(v.avatar)}">` : "") + fmt(v.display);
    case "issue":
      return `<a class="link" href="${fmt(v.url)}" target="_blank" rel="noopener">${fmt(v.display)}</a>` +
             (v.summary ? ` <span class="meta">${fmt(v.summary)}</span>` : "");
    case "list":
      return (v.items || []).map(i => `<span class="chip">${renderValue(i)}</span>`).join("");
    case "date": {
      const d = new Date(v.sort);
      return isNaN(d) ? fmt(v.display) : `<span title="${fmt(v.sort)}">${fmt(d.toLocaleDateString(undefined, {year:"numeric",month:"short",day:"numeric"}))}</span>`;
    }
    case "option":
      return fmt(v.display);
    default:
      return fmt(v.display);
  }
}

async function saveCurrentView() {
  STATE.workingView.mode = $("#mode-select").value;
  const q = $("#query").value.trim();
  if (STATE.workingView.mode === "jql") STATE.workingView.jql = q;
  else STATE.workingView.keys = q.split(/[,\s]+/).filter(Boolean);
  STATE.workingView.refresh_seconds = parseInt($("#cfg-refresh").value, 10) || 60;
  STATE.workingView.project_key = $("#cfg-project").value.trim();

  await api(`/api/views/${encodeURIComponent(STATE.workingViewName)}`,
            {method: "PUT", body: STATE.workingView});
  toast(`Saved view "${STATE.workingViewName}"`);
  await loadConfig();
}

async function switchView(name) {
  await api("/api/active_view", {method: "POST", body: {name}});
  await loadConfig();
  await loadTickets();
}

async function newView() {
  const name = prompt("New view name:");
  if (!name) return;
  await api(`/api/views/${encodeURIComponent(name)}`, {method: "PUT", body: {
    mode: "jql", jql: "", keys: [], columns: ["key","summary","status","assignee"], refresh_seconds: 60,
  }});
  await api("/api/active_view", {method: "POST", body: {name}});
  await loadConfig();
}

async function renameView() {
  const newName = $("#view-name").value.trim();
  if (!newName || newName === STATE.workingViewName) return;
  await api(`/api/views/${encodeURIComponent(newName)}`,
            {method: "PUT", body: STATE.workingView});
  await api("/api/active_view", {method: "POST", body: {name: newName}});
  if (newName !== STATE.workingViewName) {
    await api(`/api/views/${encodeURIComponent(STATE.workingViewName)}`, {method: "DELETE"})
      .catch(() => {});
  }
  await loadConfig();
  toast(`Renamed to "${newName}"`);
}

async function deleteCurrentView() {
  if (STATE.protectedViews.has(STATE.workingViewName)) {
    toast("Default views cannot be deleted.", true);
    return;
  }
  if (!confirm(`Delete view "${STATE.workingViewName}"?`)) return;
  try {
    await api(`/api/views/${encodeURIComponent(STATE.workingViewName)}`, {method: "DELETE"});
    await loadConfig();
    await loadTickets();
  } catch (e) { toast(e.message, true); }
}

async function refreshJiraStatus() {
  try {
    const s = await api("/api/jira/status");
    if (s.ok) {
      $("#jira-status").innerHTML = `connected to <b>${fmt(s.base_url)}</b> as <b>${fmt(s.user)}</b>`;
      const dot = $("#jira-dot");
      if (dot) dot.className = "status-dot ok";
      const txt = $("#jira-status-text");
      if (txt) {
        const name = fmt(s.user);
        txt.innerHTML = PROFILE_URL
          ? `by <a class="hero-user-link" href="${fmt(PROFILE_URL)}" target="_blank" rel="noopener">${name}</a>`
          : `by ${name}`;
      }
    } else {
      $("#jira-status").innerHTML = `<span class="err">${fmt(s.error || "not connected")}</span>`;
      const dot = $("#jira-dot");
      if (dot) dot.className = "status-dot err";
      const txt = $("#jira-status-text");
      if (txt) txt.textContent = "not connected";
    }
  } catch (e) {
    $("#jira-status").innerHTML = `<span class="err">${fmt(e.message)}</span>`;
  }
}

function openDrawer()  { $("#drawer").classList.add("open"); $("#panel-overlay").classList.add("open"); }
function closeDrawer() { $("#drawer").classList.remove("open"); $("#panel-overlay").classList.remove("open"); }

// Sidebar collapse toggle
let sidebarCollapsed = false;
function toggleSidebar() {
  sidebarCollapsed = !sidebarCollapsed;
  $("#sidebar").classList.toggle("collapsed", sidebarCollapsed);
}

// Charts collapse toggle
let chartsCollapsed = false;
function toggleCharts() {
  chartsCollapsed = !chartsCollapsed;
  $("#charts").classList.toggle("hidden", chartsCollapsed);
  $("#charts-icon").classList.toggle("collapsed", chartsCollapsed);
}

// Stale callout collapse toggle
let staleCalloutCollapsed = false;
function toggleStaleCallout() {
  staleCalloutCollapsed = !staleCalloutCollapsed;
  $("#stale-callout-body").classList.toggle("hidden", staleCalloutCollapsed);
  $("#stale-callout-icon").classList.toggle("collapsed", staleCalloutCollapsed);
}

// Theme
function getTheme() {
  const stored = localStorage.getItem("theme");
  if (stored) return stored;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const icon = theme === "dark" ? "☀️" : "🌙";
  const title = theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
  $("#btn-theme").textContent = icon;
  $("#btn-theme").title = title;
}

function cycleTheme() {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  localStorage.setItem("theme", next);
  applyTheme(next);
}

$("#btn-settings").addEventListener("click", openDrawer);
$("#btn-close-drawer").addEventListener("click", closeDrawer);
$("#panel-overlay").addEventListener("click", closeDrawer);
$("#btn-load").addEventListener("click", loadTickets);
$("#btn-refresh").addEventListener("click", loadTickets);
$("#query").addEventListener("keydown", e => { if (e.key === "Enter") loadTickets(); });
$("#mode-select").addEventListener("change", () => { STATE.workingView.mode = $("#mode-select").value; renderToolbar(); });
$("#btn-save-view").addEventListener("click", saveCurrentView);
$("#btn-new-view").addEventListener("click", newView);
$("#btn-rename-view").addEventListener("click", renameView);
$("#btn-delete-view").addEventListener("click", deleteCurrentView);
$("#btn-reload-fields").addEventListener("click", loadFields);
$("#field-search").addEventListener("input", renderFieldPicker);
$("#btn-sidebar-toggle").addEventListener("click", toggleSidebar);
$("#btn-charts-toggle").addEventListener("click", toggleCharts);
$("#stale-callout-toggle").addEventListener("click", toggleStaleCallout);
$("#btn-alert-dismiss").addEventListener("click", () => {
  alertBarDismissed = true;
  $("#alert-bar").classList.add("hidden");
});
$("#btn-hide-done").addEventListener("click", () => {
  STATE.hideDone = !STATE.hideDone;
  const btn = $("#btn-hide-done");
  btn.classList.toggle("active", STATE.hideDone);
  btn.textContent = STATE.hideDone ? "Show Done" : "Hide Done";
  renderKpis();
  renderCharts();
  renderTable();
  renderAlertBar();
});
$("#btn-theme").addEventListener("click", cycleTheme);

applyTheme(getTheme());

(async function init() {
  await loadConfig();
  await refreshJiraStatus();
  await loadFields();
  if (STATE.workingView && (STATE.workingView.jql || (STATE.workingView.keys || []).length)) {
    await loadTickets();
  } else {
    $("#status").textContent = "configure a view to get started";
  }
})();
</script>
</body>
</html>
"""
