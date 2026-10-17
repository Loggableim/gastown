# gt-bridge

Kleine Brücke zwischen **Gas Town** und externen Bots.

* **Admin-API** auf `127.0.0.1:3100`, Bearer-Auth, Scope pro Rig.
* **Outbound-Events**: Poller liest Gas-Town-Events und schreibt sie als JSONL
  in die Outbox (bot-lesbar per Datei/Endpoint); optional Post nach Slack.

Nur Python-Standardbibliothek, Zielversion **Python 3.10** (Ubuntu 22.04).
Läuft als User `gastown`. Caddy routet `agents.lastbrowser.com/api/bridge/*`
dorthin (vgl. `Caddyfile.snippet`).

---

## 1. Dateien

| Datei | Zweck |
|-------|-------|
| `gt_bridge.py`         | Server + Poller (alles in einem Modul) |
| `test_gt_bridge.py`    | unittest, subprocess/Slack gemockt |
| `gt-bridge.service`    | systemd-Unit (User=gastown, Slice=gastown.slice) |
| `Caddyfile.snippet`    | Reverse-Proxy-Regel **vor** der Dashboard-Basic-Auth |

Zentraler Anpassungspunkt für die echte CLI ist die Tabelle **`COMMANDS`** in
`gt_bridge.py` (alle `gt`/`bd`-Unterbefehle und Flags).

---

## 2. Setup

```bash
# 1) Verzeichnisse
install -d -m 700 /home/gastown/.config/gt-bridge
install -d -m 755 /home/gastown/.local/state/gt-bridge

# 2) Dateien ablegen (im Fork: contrib/bridge/)
#    gt_bridge.py, test_gt_bridge.py, gt-bridge.service, Caddyfile.snippet
```

### Tokens erzeugen

```bash
python3 -c "import secrets;print(secrets.token_urlsafe(32))"
```

### Env-Datei `/home/gastown/.config/gt-bridge/bridge.env`

```dotenv
# --- Tokens ------------------------------------------------------------
# Admin: alle Rigs + Town
BRIDGE_TOKEN_ADMIN=<token-admin>
# Normaler Bot: nur die aufgeführten Rigs
BRIDGE_TOKEN_VALI=<token-vali>
BRIDGE_SCOPE_VALI=tiroltourismus
BRIDGE_TOKEN_AMI=<token-ami>
BRIDGE_SCOPE_AMI=sk-games,sk-quiz

# --- Outbound (optional; sonst nur Dry-run ins Log) --------------------
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
# alternativ:
# SLACK_BOT_TOKEN=xoxb-...

# --- Overrides (optional) ---------------------------------------------
# GT_BRIDGE_HOST=127.0.0.1
# GT_BRIDGE_PORT=3100
# GT_ROOT=/home/gastown/gt
# GT_BRIDGE_POLL_INTERVAL=60
# GT_BRIDGE_RATE_LIMIT=10
# GT_BRIDGE_RATE_WINDOW=600
# GT_BRIDGE_ROUTES=/home/gastown/.config/gt-bridge/routes.json
# GT_BRIDGE_OUTBOX=/home/gastown/gt/logs/bridge-outbox.jsonl
```

Die Env-Datei sollte `chmod 600` sein. **Keine echten Tokens ins Repository.**

### Slack-Routing `/home/gastown/.config/gt-bridge/routes.json`

```json
{
  "tiroltourismus": { "channel": "#tirol-content", "owner_prefix": "FUER-VALI" },
  "sk-games":       { "channel": "#sk-games",     "owner_prefix": "FUER-AMI" },
  "_town":          { "channel": "#gastown-town", "owner_prefix": "FUER-AMI" }
}
```

* `owner_prefix` wird jeder Nachricht vorangestellt (z. B. Slack-Usergroup).
* `_town` ist der Fallback für rig-lose Events.
* `webhook_env` kann pro Rig einen alternativen Webhook setzen:
  `{"channel": "#x", "owner_prefix": "FUER-X", "webhook_env": "SLACK_WEBHOOK_X"}`.
* Eskalationen und tote/hängende Agenten bekommen **zusätzlich** `FUER-AMI`.

---

## 3. Endpunkte

Basis: `https://agents.lastbrowser.com/api/bridge` (lokal `http://127.0.0.1:3100`).
Auth: `Authorization: Bearer <token>`. Nur `health` ist ohne Auth.

