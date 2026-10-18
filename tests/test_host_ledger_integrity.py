import base64
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentfanout.host_runtime import HostLedger, LEDGER_CHAIN_GENESIS_SHA256


def canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class HostLedgerIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.sqlite3"
        self.ledger = HostLedger(self.path)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_events_form_independent_per_run_hash_chains(self):
        self.ledger.append("run-a", "started", {"value": 1}, "task-a")
        self.ledger.append("run-b", "started", {"value": 2}, "task-b")
        self.ledger.append("run-a", "finished", {"value": 3}, "task-a")

        run_a = self.ledger.events("run-a")
        run_b = self.ledger.events("run-b")
        self.assertEqual(run_a[0]["previous_event_sha256"], LEDGER_CHAIN_GENESIS_SHA256)
        self.assertEqual(run_a[1]["previous_event_sha256"], run_a[0]["event_sha256"])
        self.assertEqual(run_b[0]["previous_event_sha256"], LEDGER_CHAIN_GENESIS_SHA256)
        self.assertEqual(len(run_a[0]["event_sha256"]), 64)
        self.assertEqual(
            self.ledger.latest_task_event("run-a", "task-a", "finished"),
            {"value": 3},
        )

    def test_update_and_delete_are_denied_by_sqlite_triggers(self):
        self.ledger.append("run-a", "started", {"value": 1})

        with self.assertRaisesRegex(sqlite3.IntegrityError, "events_append_only_update_denied"):
            self.ledger.db.execute("UPDATE events SET event_type='changed'")
        self.ledger.db.rollback()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "events_append_only_delete_denied"):
            self.ledger.db.execute("DELETE FROM events")
        self.ledger.db.rollback()

        self.assertEqual(len(self.ledger.events("run-a")), 1)

    def test_raw_result_credential_key_schemes_and_cookies_never_reach_sqlite_or_wal(self):
        key_cases = {
            "openai_api_key": "fixture-openai-" + ("a" * 32),
            "oauth_client_secret": "fixture-oauth-" + ("b" * 32),
            "database_url": "postgresql" + "://" + "fixture-user:fixture-pass@db.invalid/app",
            "primaryConnectionString": "Server=db.invalid;Password=" + ("c" * 32),
            "sentry_dsn": "https://fixture-public:fixture-secret@sentry.invalid/1",
            "billing_api_key": 1234567890123456,
            "database_password": -987654321,
            "access_pin": 4321,
            "refresh_token": "short",
            "otp": 654321,
            "recovery_code": "RC-7X9Q",
        }
        standalone = (
            "Be" + "arer " + ("d" * 40),
            "Ba" + "sic " + base64.b64encode(b"fixture-user:fixture-password").decode(),
            "Cook" + "ie: session=" + ("e" * 40),
            "Set-" + "Cookie: sid=" + ("f" * 40) + "; HttpOnly",
        )
        secrets = [str(value) for value in key_cases.values()] + list(standalone)
        persistence_needles = [
            str(value) if isinstance(value, str) else f'"{key}":{{"value":{value}}}'
            for key, value in key_cases.items()
        ] + list(standalone)
        payloads = [
            {
                "result": {
                    "summary": "bounded result", "evidence": [{key: {"value": secret}}],
                }
            }
            for key, secret in key_cases.items()
        ] + [
            {"result": {"summary": secret, "evidence": []}}
            for secret in standalone
        ]
        for index, payload in enumerate(payloads):
            event_type = "accepted_result_packet" if index % 2 == 0 else "validator_result"
            event_payload = payload if event_type == "accepted_result_packet" else payload["result"]
            with self.subTest(event_type=event_type, index=index):
                with self.assertRaisesRegex(ValueError, "result_packet_sensitive_material") as rejected:
                    self.ledger.append("run-sensitive", event_type, event_payload, "task-sensitive")
                error = str(rejected.exception)
                self.assertTrue(all(secret not in error for secret in secrets))
        self.assertEqual(self.ledger.events("run-sensitive"), [])
        self.ledger.db.execute("PRAGMA wal_checkpoint(FULL)")
        persistent = b"".join(
            candidate.read_bytes()
            for candidate in (
                self.path,
                Path(str(self.path) + "-wal"),
                Path(str(self.path) + "-shm"),
                self.ledger.operation_lock_path,
            )
            if candidate.exists() and candidate.is_file()
        )
        for needle in persistence_needles:
            self.assertNotIn(needle.encode(), persistent)

    def test_verified_read_detects_database_owner_payload_corruption(self):
        self.ledger.append("run-a", "finished", {"value": "original"}, "task-a")

        # Model the documented database-owner threat: the owner can remove the
        # trigger, but cannot make the stale chain validate accidentally.
        tampered = canonical({"value": "tampered"})
        self.ledger.db.execute("DROP TRIGGER events_deny_update")
        self.ledger.db.execute(
            "UPDATE events SET payload_json=?,payload_sha256=? WHERE run_id=?",
            (tampered, hashlib.sha256(tampered.encode("utf-8")).hexdigest(), "run-a"),
        )
        self.ledger.db.commit()

        with self.assertRaisesRegex(ValueError, "ledger_event_chain_invalid"):
            self.ledger.events("run-a")
        with self.assertRaisesRegex(ValueError, "ledger_event_chain_invalid"):
            self.ledger.latest_task_event("run-a", "task-a", "finished")

    def test_legacy_rows_are_backfilled_before_triggers_are_installed(self):
        self.ledger.close()
        payload = canonical({"legacy": True})
        connection = sqlite3.connect(self.path)
        connection.executescript(
            """
            DROP TRIGGER IF EXISTS events_deny_update;
            DROP TRIGGER IF EXISTS events_deny_delete;
            DROP TABLE events;
            CREATE TABLE events (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id TEXT NOT NULL UNIQUE,
              run_id TEXT NOT NULL,
              task_id TEXT,
              event_type TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              payload_sha256 TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO events(event_id,run_id,task_id,event_type,occurred_at,payload_json,payload_sha256) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "event_legacy",
                "run-legacy",
                "task-legacy",
                "legacy",
                "2026-07-10T00:00:00Z",
                payload,
                hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            ),
        )
        connection.commit()
        connection.close()
        self.path.chmod(0o600)

        self.ledger = HostLedger(self.path)
        migrated = self.ledger.events("run-legacy")
        self.assertEqual(migrated[0]["previous_event_sha256"], LEDGER_CHAIN_GENESIS_SHA256)
        self.assertEqual(len(migrated[0]["event_sha256"]), 64)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "events_append_only_update_denied"):
            self.ledger.db.execute("UPDATE events SET event_type='changed'")
        self.ledger.db.rollback()


if __name__ == "__main__":
    unittest.main()
