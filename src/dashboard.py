from __future__ import annotations

import os
from threading import RLock

from flask import Flask, jsonify, render_template_string, request

from .config import ConfigStore
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
    app = Flask(__name__)
    holder = ClientHolder()

    @app.errorhandler(JiraConfigError)
    def _config_err(exc):
        return jsonify({"error": str(exc), "code": "config"}), 412

    @app.errorhandler(ValueError)
    def _value_err(exc):
        return jsonify({"error": str(exc), "code": "bad_request"}), 400

    @app.get("/")
    def index():
        return render_template_string(DASHBOARD_HTML)

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
            issues = client.search_issues(jql, request_fields)
            tickets = [_shape_issue(i["key"], i.get("fields", {}), column_list, client.base_url)
                       for i in issues]
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
        return jsonify({"jira_base_url": client.base_url, "columns": column_list, "tickets": tickets})

    return app


SPECIAL_COLUMNS = {"key"}
COLUMN_TO_FIELD = {"epic": "parent"}


def _expand_request_fields(columns: list[str]) -> list[str]:
    fields: set[str] = set()
    for c in columns:
        if c in SPECIAL_COLUMNS:
            continue
        fields.add(COLUMN_TO_FIELD.get(c, c))
    fields.add("summary")
    return sorted(fields)