| Methode | Pfad | Befehl intern |
|---------|------|---------------|
| GET  | `/api/bridge/health` | – |
| GET  | `/api/bridge/status` | `gt status --json` |
| GET  | `/api/bridge/ready?rig=` | `gt ready --json [--rig X]` |
| GET  | `/api/bridge/convoys` | `gt convoy list --json` |
| GET  | `/api/bridge/escalations` | `gt escalate list --json` |
| GET  | `/api/bridge/outbox?since=&limit=&rig=` | JSONL-Outbox lesen (Cursor) |
| POST | `/api/bridge/mail` | `gt mail send <to> -s <subj> -m <body>` |
| POST | `/api/bridge/nudge` | `gt nudge <target> <message> [--mode <wait-idle|queue|immediate>]` |
| POST | `/api/bridge/sling` | `gt sling <bead> <rig>` |
| POST | `/api/bridge/bead` | `bd create --json` im Rig-Verzeichnis |

### Scope-Regeln

* `ADMIN`-Token: alle Rigs + Town.
* Normale Tokens: nur die Rigs in `BRIDGE_SCOPE_<NAME>`.
* Rig wird aus Body/Query aufgelöst; bei Beads zusätzlich über den Prefix aus
  `~/gt/.beads/routes.jsonl` bzw. `~/gt/routes.jsonl`.
* Nicht-Admins dürfen **nur Adressen ihres Rigs** anschreiben (`mail`/`nudge`);
  Town-Adressen wie `mayor/` sind Admin-only.
* Fehlender/ungültiger Token → `401`; Rig nicht im Scope → `403`.

### Validierung

rig/agent/bead/`to`/`target` müssen `^[A-Za-z0-9._/-]+$` erfüllen, dürfen nicht
mit `-` beginnen und max. 512 Zeichen lang sein. Alle Kommandos laufen als
**Argumentliste ohne Shell** (`subprocess.run`, `shell=False`), `cwd=GT_ROOT`,
Timeout 60 s, `GT_TMUX_SOCKET` wird vererbt.

### curl-Beispiele

```bash
BASE=https://agents.lastbrowser.com/api/bridge
TOKEN=<token-vali>

# Health (ohne Auth)
curl -s $BASE/health

# Status (Admin)
curl -s -H "Authorization: Bearer $TOKEN" $BASE/status

# Ready eines Rigs im Scope
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/ready?rig=tiroltourismus"

# Convoys / Eskalationen
curl -s -H "Authorization: Bearer $TOKEN" $BASE/convoys
curl -s -H "Authorization: Bearer $TOKEN" $BASE/escalations

# Outbox ab Cursor 0 lesen (nur eigene Rigs beim Nicht-Admin-Token)
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/outbox?since=0&limit=100"

# Mail an den Witness des eigenen Rigs
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"to":"tiroltourismus/witness","subject":"Status","body":"Bitte pruefen"}' \
  $BASE/mail

# Nudge (Default-Modus wait-idle)
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"target":"tiroltourismus/furiosa","message":"Check mail"}' \
  $BASE/nudge

# Nudge mit explizitem Modus (wait-idle | queue | immediate)
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"target":"tiroltourismus/furiosa","message":"Jetzt","mode":"immediate"}' \
  $BASE/nudge

# Sling
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"bead":"tt-123","rig":"tiroltourismus"}' \
  $BASE/sling

# Bead anlegen
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"rig":"tiroltourismus","title":"Fix","description":"Details","priority":2}' \
  $BASE/bead
```

---

## 4. Outbound-Events: JSONL-Outbox + Slack

Der Poller (Thread, Default 60 s) nutzt primär das Event-Log
`~/gt/.events.jsonl` (Format siehe `internal/events/events.go`):

```json
{"ts":"...","source":"gt","type":"merged","actor":"tiroltourismus/polecat",
 "payload":{"mr":"mr-7","worker":"furiosa","branch":"..."},"visibility":"both"}
```

Erkannte Typen (Mapping in `EVENT_TYPE_MAP`):

| Bruecken-Typ | gt-Event-Typ (Beispiel) |
|--------------|-------------------------|
| `pr_created` | `pr_created` |
| `pr_merged`  | `merged` |
| `escalation` | `escalation_sent` |
| `agent_stuck`| `agent_stuck`, state `stuck` |
| `agent_dead` | `session_death`, `mass_death` |
| `bead_done`  | `done` |
| `overseer_mail` | `mail` mit `payload.to` = `overseer`/`human` |

Jedes erkannte Event wird **immer** als eine Zeile JSONL an die Outbox
angehängt (`GT_BRIDGE_OUTBOX`, Default `~/gt/logs/bridge-outbox.jsonl`).
Zusätzlich werden Mails an den Overseer (Mayor-Meldungen) erfasst — also
genau die Nachrichten, die früher nur im Overseer-Postfach lagen.

### Outbox-Format (eine JSON-Zeile pro Eintrag)

