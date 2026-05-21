from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from typing import Any

APP_VERSION = "1.0.8"

CONFIG_FILENAME = "mining_dashboard.config.json"

DEFAULT_COLUMNS = [
    "key", "summary", "status", "assignee", "priority",
    "labels", "components", "parent",
    "occurrence_count", "created", "updated",
]

DEFAULT_JQL = (
    'project = VSTAB AND type = "Epic" AND component = "Off Road" '
    'ORDER BY status ASC'
)

DEFAULT_VIEW = {
    "mode": "jql",
    "jql": DEFAULT_JQL,
    "keys": [],
    "columns": DEFAULT_COLUMNS,
    "refresh_seconds": 60,
    "project_key": "VSTAB",
}

_OFFROAD_JQL = (
    'project = EC AND type = Bug AND "stack[checkboxes]" = mine_autonomy '
    'AND status NOT IN (Declined, "Unable to Reproduce", Resolved) '
    'ORDER BY priority DESC, created DESC'
)

SEED_VIEWS: dict[str, dict] = {
    "VSTAB Off Road Epics": dict(DEFAULT_VIEW),
    "Offroad Stack Issues": {
        **DEFAULT_VIEW,
        "jql": _OFFROAD_JQL,
        "project_key": "EC",
    },
}

VIEW_RENAMES: dict[str, str] = {
    "EC-11955 children": "Offroad Stack Issues",
    "VSTAB Off Road epics": "VSTAB Off Road Epics",
}

JQL_MIGRATIONS: dict[str, str] = {
    "parent = EC-11955 ORDER BY status ASC": _OFFROAD_JQL,
    "parent = EC-11955 AND issuetype = Bug ORDER BY status ASC": _OFFROAD_JQL,
    "parent = EC-11955 AND issuetype = Task ORDER BY status ASC": _OFFROAD_JQL,
    'parent in childIssuesOf("EC-11955") ORDER BY status ASC': _OFFROAD_JQL,
}

DEFAULT_CONFIG = {
    "active_view": "VSTAB Off Road Epics",
    "views": {k: dict(v) for k, v in SEED_VIEWS.items()},
}


class ConfigStore:
    def __init__(self, path: Path | None = None):
        self._path = path or Path(os.getcwd()) / CONFIG_FILENAME
        self._lock = RLock()
        self._data: dict[str, Any] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        with self._lock:
            if self._path.exists():
                try:
                    self._data = json.loads(self._path.read_text())
                except json.JSONDecodeError:
                    self._data = json.loads(json.dumps(DEFAULT_CONFIG))
            else:
                self._data = json.loads(json.dumps(DEFAULT_CONFIG))
            self._migrate()

    def _migrate(self) -> None:
        self._data.pop("jira", None)
        self._data.setdefault("views", {})
        for old, new in VIEW_RENAMES.items():
            if old in self._data["views"] and new not in self._data["views"]:
                self._data["views"][new] = self._data["views"].pop(old)
                if self._data.get("active_view") == old:
                    self._data["active_view"] = new
        freshly_seeded: set[str] = set()
        for name, seed in SEED_VIEWS.items():
            if name not in self._data["views"]:
                self._data["views"][name] = dict(seed)
                freshly_seeded.add(name)
        if not self._data["views"]:
            self._data["views"] = {k: dict(v) for k, v in DEFAULT_CONFIG["views"].items()}
            freshly_seeded.update(self._data["views"].keys())
        self._data.setdefault("active_view", next(iter(self._data["views"])))
        for v in self._data["views"].values():
            if v.get("jql") in JQL_MIGRATIONS:
                v["jql"] = JQL_MIGRATIONS[v["jql"]]
        for v_name, v in self._data["views"].items():
            for k, default in DEFAULT_VIEW.items():
                v.setdefault(k, default if not isinstance(default, list) else list(default))
            # Only top-up DEFAULT_COLUMNS for views that were just created, not saved ones
            if v_name in freshly_seeded:
                existing = v["columns"]
                for col in DEFAULT_COLUMNS:
                    if col not in existing:
                        existing.append(col)
        self.save()

    def save(self) -> None:
        with self._lock:
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            tmp.replace(self._path)

    def list_views(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._data["views"].items()}

    def get_view(self, name: str) -> dict | None:
        with self._lock:
            v = self._data["views"].get(name)
            return dict(v) if v else None

    def upsert_view(self, name: str, view: dict) -> None:
        cleaned = {**DEFAULT_VIEW, **{k: view[k] for k in DEFAULT_VIEW if k in view}}
        cleaned["columns"] = list(cleaned["columns"]) or DEFAULT_COLUMNS
        cleaned["keys"] = list(cleaned["keys"]) or []
        cleaned["refresh_seconds"] = int(cleaned["refresh_seconds"]) or 60
        with self._lock:
            self._data["views"][name] = cleaned
            self.save()

    def delete_view(self, name: str) -> bool:
        with self._lock:
            if name not in self._data["views"]:
                return False
            if name in SEED_VIEWS:
                return False
            if len(self._data["views"]) == 1:
                return False
            del self._data["views"][name]
            if self._data["active_view"] == name:
                self._data["active_view"] = next(iter(self._data["views"]))
            self.save()
            return True

    def get_active(self) -> str:
        with self._lock:
            return self._data["active_view"]

    def set_active(self, name: str) -> bool:
        with self._lock:
            if name not in self._data["views"]:
                return False
            self._data["active_view"] = name
            self.save()
            return True

    def public_dict(self) -> dict:
        with self._lock:
            return {
                "active_view": self._data["active_view"],
                "views": {k: dict(v) for k, v in self._data["views"].items()},
                "protected_views": list(SEED_VIEWS.keys()),
            }
