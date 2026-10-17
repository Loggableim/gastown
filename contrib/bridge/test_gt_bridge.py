#!/usr/bin/env python3
"""Tests fuer gt-bridge.

Ausfuehren:  python3 -m unittest -v
Subprozesse und Slack werden gemockt; es wird nichts nach aussen gesendet.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gt_bridge as gb  # noqa: E402

logging.disable(logging.CRITICAL)


ADMIN_TOKEN = "admin-secret-token"
VALI_TOKEN = "vali-secret-token"


class FakeRunner:
    """Ersetzt subprocess.run. Antworten werden per argv-Match gesteuert."""

    def __init__(self):
        self.calls = []
        self._responses = []
        self.default = subprocess.CompletedProcess([], 0, "{}", "")

    def when(self, contains, rc=0, stdout="{}", stderr=""):
        self._responses.append((tuple(contains), rc, stdout, stderr))
        return self

    def __call__(self, argv, cwd, timeout, env):
        self.calls.append({"argv": list(argv), "cwd": cwd,
                           "timeout": timeout, "env": env})
        args = list(argv)
        for contains, rc, stdout, stderr in self._responses:
            if all(part in args for part in contains):
                return subprocess.CompletedProcess(args, rc, stdout, stderr)
        return self.default

    def last_argv(self):
        return self.calls[-1]["argv"]


def make_env(dirpath, extra=None):
    path = os.path.join(dirpath, "bridge.env")
    lines = [
        f"BRIDGE_TOKEN_ADMIN={ADMIN_TOKEN}",
        f"BRIDGE_TOKEN_VALI={VALI_TOKEN}",
        "BRIDGE_SCOPE_VALI=tiroltourismus",
    ]
    if extra:
        lines.extend(extra)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


class BridgeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.gt_root = os.path.join(self.root, "gt")
        os.makedirs(os.path.join(self.gt_root, ".beads"), exist_ok=True)
        self.env_file = make_env(self.root)
        self.runner = FakeRunner()
        self.config = gb.Config(
            gt_root=self.gt_root,
            env_file=self.env_file,
            audit_log=os.path.join(self.root, "audit.jsonl"),
            routes_file=os.path.join(self.root, "routes.json"),
            state_file=os.path.join(self.root, "state.json"),
            events_file=os.path.join(self.gt_root, ".events.jsonl"),
            slack_webhook_url="",
            slack_bot_token="",
        )
        self.bridge = gb.Bridge(self.config, runner=self.runner)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, path, token=None, query=None, body=None):
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        return self.bridge.request(method, path, query or {}, headers, raw)

    def write_routes_jsonl(self, entries):
        path = os.path.join(self.gt_root, ".beads", "routes.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")
        # Routen nach dem Schreiben neu laden.
        self.bridge.routes = gb.load_routes(self.gt_root)

    def write_slack_routes(self, mapping):
        with open(self.config.routes_path, "w", encoding="utf-8") as fh:
            json.dump(mapping, fh)


# ---------------------------------------------------------------------------
# Auth / Scope
# ---------------------------------------------------------------------------

class AuthTests(BridgeTestCase):
    def test_health_without_auth(self):
        status, payload = self.call("GET", "/api/bridge/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_missing_token_is_401(self):
        status, payload = self.call("GET", "/api/bridge/status")
        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])

    def test_wrong_token_is_401(self):
        status, _ = self.call("GET", "/api/bridge/status", token="nope")
        self.assertEqual(status, 401)

    def test_admin_can_read_status(self):
        self.runner.when(["gt", "status", "--json"], rc=0,
                         stdout=json.dumps({"name": "town"}), stderr="")
        status, payload = self.call("GET", "/api/bridge/status", token=ADMIN_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"], {"name": "town"})

    def test_unknown_endpoint_404(self):
        status, _ = self.call("GET", "/api/bridge/nope", token=ADMIN_TOKEN)
        self.assertEqual(status, 404)

    def test_non_bridge_path_ignored(self):
        status, _ = self.call("GET", "/other/thing", token=ADMIN_TOKEN)
        # action "" -> 404
        self.assertEqual(status, 404)


class ScopeTests(BridgeTestCase):
    def test_ready_scope_denied(self):
        status, payload = self.call("GET", "/api/bridge/ready",
                                    token=VALI_TOKEN,
                                    query={"rig": "tiroltourismus"})
        self.assertEqual(status, 200)

        status, payload = self.call("GET", "/api/bridge/ready",
                                    token=VALI_TOKEN,
                                    query={"rig": "fremdrig"})
        self.assertEqual(status, 403)

    def test_ready_town_requires_admin(self):
        status, _ = self.call("GET", "/api/bridge/ready", token=VALI_TOKEN)
        self.assertEqual(status, 403)

    def test_ready_rig_flag_in_argv(self):
        self.runner.when(["gt", "ready", "--json", "--rig", "tiroltourismus"])
        status, payload = self.call("GET", "/api/bridge/ready",
                                    token=VALI_TOKEN,
                                    query={"rig": "tiroltourismus"})
        self.assertEqual(status, 200)
        self.assertIn("--rig", self.runner.last_argv())
        self.assertIn("tiroltourismus", self.runner.last_argv())

    def test_invalid_rig_is_400(self):
        status, payload = self.call("GET", "/api/bridge/ready",
                                    token=ADMIN_TOKEN,
                                    query={"rig": "bad;rm -rf"})
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])

    def test_mail_to_other_rig_denied(self):
        status, payload = self.call(
            "POST", "/api/bridge/mail", token=VALI_TOKEN,
            body={"to": "fremdrig/witness", "subject": "x", "body": "y"},
        )
        self.assertEqual(status, 403)

    def test_mail_to_mayor_requires_admin(self):
        # Non-admin darf Town-Adressen nicht anschreiben.
        status, _ = self.call(
            "POST", "/api/bridge/mail", token=VALI_TOKEN,
            body={"to": "mayor/", "subject": "x", "body": "y"},
        )
        self.assertEqual(status, 403)

        self.runner.when(["gt", "mail", "send", "mayor/"])
        status, payload = self.call(
            "POST", "/api/bridge/mail", token=ADMIN_TOKEN,
            body={"to": "mayor/", "subject": "x", "body": "y"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["to"], "mayor/")


# ---------------------------------------------------------------------------
# Validierung & Kommandobau
# ---------------------------------------------------------------------------

class CommandTests(BridgeTestCase):
    def test_mail_command_argv(self):
        self.runner.when(["gt", "mail", "send", "tiroltourismus/witness"])
        status, _ = self.call(
            "POST", "/api/bridge/mail", token=VALI_TOKEN,
            body={"to": "tiroltourismus/witness",
                  "subject": "Hallo", "body": "Welt"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.runner.last_argv(),
            ["gt", "mail", "send", "tiroltourismus/witness",
             "-s", "Hallo", "-m", "Welt"],
        )

    def test_mail_missing_field_is_400(self):
        status, _ = self.call(
            "POST", "/api/bridge/mail", token=VALI_TOKEN,
            body={"to": "tiroltourismus/witness", "subject": "x"},
        )
        self.assertEqual(status, 400)

    def test_mail_leading_dash_rejected(self):
        status, _ = self.call(
            "POST", "/api/bridge/mail", token=VALI_TOKEN,
            body={"to": "--force", "subject": "x", "body": "y"},
        )
        self.assertEqual(status, 400)

    def test_nudge_command_argv(self):
        self.runner.when(["gt", "nudge", "tiroltourismus/polecat"])
        status, _ = self.call(
            "POST", "/api/bridge/nudge", token=VALI_TOKEN,
            body={"target": "tiroltourismus/polecat", "message": "status?"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.runner.last_argv(),
            ["gt", "nudge", "tiroltourismus/polecat", "status?"],
        )

    def test_nudge_mode_immediate_argv(self):
        self.runner.when(["gt", "nudge", "tiroltourismus/polecat"])
        status, payload = self.call(
            "POST", "/api/bridge/nudge", token=VALI_TOKEN,
            body={"target": "tiroltourismus/polecat", "message": "los",
                  "mode": "immediate"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["mode"], "immediate")
        self.assertEqual(
            self.runner.last_argv(),
            ["gt", "nudge", "tiroltourismus/polecat", "los", "--mode", "immediate"],
        )

    def test_nudge_default_mode_wait_idle(self):
        self.runner.when(["gt", "nudge", "tiroltourismus/polecat"])
        status, payload = self.call(
            "POST", "/api/bridge/nudge", token=VALI_TOKEN,
            body={"target": "tiroltourismus/polecat", "message": "hi"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["mode"], "wait-idle")

    def test_nudge_invalid_mode_is_400(self):
        status, _ = self.call(
            "POST", "/api/bridge/nudge", token=VALI_TOKEN,
            body={"target": "tiroltourismus/polecat", "message": "hi",
                  "mode": "jetzt"},
        )
        self.assertEqual(status, 400)

    def test_sling_prefix_scope_ok(self):
        self.write_routes_jsonl([
            {"prefix": "tt-", "path": "tiroltourismus/mayor/rig"},
        ])
        self.runner.when(["gt", "sling", "tt-123", "tiroltourismus"])
        status, _ = self.call(
            "POST", "/api/bridge/sling", token=VALI_TOKEN,
            body={"bead": "tt-123", "rig": "tiroltourismus"},
        )
        self.assertEqual(status, 200)

    def test_sling_prefix_mismatch_409(self):
        self.write_routes_jsonl([
            {"prefix": "tt-", "path": "tiroltourismus/mayor/rig"},
        ])
        status, payload = self.call(
            "POST", "/api/bridge/sling", token=ADMIN_TOKEN,
            body={"bead": "tt-123", "rig": "andererig"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["bead_rig"], "tiroltourismus")

    def test_sling_scope_denied(self):
        status, _ = self.call(
            "POST", "/api/bridge/sling", token=VALI_TOKEN,
            body={"bead": "ff-1", "rig": "fremd"},
        )
        self.assertEqual(status, 403)

    def test_bead_create_argv_and_cwd(self):
        self.runner.when(["bd", "create", "--json"], rc=0,
                         stdout=json.dumps({"id": "tt-9"}), stderr="")
        status, payload = self.call(
            "POST", "/api/bridge/bead", token=VALI_TOKEN,
            body={"rig": "tiroltourismus", "title": "Titel",
                  "description": "Beschreibung", "priority": 2},
        )
        self.assertEqual(status, 200)
        argv = self.runner.last_argv()
        self.assertIn("--title=Titel", argv)
        self.assertIn("--description=Beschreibung", argv)
        self.assertIn("--priority=2", argv)
        self.assertEqual(self.runner.calls[-1]["cwd"],
                         os.path.join(self.gt_root, "tiroltourismus"))
        self.assertEqual(payload["bead"], {"id": "tt-9"})

    def test_bead_priority_invalid(self):
        status, _ = self.call(
            "POST", "/api/bridge/bead", token=VALI_TOKEN,
            body={"rig": "tiroltourismus", "title": "T",
                  "description": "D", "priority": 9},
        )
        self.assertEqual(status, 400)

    def test_escalations_default_command(self):
        self.runner.when(["gt", "escalate", "list", "--json"], rc=0,
                         stdout=json.dumps({"escalations": []}), stderr="")
        status, payload = self.call("GET", "/api/bridge/escalations",
                                    token=ADMIN_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(self.runner.last_argv(),
                         ["gt", "escalate", "list", "--json"])

    def test_invalid_json_body_400(self):
        headers = {"Authorization": "Bearer " + ADMIN_TOKEN}
        status, _ = self.bridge.request("POST", "/api/bridge/mail",
                                        {}, headers, b"{not json")
        self.assertEqual(status, 400)

    def test_status_text_fallback(self):
        self.runner.when(["gt", "status", "--json"], rc=1, stdout="",
                         stderr="unknown flag")
        self.runner.when(["gt", "status"], rc=0, stdout="Town: x\n", stderr="")
        status, payload = self.call("GET", "/api/bridge/status",
                                    token=ADMIN_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(payload["format"], "text")
        self.assertIn("Town: x", payload["data"])

    def test_command_failure_502(self):
        self.runner.when(["gt", "convoy", "list", "--json"], rc=2,
                         stdout="", stderr="boom")
        status, payload = self.call("GET", "/api/bridge/convoys",
                                    token=ADMIN_TOKEN)
        self.assertEqual(status, 502)


class SubprocessMockTests(BridgeTestCase):
    """Verifiziert, dass wirklich subprocess.run ohne Shell genutzt wird."""

    def test_default_runner_uses_subprocess(self):
        captured = {}

        def fake_run(argv, cwd=None, timeout=None, capture_output=False,
                     text=False, env=None, **kwargs):
            captured["argv"] = argv
            captured["shell"] = kwargs.get("shell", False)
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        import unittest.mock as mock
        with mock.patch("subprocess.run", side_effect=fake_run) as patched:
            bridge = gb.Bridge(self.config)  # default runner
            status, _ = bridge.request(
                "GET", "/api/bridge/status", {},
                {"Authorization": "Bearer " + ADMIN_TOKEN}, b"",
            )
        self.assertEqual(status, 200)
        self.assertTrue(patched.called)
        self.assertFalse(captured.get("shell", False))
        self.assertEqual(captured["argv"][:2], ["gt", "status"])


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

class AuditTests(BridgeTestCase):
    def test_audit_log_written_without_token(self):
        self.call("GET", "/api/bridge/status", token=ADMIN_TOKEN)
        with open(self.config.audit_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("/api/bridge/status", content)
        self.assertIn("admin", content)
        self.assertNotIn(ADMIN_TOKEN, content)

    def test_audit_records_401(self):
        self.call("GET", "/api/bridge/status")
        with open(self.config.audit_path, "r", encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(records[-1]["status"], 401)


# ---------------------------------------------------------------------------
# Outbound: Rate-Limit, Dedupe, Dry-run, Slack
# ---------------------------------------------------------------------------

class RecordingPoster:
    def __init__(self):
        self.calls = []

    def __call__(self, url, payload, token):
        self.calls.append({"url": url, "payload": payload, "token": token})
        return "ok"


class OutboundTests(BridgeTestCase):
    def test_rate_limiter(self):
        state = gb.StateStore(self.config.state_path)
        limiter = gb.RateLimiter(state, limit=2, window=600)
        now = 1000.0
        self.assertTrue(limiter.allow("#c", now=now))
        self.assertTrue(limiter.allow("#c", now=now + 1))
        self.assertFalse(limiter.allow("#c", now=now + 2))
        # Nach Fensterablauf wieder frei.
        self.assertTrue(limiter.allow("#c", now=now + 700))

    def test_dedupe_store(self):
        state = gb.StateStore(self.config.state_path)
        dedupe = gb.DedupeStore(state)
        self.assertTrue(dedupe.first_seen("e1"))
        self.assertFalse(dedupe.first_seen("e1"))
        self.assertTrue(dedupe.first_seen("e2"))

    def test_notifier_dry_run(self):
        poster = RecordingPoster()
        notifier = gb.SlackNotifier(self.config, poster=poster)
        result = notifier.send("#c", "text")
        self.assertEqual(result, "dry-run")
        self.assertEqual(poster.calls, [])

    def test_notifier_webhook(self):
        poster = RecordingPoster()
        config = gb.Config(
            gt_root=self.gt_root, env_file=self.env_file,
            audit_log=os.path.join(self.root, "audit.jsonl"),
            routes_file=self.config.routes_path,
            state_file=self.config.state_path,
            slack_webhook_url="https://hooks.example/x",
        )
        notifier = gb.SlackNotifier(config, poster=poster)
        result = notifier.send("#c", "text")
        self.assertEqual(result, "webhook")
        self.assertEqual(poster.calls[0]["url"], "https://hooks.example/x")
        self.assertEqual(poster.calls[0]["payload"]["text"], "text")

    def test_notifier_bot_token(self):
        poster = RecordingPoster()
        config = gb.Config(
            gt_root=self.gt_root, env_file=self.env_file,
            audit_log=os.path.join(self.root, "audit.jsonl"),
            routes_file=self.config.routes_path,
            state_file=self.config.state_path,
            slack_bot_token="xoxb-abc",
        )
        notifier = gb.SlackNotifier(config, poster=poster)
        result = notifier.send("#c", "text")
        self.assertEqual(result, "bot")
        self.assertEqual(poster.calls[0]["token"], "xoxb-abc")
        self.assertIn("chat.postMessage", poster.calls[0]["url"])

    def _poller(self, poster):
        self.config.slack_webhook_url = "https://hooks.example/test"
        notifier = gb.SlackNotifier(self.config, poster=poster)
        self.bridge.notifier = notifier
        return gb.OutboundPoller(self.bridge, interval=1,
                                 stop_event=__import__("threading").Event())

    def test_handle_event_routes_and_mentions(self):
        self.write_slack_routes({
            "tiroltourismus": {"channel": "#tirol-content",
                               "owner_prefix": "FUER-VALI"},
            "_town": {"channel": "#town", "owner_prefix": "FUER-AMI"},
        })
        poster = RecordingPoster()
        poller = self._poller(poster)
        handled = poller.handle_event({
            "ts": "2026-01-01T00:00:00Z",
            "type": "escalation_sent",
            "actor": "tiroltourismus/witness",
            "payload": {"rig": "tiroltourismus", "escalation": "hq-1"},
        })
        self.assertTrue(handled)
        self.assertEqual(poster.calls[0]["payload"]["channel"], "#tirol-content")
        text = poster.calls[0]["payload"]["text"]
        self.assertIn("FUER-VALI", text)
        self.assertIn("FUER-AMI", text)

    def test_handle_event_dedupe(self):
        self.write_slack_routes({
            "tiroltourismus": {"channel": "#c", "owner_prefix": "X"},
        })
        poster = RecordingPoster()
        poller = self._poller(poster)
        event = {"type": "merged", "actor": "tiroltourismus/polecat",
                 "payload": {"rig": "tiroltourismus", "mr": "mr-1"}}
        self.assertTrue(poller.handle_event(event))
        self.assertFalse(poller.handle_event(event))
        self.assertEqual(len(poster.calls), 1)

    def test_handle_event_rate_limit(self):
        self.write_slack_routes({
            "tiroltourismus": {"channel": "#c", "owner_prefix": "X"},
        })
        self.config.rate_limit = 1
        self.bridge.rate = gb.RateLimiter(self.bridge.state, 1, 600)
        poster = RecordingPoster()
        poller = self._poller(poster)
        ok1 = poller.handle_event({
            "type": "merged", "actor": "tiroltourismus/a",
            "payload": {"rig": "tiroltourismus", "mr": "1"}})
        ok2 = poller.handle_event({
            "type": "merged", "actor": "tiroltourismus/a",
            "payload": {"rig": "tiroltourismus", "mr": "2"}})
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertEqual(len(poster.calls), 1)

    def test_handle_event_unknown_type_ignored(self):
        poster = RecordingPoster()
        poller = self._poller(poster)
        self.assertFalse(poller.handle_event({"type": "something_else"}))
        self.assertEqual(poster.calls, [])

    def test_read_new_events_tail(self):
        events_path = self.config.events_file
        with open(events_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "done", "payload": {"bead": "x-1"}}) + "\n")
        poster = RecordingPoster()
        poller = self._poller(poster)
        events = poller.read_new_events()
        self.assertEqual(len(events), 1)
        # Offset wurde gespeichert -> zweiter Aufruf liest nichts.
        self.assertEqual(poller.read_new_events(), [])

    def test_event_id_uses_payload_identifier(self):
        eid = gb.OutboundPoller._event_id(
            {"type": "merged", "ts": "t", "actor": "a"},
            "pr_merged", {"mr": "mr-7"})
        self.assertEqual(eid, "pr_merged:mr:mr-7")

    def test_bead_route_event_derives_rig_from_prefix(self):
        self.write_routes_jsonl([
            {"prefix": "tt-", "path": "tiroltourismus/mayor/rig"},
        ])
        self.write_slack_routes({
            "tiroltourismus": {"channel": "#tirol-content", "owner_prefix": "V"},
        })
        poster = RecordingPoster()
        poller = self._poller(poster)
        handled = poller.handle_event({
            "type": "done", "actor": "town",
            "payload": {"bead": "tt-42"},
        })
        self.assertTrue(handled)
        self.assertEqual(poster.calls[0]["payload"]["channel"], "#tirol-content")


class TokenStoreTests(unittest.TestCase):
    def test_admin_and_scopes(self):
        store = gb.TokenStore.from_mapping({
            "BRIDGE_TOKEN_ADMIN": "a",
            "BRIDGE_TOKEN_X": "x",
            "BRIDGE_SCOPE_X": "rig1,rig2",
        })
        admin = store.authenticate("Bearer a")
        self.assertIsNotNone(admin)
        self.assertTrue(admin.admin)
        self.assertTrue(admin.allows_rig("anything"))
        self.assertTrue(admin.allows_town())

        x = store.authenticate("Bearer x")
        self.assertFalse(x.admin)
        self.assertTrue(x.allows_rig("rig1"))
        self.assertFalse(x.allows_rig("rig3"))
        self.assertFalse(x.allows_town())

        self.assertIsNone(store.authenticate("Bearer nope"))
        self.assertIsNone(store.authenticate("Basic a"))
        self.assertIsNone(store.authenticate(None))


if __name__ == "__main__":
    unittest.main()