def _shape_issue(key: str, fields: dict, columns: list[str], base_url: str) -> dict:
    values: dict = {}
    for col in columns:
        if col == "key":
            continue
        field_id = COLUMN_TO_FIELD.get(col, col)
        values[col] = _render_field(field_id, fields.get(field_id), base_url)
    return {"key": key, "summary": fields.get("summary") or "", "values": values}


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
        items = [_render_field(field_id, x, base_url) for x in raw]
        labels = [i.get("display") for i in items if i.get("display")]
        return {"type": "list", "items": items, "display": ", ".join(labels), "sort": ", ".join(labels)}

    if isinstance(raw, bool):
        s = "yes" if raw else "no"
        return {"type": "bool", "display": s, "sort": s}

    if isinstance(raw, (int, float)):
        return {"type": "number", "display": str(raw), "sort": raw}

    if isinstance(raw, str):
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
<style>
  :root {
    color-scheme: light;
    --bg: #f6f7f9; --bg-card: #fff; --bg-subtle: #f6f8fa; --bg-hover: #f0f3f6;
    --bg-row-hover: #fafbfc; --bg-input: #fff; --bg-chip: #eaeef2;
    --text: #1f2328; --text-chip: #424a53;
    --border: #d0d7de; --border-subtle: #eaeef2;
    --muted: #59636e; --accent: #0969da;
    --done: #1a7f37; --inprog: #0550ae; --todo: #6e7781; --warn: #bf8700;
    --badge-todo-bg: #eaeef2; --badge-todo-fg: #424a53;
    --badge-inprog-bg: #ddf4ff; --badge-inprog-fg: #0550ae;
    --badge-done-bg: #dafbe1; --badge-done-fg: #1a7f37;
    --toast-bg: #1f2328;
  }
  [data-theme="dark"] {
    color-scheme: dark;
    --bg: #0d1117; --bg-card: #161b22; --bg-subtle: #1c2128; --bg-hover: #1c2128;
    --bg-row-hover: #1c2128; --bg-input: #0d1117; --bg-chip: #2d333b;
    --text: #e6edf3; --text-chip: #adbac7;
    --border: #30363d; --border-subtle: #21262d;
    --muted: #8b949e; --accent: #58a6ff;
    --done: #3fb950; --inprog: #58a6ff; --todo: #8b949e; --warn: #d29922;
    --badge-todo-bg: #2d333b; --badge-todo-fg: #adbac7;
    --badge-inprog-bg: #0c2d6b; --badge-inprog-fg: #58a6ff;
    --badge-done-bg: #0d3b1e; --badge-done-fg: #3fb950;
    --toast-bg: #2d333b;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --bg: #0d1117; --bg-card: #161b22; --bg-subtle: #1c2128; --bg-hover: #1c2128;
      --bg-row-hover: #1c2128; --bg-input: #0d1117; --bg-chip: #2d333b;
      --text: #e6edf3; --text-chip: #adbac7;
      --border: #30363d; --border-subtle: #21262d;
      --muted: #8b949e; --accent: #58a6ff;
      --done: #3fb950; --inprog: #58a6ff; --todo: #8b949e; --warn: #d29922;
      --badge-todo-bg: #2d333b; --badge-todo-fg: #adbac7;
      --badge-inprog-bg: #0c2d6b; --badge-inprog-fg: #58a6ff;
      --badge-done-bg: #0d3b1e; --badge-done-fg: #3fb950;
      --toast-bg: #2d333b;
    }
  }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         margin: 0; background: var(--bg); color: var(--text); }
  .card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px;
          box-shadow: 0 1px 2px rgba(0,0,0,0.03); }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
          gap: 0.75rem; margin-bottom: 1rem; }
  .kpi { padding: 0.85rem 1rem; }
  .kpi .label { color: var(--muted); font-size: 0.75rem; text-transform: uppercase;
                letter-spacing: 0.04em; }
  .kpi .value { font-size: 1.6rem; font-weight: 600; margin-top: 0.2rem; }
  .kpi .sub { color: var(--muted); font-size: 0.78rem; margin-top: 0.2rem; }
  .kpi.done .value  { color: var(--done); }
  .kpi.inprog .value { color: var(--inprog); }
  .kpi.todo .value  { color: var(--todo); }
  .charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 0.75rem; margin-bottom: 1rem; }
  .chart-card { padding: 0.75rem 0.9rem; }
  .chart-card h3 { margin: 0 0 0.5rem; font-size: 0.78rem; text-transform: uppercase;
                   letter-spacing: 0.04em; color: var(--muted); }
  .chart-card .canvas-wrap { position: relative; height: 200px; }
  .chart-card.empty { color: var(--muted); font-size: 0.85rem; padding: 1rem; }
  header { display: flex; gap: 0.75rem; padding: 0.75rem 1rem; border-bottom: 1px solid var(--border);
           background: var(--bg-card); align-items: center; flex-wrap: wrap; }
  header h1 { margin: 0; font-size: 1rem; flex-shrink: 0; }
  .grow { flex: 1; }
  button, select, input[type=text], input[type=password] {
    font: inherit; padding: 0.4rem 0.6rem; border: 1px solid var(--border);
    border-radius: 6px; background: var(--bg-input); color: var(--text);
  }
  button { cursor: pointer; }
  button:hover { background: var(--bg-hover); }
  button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  button.primary:hover { opacity: 0.88; }
  button.danger { color: #cf222e; }
  main { padding: 1rem; }
  .meta { color: var(--muted); font-size: 0.85rem; }
  .err { color: #cf222e; }
  .toolbar { display: flex; gap: 0.5rem; margin-bottom: 0.75rem; align-items: center; flex-wrap: wrap; }
  .toolbar input[type=text] { flex: 1; min-width: 320px; }
  table { width: 100%; border-collapse: collapse; background: var(--bg-card);
          border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  table { box-shadow: 0 1px 2px rgba(0,0,0,0.03); }
  th, td { text-align: left; padding: 0.55rem 0.85rem; border-bottom: 1px solid var(--border-subtle);
           font-size: 0.875rem; vertical-align: top; }
  th { background: var(--bg-subtle); cursor: pointer; user-select: none; font-weight: 600;
       white-space: nowrap; }
  tr:hover td { background: var(--bg-row-hover); }
  th.sorted-asc::after  { content: " ▲"; }
  th.sorted-desc::after { content: " ▼"; }
  tr:last-child td { border-bottom: none; }
  a.key, a.link { color: var(--accent); text-decoration: none; font-family: ui-monospace, SFMono-Regular, monospace; }
  a.key:hover, a.link:hover { text-decoration: underline; }
  .badge { display: inline-block; padding: 0.15rem 0.55rem; border-radius: 999px;
           font-size: 0.75rem; font-weight: 600; }
  .cat-new, .cat-undefined { background: var(--badge-todo-bg); color: var(--badge-todo-fg); }
  .cat-indeterminate       { background: var(--badge-inprog-bg); color: var(--badge-inprog-fg); }
  .cat-done                { background: var(--badge-done-bg); color: var(--badge-done-fg); }
  .chip { display: inline-block; padding: 0.1rem 0.45rem; margin: 0 0.15rem 0.15rem 0;
          background: var(--bg-chip); color: var(--text); border-radius: 4px; font-size: 0.75rem; }
  .avatar { width: 18px; height: 18px; border-radius: 50%; vertical-align: middle; margin-right: 0.3rem; }

  /* drawer */
  .drawer-bg { position: fixed; inset: 0; background: rgba(0,0,0,0.5); display: none; z-index: 100; }
  .drawer-bg.open { display: block; }
  .drawer { position: fixed; right: 0; top: 0; bottom: 0; width: 480px; max-width: 100vw;
            background: var(--bg-card); border-left: 1px solid var(--border); padding: 1rem; overflow-y: auto;
            transform: translateX(100%); transition: transform 0.18s; z-index: 101; }
  .drawer.open { transform: translateX(0); }
  .drawer h2 { margin: 0 0 0.75rem; font-size: 1.05rem; }
  .drawer h3 { margin: 1.25rem 0 0.5rem; font-size: 0.9rem; text-transform: uppercase;
               letter-spacing: 0.04em; color: var(--muted); }
  .drawer label { display: block; font-size: 0.8rem; margin: 0.5rem 0 0.2rem; color: var(--muted); }
  .drawer input[type=text], .drawer input[type=password], .drawer select, .drawer textarea {
    width: 100%; padding: 0.4rem 0.6rem; border: 1px solid var(--border); border-radius: 6px;
    font: inherit; background: var(--bg-input); color: var(--text);
  }
  .drawer textarea { min-height: 64px; resize: vertical; }
  .row { display: flex; gap: 0.5rem; align-items: center; }
  .row > * { flex-shrink: 0; }
  .row .grow { flex: 1; }
  .field-picker { border: 1px solid var(--border); border-radius: 6px;
                  max-height: 240px; overflow-y: auto; padding: 0.4rem; }
  .field-picker label { display: flex; align-items: center; gap: 0.4rem; padding: 0.15rem 0.2rem;
                        margin: 0; font-size: 0.85rem; color: var(--text); cursor: pointer; }
  .field-picker label:hover { background: var(--bg-subtle); }
  .selected-cols { display: flex; flex-wrap: wrap; gap: 0.25rem; margin-top: 0.4rem; }
  .selected-cols .chip { display: inline-flex; align-items: center; gap: 0.3rem; }
  .selected-cols .chip button { padding: 0; border: none; background: transparent; cursor: pointer; color: var(--muted); }
  .toast { position: fixed; bottom: 1rem; right: 1rem; padding: 0.55rem 0.85rem;
           background: var(--toast-bg); color: var(--text); border: 1px solid var(--border);
           border-radius: 6px; font-size: 0.85rem; z-index: 200; }
  .toast.err { background: #cf222e; color: #fff; border-color: #cf222e; }
  #btn-theme { font-size: 1rem; padding: 0.3rem 0.5rem; line-height: 1; }
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
</head>
<body>

<header>
  <h1>Mining Dashboard</h1>
  <select id="view-select" title="Active view"></select>
  <button id="btn-save-view" title="Save current edits to active view">Save view</button>
  <span class="grow"></span>
  <span class="meta" id="status">—</span>
  <button id="btn-refresh">Refresh</button>
  <button id="btn-theme" title="Toggle dark mode">🌙</button>
  <button id="btn-settings">Settings</button>
</header>

<main>
  <div class="toolbar">
    <select id="mode-select">
      <option value="jql">JQL</option>
      <option value="keys">Issue keys</option>
    </select>
    <input type="text" id="query" placeholder="JQL or comma-separated keys">
    <button id="btn-load" class="primary">Load</button>
  </div>
  <div class="kpis" id="kpis"></div>
  <div class="charts" id="charts"></div>
  <table id="tbl" class="card">
    <thead><tr id="head-row"></tr></thead>
    <tbody></tbody>
  </table>
</main>

<div class="drawer-bg" id="drawer-bg"></div>
<aside class="drawer" id="drawer">
  <div class="row"><h2 class="grow">Settings</h2><button id="btn-close-drawer">Close</button></div>

  <h3>Jira connection</h3>
  <div class="meta" id="jira-status">checking…</div>
  <div class="meta" style="margin-top: 0.4rem;">
    Credentials are set as environment variables (JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN)
    via Secret Manager. Contact your admin to update them.
  </div>

  <h3>Active view</h3>
  <div class="row">
    <input type="text" id="view-name" placeholder="view name" class="grow">
    <button id="btn-rename-view">Rename</button>
    <button id="btn-new-view">New</button>
    <button id="btn-delete-view" class="danger">Delete</button>
  </div>

  <label>Refresh interval (seconds)</label>
  <input type="text" id="cfg-refresh" value="60">

  <label>Project key (for component picker, optional)</label>
  <input type="text" id="cfg-project" placeholder="ADT">

  <h3>Columns</h3>
  <div class="row" style="margin-bottom: 0.4rem;">
    <input type="text" id="field-search" placeholder="search fields…" class="grow">
    <button id="btn-reload-fields">Reload</button>
  </div>
  <div class="field-picker" id="field-picker">loading…</div>
  <label>Selected (drag to reorder is not supported; use × to remove)</label>
  <div class="selected-cols" id="selected-cols"></div>
</aside>

<script>
const $ = sel => document.querySelector(sel);
const fmt = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

const STATE = {
  config: null,
  fields: [],
  fieldById: new Map(),
  workingView: null,
  workingViewName: "default",
  tickets: [],
  columns: [],
  jiraBase: "",
  sortKey: null,
  sortDir: 1,
  refreshTimer: null,
};

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
  renderViewSelect();
  renderToolbar();
  renderDrawerForView();
}

function renderViewSelect() {
  const sel = $("#view-select");
  sel.innerHTML = Object.keys(STATE.config.views)
    .map(n => `<option value="${fmt(n)}" ${n===STATE.workingViewName?"selected":""}>${fmt(n)}</option>`).join("");
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
  const f = STATE.fieldById.get(col);
  return f?.name || col;
}

const CHART_DIMS = [
  {col: "status",     title: "By status",     extractor: v => v && v.display, colorBy: "category"},
  {col: "assignee",   title: "By assignee",   extractor: v => v && (v.display || "Unassigned")},
  {col: "priority",   title: "By priority",   extractor: v => v && v.display},
  {col: "components", title: "By component",  list: true},
  {col: "labels",     title: "By label",      list: true},
];

const CATEGORY_COLOR = {
  done: "#1a7f37", indeterminate: "#0550ae", new: "#6e7781", undefined: "#6e7781",
};
const PALETTE = ["#0969da","#1a7f37","#bf8700","#cf222e","#8250df","#0550ae","#6e7781",
                 "#bc4c00","#1f883d","#e85d04","#0a3069","#a40e26","#3192aa"];

const CHART_INSTANCES = new Map();

function renderKpis() {
  const total = STATE.tickets.length;
  let done = 0, inprog = 0, todo = 0, hasStatus = false;
  for (const t of STATE.tickets) {
    const s = t.values?.status;
    if (!s) continue;
    hasStatus = true;
    if (s.category === "done") done++;
    else if (s.category === "indeterminate") inprog++;
    else todo++;
  }
  const pct = n => total ? Math.round((n / total) * 100) + "%" : "—";
  const tiles = [
    {label: "Total tickets", value: total, sub: ""},
    hasStatus && {cls: "done",   label: "Done",        value: done,   sub: pct(done)},
    hasStatus && {cls: "inprog", label: "In progress", value: inprog, sub: pct(inprog)},
    hasStatus && {cls: "todo",   label: "To do",       value: todo,   sub: pct(todo)},
  ].filter(Boolean);
  $("#kpis").innerHTML = tiles.map(t =>
    `<div class="card kpi ${t.cls || ''}">
      <div class="label">${fmt(t.label)}</div>
      <div class="value">${fmt(t.value)}</div>
      <div class="sub">${fmt(t.sub)}</div>
    </div>`).join("");
}

function aggregateDim(dim) {
  const counts = new Map();
  const meta = new Map();
  for (const t of STATE.tickets) {
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
    </div>`).join("");

  for (const dim of present) {
    const agg = aggregateDim(dim);
    if (!agg.labels.length) continue;
    const canvas = root.querySelector(`[data-col="${dim.col}"] canvas`);
    const chart = new Chart(canvas, {
      type: "doughnut",
      data: {
        labels: agg.labels,
        datasets: [{ data: agg.data, backgroundColor: colorsFor(dim, agg), borderWidth: 1 }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { position: "right", labels: { boxWidth: 10, font: { size: 11 } } },
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
    STATE.columns = data.columns;
    STATE.jiraBase = data.jira_base_url;
    renderKpis();
    renderCharts();
    renderTable();
    $("#status").textContent = `${STATE.tickets.length} ticket(s) — ${new Date().toLocaleTimeString()}`;
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

function scheduleRefresh() {
  if (STATE.refreshTimer) clearInterval(STATE.refreshTimer);
  const secs = Math.max(5, parseInt(STATE.workingView.refresh_seconds, 10) || 60);
  STATE.refreshTimer = setInterval(loadTickets, secs * 1000);
}

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

  const rows = [...STATE.tickets];
  if (STATE.sortKey) {
    rows.sort((a, b) => {
      const av = sortValueFor(a, STATE.sortKey);
      const bv = sortValueFor(b, STATE.sortKey);
      if (typeof av === "number" && typeof bv === "number") return (av - bv) * STATE.sortDir;
      return String(av).localeCompare(String(bv), undefined, {numeric: true}) * STATE.sortDir;
    });
  }

  const tbody = $("#tbl tbody");
  tbody.innerHTML = rows.map(t => {
    const cells = STATE.columns.map(c => `<td>${renderCell(t, c)}</td>`).join("");
    return `<tr>${cells}</tr>`;
  }).join("");
}

function sortValueFor(ticket, col) {
  if (col === "key") return ticket.key;
  const v = ticket.values?.[col];
  if (!v) return "";
  return v.sort ?? v.display ?? "";
}

function renderCell(ticket, col) {
  if (col === "key") {
    const url = `${STATE.jiraBase}/browse/${encodeURIComponent(ticket.key)}`;
    return `<a class="key" href="${fmt(url)}" target="_blank" rel="noopener">${fmt(ticket.key)}</a>`;
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
      $("#jira-status").innerHTML =
        `connected to <b>${fmt(s.base_url)}</b> as <b>${fmt(s.user)}</b>`;
    } else {
      $("#jira-status").innerHTML =
        `<span class="err">${fmt(s.error || "not connected")}</span>`;
    }
  } catch (e) {
    $("#jira-status").innerHTML = `<span class="err">${fmt(e.message)}</span>`;
  }
}

function openDrawer() { $("#drawer").classList.add("open"); $("#drawer-bg").classList.add("open"); }
function closeDrawer() { $("#drawer").classList.remove("open"); $("#drawer-bg").classList.remove("open"); }

$("#btn-settings").addEventListener("click", openDrawer);
$("#btn-close-drawer").addEventListener("click", closeDrawer);
$("#drawer-bg").addEventListener("click", closeDrawer);
$("#btn-load").addEventListener("click", loadTickets);
$("#btn-refresh").addEventListener("click", loadTickets);
$("#query").addEventListener("keydown", e => { if (e.key === "Enter") loadTickets(); });
$("#mode-select").addEventListener("change", () => { STATE.workingView.mode = $("#mode-select").value; renderToolbar(); });
$("#view-select").addEventListener("change", e => switchView(e.target.value));
$("#btn-save-view").addEventListener("click", saveCurrentView);
$("#btn-new-view").addEventListener("click", newView);
$("#btn-rename-view").addEventListener("click", renameView);
$("#btn-delete-view").addEventListener("click", deleteCurrentView);
$("#btn-reload-fields").addEventListener("click", loadFields);
$("#field-search").addEventListener("input", renderFieldPicker);

// ---------- theme ----------

function getTheme() {
  const stored = localStorage.getItem("theme");
  if (stored) return stored;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  $("#btn-theme").textContent = theme === "dark" ? "☀️" : "🌙";
  $("#btn-theme").title = theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
}

$("#btn-theme").addEventListener("click", () => {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  localStorage.setItem("theme", next);
  applyTheme(next);
});

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
