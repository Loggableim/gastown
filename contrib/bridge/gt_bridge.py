#!/usr/bin/env python3
"""gt-bridge -- kleine Bruecke zwischen Gas Town und externen Bots.

Standardbibliothek-only, Python 3.10. Zwei Aufgaben:

(a) Admin-API auf 127.0.0.1:3100 (Bearer-Auth, Scope pro Request).
    Caddy routet agents.lastbrowser.com/api/bridge/* hierher. Die Pfade
    beginnen daher mit /api/bridge/.

(b) Outbound-Events: Poller-Thread liest ~/gt/.events.jsonl (oder Polling-
    Fallback) und schreibt erkannte Ereignisse als JSONL in die Outbox
    (~/gt/logs/bridge-outbox.jsonl, per GET /api/bridge/outbox lesbar). Ohne
    konfigurierte Slack-Zugangsdaten wird nur ins Log geschrieben (dry-run).
    Mails an den Overseer werden immer in die Outbox geschrieben.

Alle gt/bd-Unterbefehle und Flags stehen zentral in COMMANDS, damit sie am
Server an die echte CLI angepasst werden koennen.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import string
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

VERSION = "0.1.0"
PREFIX = "/api/bridge"
SERVICE_NAME = "gt-bridge"

LOG = logging.getLogger(SERVICE_NAME)

# Eingabevalidierung: rig/agent/bead-IDs.
ID_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
MAX_ID_LEN = 512
MAX_TEXT_LEN = 20000
MAX_BODY_BYTES = 1000000

# ---------------------------------------------------------------------------
# Zentrale Befehlstabelle -- HIER an die echte CLI anpassen.
#
# Werte sind argv-Listen. "$platzhalter" wird aus bereits validierten Einzel-
# werten ersetzt (string.Template). Es wird NIE eine Shell benutzt.
# ---------------------------------------------------------------------------
COMMANDS: Dict[str, Any] = {
    # Basis-Binaerdateien (kann per GT_BIN/BD_BIN ueberschrieben werden).
    "gt": "gt",
    "bd": "bd",

    # Lesende Befehle.
    "status": ["gt", "status", "--json"],
    "status_text": ["gt", "status"],          # Fallback ohne --json
    "ready": ["gt", "ready", "--json"],
    "ready_rig_flag": "--rig",
    "convoy_list": ["gt", "convoy", "list", "--json"],
    # gt escalate list ist am Server ggf. anders benannt -> konfigurierbar.
    "escalations": ["gt", "escalate", "list", "--json"],

    # Schreibende Befehle.
    "mail_send": ["gt", "mail", "send", "$to", "-s", "$subject", "-m", "$body"],
    "nudge": ["gt", "nudge", "$target", "--", "$message"],
    # gt nudge kennt --mode mit wait-idle (Default), queue und immediate.
    "nudge_mode_flag": "--mode",
    "sling": ["gt", "sling", "$bead", "$rig"],
    "bd_create": [
        "bd", "create", "--json",
        "--title=$title",
        "--description=$description",
    ],
    "bd_priority_template": "--priority=$priority",
}

# Erlaubte Zustellmodi fuer gt nudge (vgl. `gt nudge --mode`).
NUDGE_MODES = {"wait-idle", "queue", "immediate"}
DEFAULT_NUDGE_MODE = "wait-idle"

# Whitelist rig-bezogener Top-Level-Schluessel fuer Nicht-Admin-Antworten.
# Nur diese Felder werden aus den jeweiligen CLI-Antworten durchgelassen.
STATUS_RIG_KEYS: Tuple[str, ...] = ("rigs", "agents")
CONVOY_RIG_KEYS: Tuple[str, ...] = ("convoys",)
ESCALATION_RIG_KEYS: Tuple[str, ...] = ("escalations",)

# Mapping gt-Event-Typ (events.go) -> Bruecken-Event-Typ.
# Konfigurierbar/erweiterbar, wenn neue Event-Typen dazukommen.
EVENT_TYPE_MAP: Dict[str, str] = {
    "pr_created": "pr_created",
    "pull_request_created": "pr_created",
    "merged": "pr_merged",
    "merge_completed": "pr_merged",
    "done": "bead_done",
    "escalation_sent": "escalation",
    "escalation": "escalation",
    "agent_stuck": "agent_stuck",
    "stuck": "agent_stuck",
    "session_death": "agent_dead",
    "mass_death": "agent_dead",
    "agent_dead": "agent_dead",
}

EVENT_EMOJI: Dict[str, str] = {
    "pr_created": "\U0001F195",   # 🆕
    "pr_merged": "\u2705",        # ✅
    "escalation": "\U0001F6A8",   # 🚨
    "agent_stuck": "\U0001F7E0",  # 🟠
    "agent_dead": "\u26AB",       # ⚫
    "bead_done": "\U0001F3C1",    # 🏁
    "overseer_mail": "\u2709",    # ✉
}

# Eskalationen / tote Agenten werden zusaetzlich an die Town erinnert.
TOWN_ALERT_TYPES = {"escalation", "agent_dead", "agent_stuck"}
TOWN_ALERT_PREFIX = "FUER-AMI"

# Empfaenger, die als "Overseer"/Mensch gelten: solche Mails werden immer in
# die Outbox geschrieben, damit Bots sie lesen koennen (Cid/Ami-Anforderung).
OVERSEER_MAIL_RECIPIENTS = {"overseer", "human"}

DEFAULT_ENV_FILE = "/home/gastown/.config/gt-bridge/bridge.env"
DEFAULT_GT_ROOT = "/home/gastown/gt"


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _strip_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def load_env_file(path: str) -> Dict[str, str]:
    """Liest eine K=V-Env-Datei. Kommentare/Leerzeilen werden ignoriert."""
    data: Dict[str, str] = {}
    if not path or not os.path.exists(path):
        return data
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                data[key.strip()] = _strip_quotes(val)
    except OSError as exc:  # pragma: no cover - defensive
        LOG.warning("Env-Datei %s nicht lesbar: %s", path, exc)
    return data


def _env_int(value: Optional[str], default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _maybe_json(text: str) -> Any:
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        return None


class ValidationError(ValueError):
    pass


class CommandError(RuntimeError):
    pass


def validate_id(value: Any, field_name: str) -> str:
    """Validiert rig/agent/bead/address-IDs gegen [A-Za-z0-9._/-]+."""
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} muss ein String sein")
    v = value.strip()
    if not v:
        raise ValidationError(f"{field_name} darf nicht leer sein")
    if len(v) > MAX_ID_LEN:
        raise ValidationError(f"{field_name} ist zu lang")
    if v.startswith("-"):
        raise ValidationError(f"{field_name} darf nicht mit '-' beginnen")
    if v.startswith("/"):
        raise ValidationError(f"{field_name} darf nicht mit '/' beginnen")
    if ".." in v.split("/"):
        raise ValidationError(f"{field_name} darf keine '..'-Segmente enthalten")
    if not ID_RE.match(v):
        raise ValidationError(
            f"{field_name} enthaelt unerlaubte Zeichen (erlaubt: A-Za-z0-9._/-)"
        )
    return v


def validate_text(value: Any, field_name: str, required: bool = True,
                  max_len: int = MAX_TEXT_LEN) -> str:
    if value is None:
        if required:
            raise ValidationError(f"{field_name} fehlt")
        return ""
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} muss ein String sein")
    if required and not value.strip():
        raise ValidationError(f"{field_name} darf nicht leer sein")
    if len(value) > max_len:
        raise ValidationError(f"{field_name} ist zu lang (max {max_len})")
    return value


def build_argv(name: str, **kwargs: Any) -> List[str]:
    template = COMMANDS[name]
    mapping = {k: str(v) for k, v in kwargs.items()}
    return [string.Template(part).substitute(mapping) for part in template]


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    gt_root: str = DEFAULT_GT_ROOT
    host: str = "127.0.0.1"
    port: int = 3100
    env_file: str = DEFAULT_ENV_FILE
    audit_log: str = "~/.local/state/gt-bridge/audit.jsonl"
    routes_file: str = "~/.config/gt-bridge/routes.json"
    state_file: str = "~/.local/state/gt-bridge/state.json"
    events_file: str = ""
    outbox_file: str = ""
    poll_interval: int = 60
    rate_limit: int = 10
    rate_window: int = 600
    api_rate_limit: int = 60
    api_rate_window: int = 60
    timeout: int = 60
    slack_webhook_url: str = ""
    slack_bot_token: str = ""
    tmux_socket: str = ""
    gt_bin: str = "gt"
    bd_bin: str = "bd"
    poll_enabled: bool = True
    env_vars: Dict[str, str] = field(default_factory=dict)

    def get(self, key: str, default: str = "") -> str:
        """os.environ hat Vorrang vor der Env-Datei."""
        if key in os.environ:
            return os.environ[key]
        return self.env_vars.get(key, default)

    @property
    def audit_path(self) -> str:
        return os.path.expanduser(self.audit_log)

    @property
    def routes_path(self) -> str:
        return os.path.expanduser(self.routes_file)

    @property
    def state_path(self) -> str:
        return os.path.expanduser(self.state_file)

    @property
    def events_path(self) -> str:
        if self.events_file:
            return os.path.expanduser(self.events_file)
        return os.path.join(self.gt_root, ".events.jsonl")

    @property
    def outbox_path(self) -> str:
        if self.outbox_file:
            return os.path.expanduser(self.outbox_file)
        return os.path.join(self.gt_root, "logs", "bridge-outbox.jsonl")

    @classmethod
    def load(cls, env_file: Optional[str] = None) -> "Config":
        path = env_file or os.environ.get("GT_BRIDGE_ENV", DEFAULT_ENV_FILE)
        env_vars = load_env_file(path)
        cfg = cls(env_file=path, env_vars=env_vars)
        cfg.gt_root = cfg.get("GT_ROOT", DEFAULT_GT_ROOT)
        cfg.host = cfg.get("GT_BRIDGE_HOST", "127.0.0.1")
        cfg.port = _env_int(cfg.get("GT_BRIDGE_PORT"), 3100)
        cfg.audit_log = cfg.get("GT_BRIDGE_AUDIT", "~/.local/state/gt-bridge/audit.jsonl")
        cfg.routes_file = cfg.get("GT_BRIDGE_ROUTES", "~/.config/gt-bridge/routes.json")
        cfg.state_file = cfg.get("GT_BRIDGE_STATE", "~/.local/state/gt-bridge/state.json")
        cfg.events_file = cfg.get("GT_BRIDGE_EVENTS", "")
        cfg.outbox_file = cfg.get("GT_BRIDGE_OUTBOX", "")
        cfg.poll_interval = _env_int(cfg.get("GT_BRIDGE_POLL_INTERVAL"), 60)
        cfg.rate_limit = _env_int(cfg.get("GT_BRIDGE_RATE_LIMIT"), 10)
        cfg.rate_window = _env_int(cfg.get("GT_BRIDGE_RATE_WINDOW"), 600)
        cfg.api_rate_limit = _env_int(cfg.get("GT_BRIDGE_API_RATE_LIMIT"), 60)
        cfg.api_rate_window = _env_int(cfg.get("GT_BRIDGE_API_RATE_WINDOW"), 60)
        cfg.timeout = _env_int(cfg.get("GT_BRIDGE_TIMEOUT"), 60)
        cfg.slack_webhook_url = cfg.get("SLACK_WEBHOOK_URL", "")
        cfg.slack_bot_token = cfg.get("SLACK_BOT_TOKEN", "")
        cfg.tmux_socket = cfg.get("GT_TMUX_SOCKET", "")
        cfg.gt_bin = cfg.get("GT_BIN", "gt")
        cfg.bd_bin = cfg.get("BD_BIN", "bd")
        return cfg

    def subprocess_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        if self.tmux_socket:
            env["GT_TMUX_SOCKET"] = self.tmux_socket
        return env


# ---------------------------------------------------------------------------
# Tokens / Identitaeten
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Identity:
    name: str
    admin: bool = False
    rigs: frozenset = frozenset()

    def allows_rig(self, rig: Optional[str]) -> bool:
        if self.admin:
            return True
        if not rig:
            return False
        return rig in self.rigs

    def allows_town(self) -> bool:
        return self.admin


class TokenStore:
    """Token -> Identity. Vergleich immer mit hmac.compare_digest."""

    def __init__(self, entries: List[Tuple[str, Identity]]):
        self._entries = [(t.encode("utf-8"), ident) for t, ident in entries if t]

    @classmethod
    def from_env_file(cls, path: str) -> "TokenStore":
        return cls.from_mapping(load_env_file(path))

    @classmethod
    def from_mapping(cls, env: Dict[str, str]) -> "TokenStore":
        entries: List[Tuple[str, Identity]] = []
        for key, token in env.items():
            if not key.startswith("BRIDGE_TOKEN_") or not token:
                continue
            name = key[len("BRIDGE_TOKEN_"):]
            if not name:
                continue
            if name == "ADMIN":
                ident = Identity(name="admin", admin=True, rigs=frozenset())
            else:
                scope_raw = env.get("BRIDGE_SCOPE_" + name, "")
                rigs = frozenset(
                    r.strip() for r in scope_raw.split(",") if r.strip()
                )
                ident = Identity(name=name, admin=False, rigs=rigs)
            entries.append((token, ident))
        return cls(entries)

    def authenticate(self, header: Optional[str]) -> Optional[Identity]:
        if not header:
            return None
        parts = header.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        supplied = parts[1].strip().encode("utf-8")
        if not supplied:
            return None
        match: Optional[Identity] = None
        for token_bytes, ident in self._entries:
            if hmac.compare_digest(token_bytes, supplied):
                match = ident
        return match


# ---------------------------------------------------------------------------
# Bead-Prefix-Routing (Scope-Aufloesung)
# ---------------------------------------------------------------------------

def load_routes(gt_root: str) -> Dict[str, Optional[str]]:
    """Liest prefix->rig aus .beads/routes.jsonl bzw. routes.jsonl.

    None = town-level (path ".").
    """
    routes: Dict[str, Optional[str]] = {}
    candidates = [
        os.path.join(gt_root, ".beads", "routes.jsonl"),
        os.path.join(gt_root, "routes.jsonl"),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    prefix = obj.get("prefix")
                    rpath = obj.get("path")
                    if not prefix or not rpath:
                        continue
                    if rpath == ".":
                        routes[prefix] = None
                    else:
                        routes[prefix] = rpath.split("/", 1)[0]
        except OSError as exc:  # pragma: no cover
            LOG.warning("routes.jsonl %s nicht lesbar: %s", path, exc)
    return routes


def bead_prefix(bead_id: str) -> str:
    idx = bead_id.find("-")
    if idx <= 0:
        return ""
    return bead_id[: idx + 1]


def rig_for_bead(routes: Dict[str, Optional[str]], bead_id: str) -> Optional[str]:
    prefix = bead_prefix(bead_id)
    if not prefix:
        return None
    return routes.get(prefix)


def rig_from_address(address: str) -> Optional[str]:
    """Ermittelt den Rig aus 'rig/agent'-Adressen. Town-Adressen -> None."""
    if "/" in address:
        head = address.split("/", 1)[0]
    else:
        head = address
    if not head:
        return None
    if head in ("mayor", "deacon", "overseer", "hq", "town"):
        return None
    return head


# ---------------------------------------------------------------------------
# Audit-Log
# ---------------------------------------------------------------------------

class AuditLog:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def write(self, record: Dict[str, Any]) -> None:
        record = dict(record)
        record.setdefault("ts", utc_now())
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        try:
            with self._lock:
                directory = os.path.dirname(self.path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError as exc:  # pragma: no cover - best effort
            LOG.warning("Audit-Log nicht schreibbar: %s", exc)


# ---------------------------------------------------------------------------
# Zustands-/Rate-Helfer (outbound)
# ---------------------------------------------------------------------------

class StateStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)
        except (OSError, ValueError):
            self._data = {}

    def save(self) -> None:
        with self._lock:
            directory = os.path.dirname(self.path)
            try:
                if directory:
                    os.makedirs(directory, exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh, ensure_ascii=False, sort_keys=True)
                os.replace(tmp, self.path)
            except OSError as exc:  # pragma: no cover
                LOG.warning("State nicht schreibbar: %s", exc)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value


class RateLimiter:
    """Max. N Posts pro Kanal und Zeitfenster (Default 10/600s)."""

    def __init__(self, state: StateStore, limit: int, window: int):
        self.state = state
        self.limit = limit
        self.window = window

    def allow(self, channel: str, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        bucket = list(self.state.get("rate", {}).get(channel, []))
        bucket = [t for t in bucket if now - t < self.window]
        if len(bucket) >= self.limit:
            self._store(channel, bucket)
            return False
        bucket.append(now)
        self._store(channel, bucket)
        return True

    def _store(self, channel: str, bucket: List[float]) -> None:
        rate = dict(self.state.get("rate", {}))
        rate[channel] = bucket[-max(self.limit, 1) * 2:]
        self.state.set("rate", rate)


class DedupeStore:
    def __init__(self, state: StateStore, max_seen: int = 5000):
        self.state = state
        self.max_seen = max_seen

    def first_seen(self, event_id: str) -> bool:
        seen = list(self.state.get("seen_events", []))
        if event_id in seen:
            return False
        seen.append(event_id)
        if len(seen) > self.max_seen:
            seen = seen[-self.max_seen:]
        self.state.set("seen_events", seen)
        return True


# ---------------------------------------------------------------------------
# JSONL-Outbox (bot-lesbar per Datei oder GET /api/bridge/outbox)
# ---------------------------------------------------------------------------

class Outbox:
    """Append-only JSONL-Ausgangskorb mit monotonem Cursor.

    Jeder Eintrag bekommt eine fortlaufende ``seq``. Leser merken sich die
    zuletzt gelesene ``seq`` und fragen mit ``since=<seq>`` nur Neues ab.
    Die Datei ist reines JSONL (eine Zeile pro Eintrag) und damit sowohl per
    Datei als auch ueber den HTTP-Endpunkt von Bots lesbar.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._seq: Optional[int] = None

    def _last_seq(self) -> int:
        last = 0
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    obj = _maybe_json(line)
                    if isinstance(obj, dict):
                        seq = obj.get("seq")
                        if isinstance(seq, int) and not isinstance(seq, bool) and seq > last:
                            last = seq
        except OSError:
            pass
        return last

    def write(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            if self._seq is None:
                self._seq = self._last_seq()
            self._seq += 1
            record = dict(entry)
            record["seq"] = self._seq
            record.setdefault("ts", utc_now())
            line = json.dumps(record, ensure_ascii=False, sort_keys=True)
            try:
                directory = os.path.dirname(self.path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                self._seq -= 1
                raise
            return record

    def read(self, since: int = 0, limit: int = 200,
             rigs: Optional[frozenset] = None
             ) -> Tuple[List[Dict[str, Any]], int, bool]:
        matches: List[Dict[str, Any]] = []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    obj = _maybe_json(line)
                    if not isinstance(obj, dict):
                        continue
                    seq = obj.get("seq")
                    if not isinstance(seq, int) or isinstance(seq, bool) or seq <= since:
                        continue
                    if rigs is not None and obj.get("rig") not in rigs:
                        continue
                    matches.append(obj)
        except OSError:
            pass
        has_more = len(matches) > limit
        matches = matches[:limit]
        cursor = since
        if matches:
            cursor = matches[-1].get("seq", since)
        return matches, cursor, has_more


# ---------------------------------------------------------------------------
# Slack-Versand
# ---------------------------------------------------------------------------
def default_http_post(url: str, payload: Dict[str, Any], token: Optional[str] = None) -> str:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        return resp.read().decode("utf-8", "replace")


class SlackNotifier:
    def __init__(self, config: Config, poster: Callable[..., str] = default_http_post):
        self.config = config
        self.poster = poster

    def send(self, channel: str, text: str,
             webhook_env: Optional[str] = None) -> str:
        """Return: webhook | bot | dry-run."""
        url = ""
        if webhook_env:
            url = self.config.get(webhook_env, "")
        if not url:
            url = self.config.slack_webhook_url
        if url:
            self.poster(url, {"channel": channel, "text": text}, None)
            return "webhook"
        if self.config.slack_bot_token:
            self.poster(
                "https://slack.com/api/chat.postMessage",
                {"channel": channel, "text": text},
                self.config.slack_bot_token,
            )
            return "bot"
        LOG.info("[dry-run] Slack-Post an %s: %s", channel, text)
        return "dry-run"


# ---------------------------------------------------------------------------
# Outbound-Router (routes.json)
# ---------------------------------------------------------------------------

class RouteTable:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._routes: Dict[str, Dict[str, Any]] = {}
        self._mtime = 0.0

    def reload_if_changed(self) -> None:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            self._routes = {}
            return
        with self._lock:
            if mtime == self._mtime:
                return
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self._routes = data if isinstance(data, dict) else {}
                self._mtime = mtime
            except (OSError, ValueError) as exc:
                LOG.warning("routes.json %s ungueltig: %s", self.path, exc)

    def route_for(self, rig: Optional[str]) -> Optional[Dict[str, Any]]:
        with self._lock:
            if rig and rig in self._routes:
                return self._routes[rig]
            if "_town" in self._routes:
                return self._routes["_town"]
            return None


# ---------------------------------------------------------------------------
# Kern-Bruecke
# ---------------------------------------------------------------------------

Runner = Callable[[List[str], str, int, Dict[str, str]], "subprocess.CompletedProcess"]


def default_runner(argv: List[str], cwd: str, timeout: int,
                   env: Dict[str, str]) -> "subprocess.CompletedProcess":
    return subprocess.run(  # noqa: S603 - argv-Liste, keine Shell
        argv,
        cwd=cwd,
        timeout=timeout,
        capture_output=True,
        text=True,
        env=env,
    )


class Bridge:
    def __init__(self, config: Config,
                 runner: Optional[Runner] = None,
                 notifier: Optional[SlackNotifier] = None):
        self.config = config
        self.runner = runner or default_runner
        self.tokens = TokenStore.from_env_file(config.env_file)
        self.audit = AuditLog(config.audit_path)
        self.routes = load_routes(config.gt_root)
        self.notifier = notifier or SlackNotifier(config)
        self.state = StateStore(config.state_path)
        self.rate = RateLimiter(self.state, config.rate_limit, config.rate_window)
        self.api_rate = RateLimiter(self.state, config.api_rate_limit,
                                    config.api_rate_window)
        self.dedupe = DedupeStore(self.state)
        self.route_table = RouteTable(config.routes_path)
        self.outbox = Outbox(config.outbox_path)

    # -- subprocess --------------------------------------------------------

    def _apply_bin_overrides(self, argv: List[str]) -> List[str]:
        if not argv:
            return argv
        first = argv[0]
        if first == "gt":
            return [self.config.gt_bin] + argv[1:]
        if first == "bd":
            return [self.config.bd_bin] + argv[1:]
        return argv

    def _run(self, argv: List[str], cwd: Optional[str] = None) -> "subprocess.CompletedProcess":
        argv = self._apply_bin_overrides(list(argv))
        workdir = cwd or self.config.gt_root
        try:
            return self.runner(argv, workdir, self.config.timeout,
                               self.config.subprocess_env())
        except FileNotFoundError as exc:
            raise CommandError(f"Befehl nicht gefunden: {argv[0]} ({exc})") from exc
        except subprocess.TimeoutExpired as exc:
            raise CommandError(f"Timeout nach {self.config.timeout}s: {argv[0]}") from exc
        except OSError as exc:
            raise CommandError(f"Fehler beim Start von {argv[0]}: {exc}") from exc

    def _run_json(self, argv: List[str], cwd: Optional[str] = None
                  ) -> Tuple[Optional[Any], Optional[str], int]:
        cp = self._run(argv, cwd=cwd)
        if cp.returncode != 0:
            return None, (cp.stderr or "").strip(), cp.returncode
        return _maybe_json(cp.stdout), cp.stdout, 0

    # -- Dispatching -------------------------------------------------------

    def request(self, method: str, path: str,
                query: Optional[Dict[str, str]] = None,
                headers: Optional[Dict[str, str]] = None,
                body: bytes = b"") -> Tuple[int, Dict[str, Any]]:
        query = query or {}
        headers = headers or {}
        audit: Dict[str, Any] = {"method": method, "path": path}

        if path.rstrip("/") == PREFIX + "/health":
            self.audit.write({**audit, "status": 200, "identity": "public",
                              "action": "health"})
            return 200, {
                "ok": True,
                "service": SERVICE_NAME,
                "version": VERSION,
                "time": utc_now(),
            }

        identity = self.tokens.authenticate(self._header(headers, "authorization"))
        if identity is None:
            self.audit.write({**audit, "status": 401, "identity": None,
                              "action": "auth"})
            return 401, {"ok": False, "error": "unauthorized"}

        audit["identity"] = identity.name
        audit["admin"] = identity.admin

        if not self.api_rate.allow("api:" + identity.name):
            self.audit.write({**audit, "status": 429, "action": "rate_limit"})
            return 429, {"ok": False, "error": "rate limit exceeded"}

        relative = path[len(PREFIX):].strip("/")
        parts = [p for p in relative.split("/") if p]
        action = parts[0] if parts else ""

        try:
            status, payload = self._dispatch(
                method, action, query, headers, body, identity
            )
        except ValidationError as exc:
            status, payload = 400, {"ok": False, "error": str(exc)}
        except CommandError as exc:
            status, payload = 502, {"ok": False, "error": str(exc)}
        except Exception as exc:  # pragma: no cover - defensive
            LOG.exception("Interner Fehler")
            status, payload = 500, {"ok": False, "error": f"interner Fehler: {exc}"}

        audit["status"] = status
        audit["action"] = action
        if isinstance(payload, dict) and "rig" in payload:
            audit["rig"] = payload.get("rig")
        self.audit.write(audit)
        return status, payload

    def _dispatch(self, method: str, action: str, query: Dict[str, str],
                  headers: Dict[str, str], body: bytes, identity: Identity
                  ) -> Tuple[int, Dict[str, Any]]:
        if action == "status" and method == "GET":
            return self._do_status(identity)
        if action == "ready" and method == "GET":
            return self._do_ready(identity, query)
        if action == "convoys" and method == "GET":
            return self._do_simple("convoy_list", identity, "convoys",
                                   CONVOY_RIG_KEYS)
        if action == "escalations" and method == "GET":
            return self._do_simple("escalations", identity, "escalations",
                                   ESCALATION_RIG_KEYS)
        if action == "outbox" and method == "GET":
            return self._do_outbox(identity, query)
        if action == "mail" and method == "POST":
            return self._do_mail(identity, body)
        if action == "nudge" and method == "POST":
            return self._do_nudge(identity, body)
        if action == "sling" and method == "POST":
            return self._do_sling(identity, body)
        if action == "bead" and method == "POST":
            return self._do_bead(identity, body)
        return 404, {"ok": False, "error": "unbekannter Endpunkt"}

    # -- Endpunkte ---------------------------------------------------------

    @staticmethod
    def _town_wide_denied(label: str = "") -> Tuple[int, Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "ok": False,
            "error": "town-weite Abfrage nur fuer Admin",
        }
        if label:
            payload["endpoint"] = label
        return 403, payload

    def _filter_for_identity(self, data: Any, identity: Identity,
                             allowed_keys: Tuple[str, ...] = ()
                             ) -> Tuple[Any, bool]:
        """Filtert Rig-bezogene Daten auf die Rigs der Identitaet.

        Whitelist-Prinzip: Bei einem Dict werden fuer Nicht-Admin nur die in
        ``allowed_keys`` genannten rig-bezogenen Listen uebernommen und auf
        die eigenen Rigs reduziert. Alle uebrigen Top-Level-Felder (z. B.
        ``overseer``, ``summary``, ``dolt``, ``tmux``, ``location``) werden
        verworfen, damit keine town-weiten Metadaten bzw. PII durchsickern.

        Rueckgabe (gefiltert, ok). ok=False -> unbekannte Form oder keine
        Rig-Attribution; der Aufrufer antwortet dann mit 403.
        """
        if identity.admin:
            return data, True
        if isinstance(data, list):
            filtered, any_attr = self._filter_items(data, identity)
            if data and not any_attr:
                return None, False
            return filtered, True
        if isinstance(data, dict):
            out: Dict[str, Any] = {}
            any_attr = False
            for key in allowed_keys:
                if key not in data:
                    continue
                value = data[key]
                if not isinstance(value, list):
                    return None, False
                if value and not all(isinstance(item, dict) for item in value):
                    return None, False
                filtered, attr = self._filter_items(value, identity)
                out[key] = filtered
                any_attr = any_attr or attr
            # Listen unter nicht erlaubten Schluesseln -> unbekannte Form.
            for key, value in data.items():
                if key in allowed_keys:
                    continue
                if isinstance(value, list):
                    return None, False
            if not any_attr:
                return None, False
            return out, True
        return None, False

    def _filter_items(self, items: List[Any], identity: Identity
                      ) -> Tuple[List[Any], bool]:
        filtered: List[Any] = []
        any_attr = False
        for item in items:
            if not isinstance(item, dict):
                continue
            rig = self._item_rig(item)
            if rig is None:
                continue
            any_attr = True
            if identity.allows_rig(rig):
                filtered.append(item)
        return filtered, any_attr

    def _item_rig(self, item: Dict[str, Any]) -> Optional[str]:
        rig = item.get("rig")
        if isinstance(rig, str) and rig:
            return rig
        bead = item.get("bead")
        if isinstance(bead, str) and bead:
            bead_rig = rig_for_bead(self.routes, bead)
            if bead_rig:
                return bead_rig
        for key in ("actor", "address", "name", "target", "to", "agent"):
            value = item.get(key)
            if isinstance(value, str) and value:
                addr_rig = rig_from_address(value)
                if addr_rig:
                    return addr_rig
        return None

    def _do_status(self, identity: Identity) -> Tuple[int, Dict[str, Any]]:
        data, raw, rc = self._run_json(COMMANDS["status"])
        if rc != 0:
            # Fallback: Textausgabe.
            cp = self._run(COMMANDS["status_text"])
            if cp.returncode == 0:
                if not identity.admin:
                    return self._town_wide_denied("status")
                return 200, {
                    "ok": True,
                    "identity": identity.name,
                    "format": "text",
                    "data": cp.stdout,
                }
            return 502, {"ok": False, "error": "gt status fehlgeschlagen",
                         "stderr": (cp.stderr or "").strip()}
        if data is None:
            if not identity.admin:
                return self._town_wide_denied("status")
            return 200, {"ok": True, "format": "text", "data": raw}
        if not identity.admin:
            filtered, ok = self._filter_for_identity(data, identity,
                                                     STATUS_RIG_KEYS)
            if not ok:
                return self._town_wide_denied("status")
            data = filtered
        return 200, {"ok": True, "format": "json", "data": data}

    def _do_ready(self, identity: Identity,
                  query: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        rig = query.get("rig")
        rig_val: Optional[str] = None
        if rig:
            rig_val = validate_id(rig, "rig")
            if not identity.allows_rig(rig_val):
                return 403, {"ok": False, "error": "scope verweigert", "rig": rig_val}
        elif not identity.admin:
            return 403, {"ok": False, "error": "nur Admin darf town-weites ready"}

        argv = list(COMMANDS["ready"])
        if rig_val:
            argv += [COMMANDS["ready_rig_flag"], rig_val]
        data, raw, rc = self._run_json(argv)
        if rc != 0:
            return 502, {"ok": False, "error": "gt ready fehlgeschlagen"}
        payload: Dict[str, Any] = {"ok": True}
        if rig_val:
            payload["rig"] = rig_val
        if data is None:
            payload["format"] = "text"
            payload["data"] = raw
        else:
            payload["format"] = "json"
            payload["data"] = data
        return 200, payload

    def _do_outbox(self, identity: Identity,
                   query: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        """GET /api/bridge/outbox?since=<seq>&limit=<n>[&rig=<rig>]"""
        since = _env_int(query.get("since"), 0)
        if since < 0:
            since = 0
        limit = _env_int(query.get("limit"), 200)
        limit = max(1, min(limit, 1000))

        rig = query.get("rig")
        rigs: Optional[frozenset] = None
        if rig:
            rig_val = validate_id(rig, "rig")
            if not identity.allows_rig(rig_val):
                return 403, {"ok": False, "error": "scope verweigert", "rig": rig_val}
            rigs = frozenset({rig_val})
        elif not identity.admin:
            # Nicht-Admins sehen nur Eintraege ihrer Rigs (Town-Eintraege
            # ohne rig bleiben Admin-only).
            rigs = identity.rigs

        entries, cursor, has_more = self.outbox.read(
            since=since, limit=limit, rigs=rigs
        )
        payload: Dict[str, Any] = {
            "ok": True,
            "entries": entries,
            "cursor": cursor,
            "count": len(entries),
            "has_more": has_more,
        }
        if rig:
            payload["rig"] = rig
        return 200, payload

    def _do_simple(self, command_name: str, identity: Identity,
                   label: str, allowed_keys: Tuple[str, ...] = ()
                   ) -> Tuple[int, Dict[str, Any]]:
        data, raw, rc = self._run_json(COMMANDS[command_name])
        if rc != 0:
            return 502, {"ok": False, "error": f"{command_name} fehlgeschlagen"}
        if data is None:
            if not identity.admin:
                return self._town_wide_denied(label)
            return 200, {"ok": True, label: raw, "format": "text"}
        if not identity.admin:
            filtered, ok = self._filter_for_identity(data, identity,
                                                     allowed_keys)
            if not ok:
                return self._town_wide_denied(label)
            data = filtered
        return 200, {"ok": True, label: data, "format": "json"}

    def _do_mail(self, identity: Identity,
                 body: bytes) -> Tuple[int, Dict[str, Any]]:
        payload = self._json_body(body)
        to = validate_id(payload.get("to"), "to")
        subject = validate_text(payload.get("subject"), "subject")
        message = validate_text(payload.get("body"), "body")

        rig = rig_from_address(to)
        if not identity.allows_rig(rig) and not (rig is None and identity.allows_town()):
            return 403, {"ok": False, "error": "scope verweigert", "to": to}

        argv = build_argv("mail_send", to=to, subject=subject, body=message)
        cp = self._run(argv)
        if cp.returncode != 0:
            return 502, {"ok": False, "error": "gt mail send fehlgeschlagen",
                         "stderr": (cp.stderr or "").strip()}
        return 200, {"ok": True, "to": to, "rig": rig,
                     "stdout": (cp.stdout or "").strip()}

    def _do_nudge(self, identity: Identity,
                  body: bytes) -> Tuple[int, Dict[str, Any]]:
        payload = self._json_body(body)
        target = validate_id(payload.get("target"), "target")
        message = validate_text(payload.get("message"), "message")
        mode = payload.get("mode")

        rig = rig_from_address(target)
        if not identity.allows_rig(rig) and not (rig is None and identity.allows_town()):
            return 403, {"ok": False, "error": "scope verweigert", "target": target}

        argv = build_argv("nudge", target=target, message=message)
        if mode is not None:
            if not isinstance(mode, str) or mode not in NUDGE_MODES:
                raise ValidationError(
                    "mode muss einer von " + ", ".join(sorted(NUDGE_MODES)) + " sein"
                )
            # Flags stehen vor dem '--', das die Nachricht schuetzt.
            idx = argv.index("--")
            argv[idx:idx] = [COMMANDS["nudge_mode_flag"], mode]
        cp = self._run(argv)
        if cp.returncode != 0:
            return 502, {"ok": False, "error": "gt nudge fehlgeschlagen",
                         "stderr": (cp.stderr or "").strip()}
        return 200, {"ok": True, "target": target, "rig": rig,
                     "mode": mode or DEFAULT_NUDGE_MODE,
                     "stdout": (cp.stdout or "").strip()}

    def _do_sling(self, identity: Identity,
                  body: bytes) -> Tuple[int, Dict[str, Any]]:
        payload = self._json_body(body)
        bead = validate_id(payload.get("bead"), "bead")
        rig = validate_id(payload.get("rig"), "rig")

        if not identity.allows_rig(rig):
            return 403, {"ok": False, "error": "scope verweigert", "rig": rig}

        bead_rig = rig_for_bead(self.routes, bead)
        if bead_rig is not None and bead_rig != rig:
            return 409, {
                "ok": False,
                "error": "bead-Prefix passt nicht zum rig",
                "bead": bead,
                "bead_rig": bead_rig,
                "rig": rig,
            }

        argv = build_argv("sling", bead=bead, rig=rig)
        cp = self._run(argv)
        if cp.returncode != 0:
            return 502, {"ok": False, "error": "gt sling fehlgeschlagen",
                         "stderr": (cp.stderr or "").strip()}
        return 200, {"ok": True, "bead": bead, "rig": rig,
                     "stdout": (cp.stdout or "").strip()}

    def _do_bead(self, identity: Identity,
                 body: bytes) -> Tuple[int, Dict[str, Any]]:
        payload = self._json_body(body)
        rig = validate_id(payload.get("rig"), "rig")
        title = validate_text(payload.get("title"), "title")
        description = validate_text(payload.get("description"), "description",
                                    required=False)

        if not identity.allows_rig(rig):
            return 403, {"ok": False, "error": "scope verweigert", "rig": rig}

        priority = payload.get("priority")
        argv = build_argv("bd_create", title=title, description=description)
        if priority is not None:
            if not isinstance(priority, int) or isinstance(priority, bool) or not (0 <= priority <= 4):
                raise ValidationError("priority muss eine Ganzzahl 0..4 sein")
            argv.append(
                string.Template(COMMANDS["bd_priority_template"]).substitute(
                    priority=priority
                )
            )

        cwd = os.path.join(self.config.gt_root, rig)
        cp = self._run(argv, cwd=cwd)
        if cp.returncode != 0:
            return 502, {"ok": False, "error": "bd create fehlgeschlagen",
                         "stderr": (cp.stderr or "").strip()}
        data = _maybe_json(cp.stdout)
        return 200, {"ok": True, "rig": rig,
                     "bead": data if data is not None else cp.stdout}

    # -- Helfer ------------------------------------------------------------

    @staticmethod
    def _header(headers: Dict[str, str], name: str) -> Optional[str]:
        for key, value in headers.items():
            if key.lower() == name.lower():
                return value
        return None

    @staticmethod
    def _json_body(body: bytes) -> Dict[str, Any]:
        if not body:
            raise ValidationError("JSON-Body fehlt")
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValidationError(f"ungueltiges JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValidationError("JSON-Body muss ein Objekt sein")
        return parsed


# ---------------------------------------------------------------------------
# Outbound-Events
# ---------------------------------------------------------------------------

class OutboundPoller(threading.Thread):
    def __init__(self, bridge: Bridge, interval: int, stop_event: threading.Event):
        super().__init__(name="gt-bridge-poller", daemon=True)
        self.bridge = bridge
        self.config = bridge.config
        self.interval = max(1, interval)
        self.stop_event = stop_event

    def run(self) -> None:  # pragma: no cover - thread loop
        while not self.stop_event.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - Poller darf nie sterben
                LOG.exception("Poller-Durchlauf fehlgeschlagen")
            self.stop_event.wait(self.interval)

    # -- Quellen -----------------------------------------------------------

    def poll_once(self) -> List[Dict[str, Any]]:
        events = self.read_new_events()
        if events is None:
            events = self.fallback_events()
        handled = [e for e in events if self.handle_event(e)]
        return handled

    def read_new_events(self) -> Optional[List[Dict[str, Any]]]:
        path = self.config.events_path
        if not os.path.exists(path):
            return None
        try:
            size = os.path.getsize(path)
        except OSError:
            return None
        offset = int(self.bridge.state.get("events_offset", 0) or 0)
        if size < offset:
            offset = 0
        try:
            with open(path, "r", encoding="utf-8") as fh:
                fh.seek(offset)
                lines = fh.readlines()
                new_offset = fh.tell()
        except OSError as exc:
            LOG.warning("events.jsonl nicht lesbar: %s", exc)
            return []
        self.bridge.state.set("events_offset", new_offset)
        self.bridge.state.save()
        events = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            obj = _maybe_json(line)
            if isinstance(obj, dict):
                events.append(obj)
        return events

    def fallback_events(self) -> List[Dict[str, Any]]:
        """Diff gegen vorherigen Zustand aus convoy/ready/escalate/status."""
        snapshot: Dict[str, Any] = {
            "escalations": [],
            "agents": {},
            "convoys": {},
        }
        events: List[Dict[str, Any]] = []

        esc_data, _, esc_rc = self.bridge._run_json(COMMANDS["escalations"])
        if esc_rc == 0 and esc_data is not None:
            for item in self._iter_items(esc_data):
                eid = item.get("id")
                if eid:
                    snapshot["escalations"].append(eid)

        status_data, _, status_rc = self.bridge._run_json(COMMANDS["status"])
        if status_rc == 0 and isinstance(status_data, dict):
            for agent in self._iter_agents(status_data):
                addr = agent.get("address") or agent.get("name")
                if not addr:
                    continue
                snapshot["agents"][addr] = {
                    "running": bool(agent.get("running")),
                    "state": agent.get("state") or "",
                    "rig": rig_from_address(addr),
                }

        convoy_data, _, convoy_rc = self.bridge._run_json(COMMANDS["convoy_list"])
        if convoy_rc == 0 and convoy_data is not None:
            for item in self._iter_items(convoy_data):
                cid = item.get("id")
                if cid:
                    snapshot["convoys"][cid] = {
                        "status": item.get("status") or "",
                        "closed": bool(item.get("closed")),
                    }

        previous = self.bridge.state.get("snapshot") or {}

        prev_esc = set(previous.get("escalations", []))
        for eid in snapshot["escalations"]:
            if eid not in prev_esc:
                events.append({
                    "ts": utc_now(),
                    "source": "poll",
                    "type": "escalation_sent",
                    "actor": "town",
                    "payload": {"escalation": eid},
                })

        prev_agents = previous.get("agents", {})
        for addr, info in snapshot["agents"].items():
            old = prev_agents.get(addr)
            if not old:
                continue
            if old.get("running") and not info.get("running"):
                events.append({
                    "ts": utc_now(),
                    "source": "poll",
                    "type": "session_death",
                    "actor": addr,
                    "payload": {"agent": addr, "rig": info.get("rig")},
                })
            elif info.get("state") == "stuck" and old.get("state") != "stuck":
                events.append({
                    "ts": utc_now(),
                    "source": "poll",
                    "type": "agent_stuck",
                    "actor": addr,
                    "payload": {"agent": addr, "rig": info.get("rig")},
                })

        prev_conv = previous.get("convoys", {})
        for cid, info in snapshot["convoys"].items():
            old = prev_conv.get(cid)
            if old and not old.get("closed") and info.get("closed"):
                events.append({
                    "ts": utc_now(),
                    "source": "poll",
                    "type": "done",
                    "actor": "town",
                    "payload": {"bead": cid},
                })

        self.bridge.state.set("snapshot", snapshot)
        self.bridge.state.save()
        return events

    @staticmethod
    def _iter_items(data: Any) -> List[Dict[str, Any]]:
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            for key in ("items", "escalations", "convoys", "data"):
                value = data.get(key)
                if isinstance(value, list):
                    return [d for d in value if isinstance(d, dict)]
        return []

    @staticmethod
    def _iter_agents(status: Dict[str, Any]) -> List[Dict[str, Any]]:
        agents: List[Dict[str, Any]] = list(status.get("agents", []) or [])
        for rig in status.get("rigs", []) or []:
            if isinstance(rig, dict):
                agents.extend(rig.get("agents", []) or [])
        return [a for a in agents if isinstance(a, dict)]

    # -- Verarbeitung ------------------------------------------------------

    def handle_event(self, event: Dict[str, Any]) -> bool:
        bridge = self.bridge
        raw_type = str(event.get("type", ""))
        etype = EVENT_TYPE_MAP.get(raw_type)

        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        # Mail an den Overseer ist eine bot-lesbare Pflichtmeldung: sie landet
        # immer in der Outbox, auch ohne Slack-Route (Cid/Ami-Anforderung).
        if etype is None and raw_type == "mail" and self._is_overseer_mail(payload):
            etype = "overseer_mail"
        if not etype:
            return False

        rig = payload.get("rig") or self._rig_for_event(event, payload)
        event_id = self._event_id(event, etype, payload)

        if not bridge.dedupe.first_seen(event_id):
            LOG.debug("Dedupe: Event %s bereits gesendet", event_id)
            bridge.state.save()
            return False

        bridge.route_table.reload_if_changed()
        route = bridge.route_table.route_for(rig) or {}
        text = self._format_message(etype, event, payload, route)

        # 1) Immer in die JSONL-Outbox schreiben (bot-lesbar per Datei/API,
        #    unabhaengig davon, ob Slack konfiguriert ist).
        try:
            bridge.outbox.write(
                self._outbox_entry(etype, event, payload, rig, event_id, text)
            )
        except Exception as exc:  # noqa: BLE001 - Outbox-Fehler nicht fatal
            LOG.warning("Outbox-Schreiben fehlgeschlagen: %s", exc)

        # 2) Slack-Post nur, wenn eine Route mit channel existiert. Ohne
        #    Credentials bleibt das ein Dry-run ins Log.
        channel = route.get("channel")
        if not channel:
            bridge.state.save()
            LOG.info("Kein Slack-Route-Eintrag fuer rig=%s (Event %s)", rig, etype)
            return True

        if not bridge.rate.allow(channel):
            bridge.state.save()
            LOG.warning("Rate-Limit erreicht fuer %s, Slack-Post fuer %s verworfen",
                        channel, event_id)
            return True
        bridge.state.save()

        try:
            result = bridge.notifier.send(channel, text, route.get("webhook_env"))
        except Exception as exc:  # noqa: BLE001 - Slack-Fehler nicht fatal
            LOG.warning("Slack-Versand fehlgeschlagen: %s", exc)
            return True
        LOG.info("Event %s (%s) -> %s via %s", event_id, etype, channel, result)
        return True

    @staticmethod
    def _is_overseer_mail(payload: Dict[str, Any]) -> bool:
        to = payload.get("to")
        if not isinstance(to, str):
            return False
        return to.strip().lower().rstrip("/") in OVERSEER_MAIL_RECIPIENTS

    @staticmethod
    def _outbox_entry(etype: str, event: Dict[str, Any],
                      payload: Dict[str, Any], rig: Optional[str],
                      event_id: str, text: str) -> Dict[str, Any]:
        return {
            "kind": etype,
            "type": event.get("type"),
            "actor": event.get("actor") or "",
            "rig": rig,
            "event_id": event_id,
            "text": text,
            "source": event.get("source") or "gt",
            "payload": payload,
        }

    def _rig_for_event(self, event: Dict[str, Any],
                       payload: Dict[str, Any]) -> Optional[str]:
        bead = payload.get("bead")
        if isinstance(bead, str) and bead:
            rig = rig_for_bead(self.bridge.routes, bead)
            if rig:
                return rig
        actor = event.get("actor")
        if isinstance(actor, str) and "/" in actor:
            return rig_from_address(actor)
        return None

    @staticmethod
    def _event_id(event: Dict[str, Any], etype: str,
                  payload: Dict[str, Any]) -> str:
        for key in ("id", "mr", "bead", "escalation", "session_id", "session"):
            value = payload.get(key)
            if value:
                return f"{etype}:{key}:{value}"
        raw = json.dumps(
            {"type": event.get("type"), "ts": event.get("ts"),
             "actor": event.get("actor"), "payload": payload},
            sort_keys=True,
        )
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        return f"{etype}:{digest}"

    @staticmethod
    def _format_message(etype: str, event: Dict[str, Any],
                        payload: Dict[str, Any],
                        route: Dict[str, Any]) -> str:
        parts: List[str] = []
        prefix = route.get("owner_prefix")
        if prefix:
            parts.append(str(prefix))
        if etype in TOWN_ALERT_TYPES and TOWN_ALERT_PREFIX:
            parts.append(TOWN_ALERT_PREFIX)

        emoji = EVENT_EMOJI.get(etype, "")
        actor = event.get("actor") or "?"
        detail = (
            payload.get("bead")
            or payload.get("mr")
            or payload.get("escalation")
            or payload.get("agent")
            or payload.get("session")
            or payload.get("to")
            or ""
        )
        title = payload.get("title") or payload.get("subject") or ""
        summary = f"{etype}: {detail}" if detail else etype
        if title:
            summary += f" ({title})"
        summary = f"{actor} - {summary}"
        parts.append(f"{emoji} {summary}".strip())
        return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# HTTP-Server
# ---------------------------------------------------------------------------

class BridgeHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = SERVICE_NAME + "/" + VERSION
    protocol_version = "HTTP/1.1"
    bridge: Bridge = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, None)

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith(PREFIX):
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            self._send(413, {"ok": False, "error": "body zu gross"})
            return
        body = self.rfile.read(length) if length else b""
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        headers = {k: v for k, v in self.headers.items()}
        status, payload = self.bridge.request(
            method, parsed.path, query, headers, body
        )
        self._send(status, payload)

    def _send(self, status: int, payload: Optional[Dict[str, Any]]) -> None:
        if payload is None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_server(config: Config, bridge: Bridge) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (BridgeHTTPRequestHandler,), {"bridge": bridge})
    return ThreadingHTTPServer((config.host, config.port), handler)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="gt-bridge")
    parser.add_argument("--env-file", default=None, help="Pfad zur bridge.env")
    parser.add_argument("--gt-root", default=None, help="Gas-Town-Root")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--once", action="store_true",
                        help="Nur einen Outbound-Poll durchfuehren, dann beenden")
    parser.add_argument("--no-poller", action="store_true",
                        help="Keinen Poller-Thread starten")
    parser.add_argument("--check", action="store_true",
                        help="Konfiguration laden und validieren, dann beenden")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)
    config = Config.load(args.env_file)
    if args.gt_root:
        config.gt_root = args.gt_root
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port

    bridge = Bridge(config)

    if args.check:
        LOG.info("Konfiguration ok: %d Token, %d Routen, gt_root=%s, outbox=%s",
                 len(bridge.tokens._entries), len(bridge.routes), config.gt_root,
                 config.outbox_path)
        return 0

    if args.once:
        poller = OutboundPoller(bridge, config.poll_interval, threading.Event())
        handled = poller.poll_once()
        LOG.info("%d Event(s) verarbeitet", len(handled))
        return 0

    stop_event = threading.Event()
    if config.poll_enabled and not args.no_poller:
        poller = OutboundPoller(bridge, config.poll_interval, stop_event)
        poller.start()

    server = make_server(config, bridge)
    LOG.info("%s %s lauscht auf http://%s:%d%s/", SERVICE_NAME, VERSION,
             config.host, config.port, PREFIX)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Beende...")
    finally:
        stop_event.set()
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