```json
{"seq":42,"ts":"2026-10-17T18:00:00+00:00","kind":"escalation",
 "type":"escalation_sent","actor":"tiroltourismus/witness","rig":"tiroltourismus",
 "event_id":"escalation:escalation:hq-1","text":"FUER-VALI FUER-AMI 🚨 ...",
 "source":"gt","payload":{"escalation":"hq-1"}}
```

`seq` ist ein monotoner Cursor. Leser merken sich die zuletzt gelesene `seq`
und fragen mit `GET /api/bridge/outbox?since=<seq>` nur neue Einträge ab
(Antwort: `{ok, entries, cursor, count, has_more}`). Nicht-Admin-Tokens sehen
nur Einträge ihrer Rigs; `&rig=<rig>` filtert zusätzlich. Die Datei ist auch
direkt per Dateisystem lesbar.

**Slack-Versand (optional)**: `SLACK_WEBHOOK_URL` (pro Rig via `webhook_env`
überschreibbar) oder `SLACK_BOT_TOKEN` + `chat.postMessage` über `urllib`.
Es werden **keine** Slack-Credentials ins Repo committed. Fehlt beides, wird
nur ins Log geschrieben (Dry-run, kein Fehler). Ein Rate-Limit verwirft nur den
Slack-Post, **nicht** den Outbox-Eintrag.

Der Poller (`~/.local/state/gt-bridge/state.json`) verfolgt den Lese-Offset in
`.events.jsonl` und die bereits gesendeten `event_id` (Dedupe). Das Rate-Limit
(`GT_BRIDGE_RATE_LIMIT`/`GT_BRIDGE_RATE_WINDOW`, Default 10/600 s) gilt pro
Kanal.

**Fallback** (wenn `.events.jsonl` fehlt): Diff-Polling von
`gt convoy list`, `gt ready`, `gt escalate list` und `gt status` gegen den
letzten Zustand in `state.json`.

---

## 5. Betrieb

```bash
# Syntax/Config prüfen
python3 gt_bridge.py --check

# Vordergrund (zum Testen)
python3 gt_bridge.py -v --no-poller

# Einen Poll-Durchlauf ohne Server
python3 gt_bridge.py --once

# Tests
python3 -m unittest
```

### systemd

```bash
sudo cp gt-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gt-bridge
systemctl status gt-bridge
journalctl -u gt-bridge -f
```

Der Service erwartet das Skript unter
`/home/gastown/gastown/contrib/bridge/gt_bridge.py` – Pfad in der Unit ggf.
anpassen.

### Caddy

Block aus `Caddyfile.snippet` **vor** der Dashboard-Basic-Auth einfügen und
`sudo systemctl reload caddy`. Kein `handle_path` verwenden, sonst geht das
`/api/bridge`-Präfix verloren.

### Audit-Log

Ein JSONL-Eintrag pro Request unter
`~/.local/state/gt-bridge/audit.jsonl` (ohne Token):

```json
{"action":"mail","admin":false,"identity":"VALI","method":"POST",
 "path":"/api/bridge/mail","rig":"tiroltourismus","status":200,"ts":"..."}
```

---

## 6. Was noch fehlt / am Server zu tun

1. **`COMMANDS` gegen die echte CLI verifiziert** (Stand 2026-10):
   `gt status --json`, `gt ready --json [--rig X]`, `gt convoy list --json`,
   `gt escalate list --json`, `gt mail send <to> -s/-m`, `gt nudge <target>
   <message> [--mode ...]`, `gt sling <bead-or-formula> [target]` und
   `bd create --json --title --description --priority` existieren in dieser
   Form. `gt nudge --mode` kennt `wait-idle` (Default), `queue`, `immediate`.
   Bei künftigen CLI-Änderungen nur die Tabelle `COMMANDS` anpassen.
2. **`pr_created`**: Gas Town schreibt aktuell kein `pr_created`-Event.
   Mapping ergänzen, sobald ein passender Event-Typ existiert.
3. **Fallback-Diff** erkennt `pr_created`/`pr_merged` nur eingeschränkt
   (über Event-Log vollständig). Fallback-Heuristiken verfeinern.
4. **Slack-Mentions**: `owner_prefix` (z. B. `FUER-VALI`) als reiner Text;
   ggf. auf `<@UGROUP>`-Syntax umstellen.
5. **Testlauf am Server**: systemd-Unit und Caddy-Route zusammen mit der
   echten `gt`/`bd`-CLI verifizieren, danach Hardening ergänzen.
6. **Rate-Limit-verworfene Events** werden aktuell nicht nachgereicht
   (bewusst, sonst Rückstau). Bei Bedarf Queue ergänzen.
