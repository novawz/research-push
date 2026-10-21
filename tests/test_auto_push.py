import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# Import with no production database configured. Tests use temporary SQLite files
# or mocked PostgreSQL connections and never send external requests.
with patch.dict(os.environ, {"AUTO_PUSH_DATABASE_URL": "", "AUTO_PUSH_MULTIUSER": "0"}):
    import app
    import daily_push


NOW = datetime(2026, 10, 19, 13, 7, tzinfo=timezone.utc)  # Monday, 09:07 New York


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.db = Path(directory) / "subscriptions.sqlite3"
        for name, value in {
            "AUTO_PUSH_ENABLED": True,
            "AUTO_PUSH_MULTIUSER": True,
            "AUTO_PUSH_BACKEND": "sqlite",
            "AUTO_PUSH_DB_FILE": self.db,
        }.items():
            self.stack.enter_context(patch.object(app, name, value))
        self.stack.enter_context(patch.object(app, "now_utc", return_value=NOW))
        self.stack.enter_context(patch.dict(os.environ, {"AUTO_PUSH_GRACE_MINUTES": "15"}))
        self.stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden in tests")))
        app.init_auto_push_db()

    def add_subscription(self, sid, *, enabled=1, schedule="daily", push_time="09:00",
                         tz="America/New_York", last_date=""):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO auto_push_subscriptions VALUES (?, ?, ?, 7, ?, ?, ?, ?, '', '')",
                (sid, enabled, schedule, push_time, tz,
                 json.dumps({"enable_webhook_push": True, "webhook_url": "https://example.invalid/webhook"}),
                 last_date),
            )

    def states(self):
        with sqlite3.connect(self.db) as conn:
            return conn.execute(
                "SELECT subscriber_id, last_run_local_date, last_error FROM auto_push_subscriptions ORDER BY subscriber_id"
            ).fetchall()

    def run_cli(self, *args):
        output = io.StringIO()
        with patch("sys.argv", ["daily_push.py", *args]), redirect_stdout(output), redirect_stderr(output):
            daily_push.main()
        return output.getvalue()

    def test_check_and_dry_run_are_read_only_and_only_report_counts(self):
        self.add_subscription("daily")
        self.add_subscription("weekly", schedule="weekly_monday")
        self.add_subscription("disabled", enabled=0)
        self.add_subscription("already-sent", last_date="2026-10-19")
        self.add_subscription("later", push_time="10:00")
        before = self.db.read_bytes()
        with ExitStack() as stack:
            for module, names in [
                (app, ["init_auto_push_db", "claim_auto_push_slot"]),
                (daily_push, ["claim_auto_push_slot", "mark_auto_push_run", "release_auto_push_slot", "run_push_once"]),
            ]:
                for name in names:
                    stack.enter_context(patch.object(module, name, side_effect=AssertionError("Write/delivery forbidden in check")))
            for flag in ["--check", "--dry-run"]:
                output = self.run_cli(flag)
                self.assertIn("due=2", output)
                self.assertNotIn("example.invalid", output)
                self.assertNotIn("weekly", output)
                self.assertEqual(before, self.db.read_bytes())

    def test_missing_database_fails_without_creating_file(self):
        missing = self.db.parent / "missing.sqlite3"
        with patch.object(app, "AUTO_PUSH_DB_FILE", missing):
            with self.assertRaises(SystemExit) as raised:
                self.run_cli("--check")
        self.assertIn("database check failed", str(raised.exception))
        self.assertFalse(missing.exists())

    def test_missing_schema_fails_without_initializing_it(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("DROP TABLE auto_push_subscriptions")
        before = self.db.read_bytes()
        with self.assertRaises(SystemExit):
            self.run_cli("--check")
        self.assertEqual(before, self.db.read_bytes())

    def test_limit_only_claims_subscriptions_it_processes(self):
        self.add_subscription("a")
        self.add_subscription("b")
        with patch.object(daily_push, "run_push_once", return_value=(True, "ok")) as delivery:
            self.run_cli("--all-due", "--limit", "1")
        delivery.assert_called_once()
        self.assertEqual(self.states(), [("a", "2026-10-19", ""), ("b", "", "")])

    def test_interruption_does_not_claim_later_subscriptions(self):
        self.add_subscription("a")
        self.add_subscription("b")
        with patch.object(daily_push, "run_push_once", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_cli("--all-due")
        self.assertEqual(self.states(), [("a", "2026-10-19", ""), ("b", "", "")])

    def test_unexpected_processing_error_releases_slot_without_logging_details(self):
        self.add_subscription("a")
        with patch.object(daily_push, "run_push_once", side_effect=RuntimeError("fake-sensitive-value")):
            with self.assertRaises(SystemExit) as raised:
                self.run_cli("--all-due")
        self.assertIn("Completed with failures", str(raised.exception))
        sid, last_date, last_error = self.states()[0]
        self.assertEqual((sid, last_date), ("a", ""))
        self.assertNotIn("fake-sensitive-value", last_error)

    def test_lost_claim_race_does_not_deliver(self):
        self.add_subscription("a")
        due = app.list_due_auto_push_subscriptions()
        self.assertTrue(app.claim_auto_push_slot("a", "2026-10-19"))
        with patch.object(daily_push, "list_due_auto_push_subscriptions", return_value=due), \
                patch.object(daily_push, "run_push_once") as delivery:
            self.run_cli("--all-due")
        delivery.assert_not_called()

    def test_claim_database_failure_exits_before_delivery(self):
        self.add_subscription("a")
        with patch.object(daily_push, "claim_auto_push_slot", side_effect=app.AutoPushDatabaseError("Cannot claim auto-push subscription.")), \
                patch.object(daily_push, "run_push_once") as delivery:
            with self.assertRaises(SystemExit):
                self.run_cli("--all-due")
        delivery.assert_not_called()

    def test_timezone_weekly_and_same_day_dedup_rules_are_preserved(self):
        self.add_subscription("daily")
        self.add_subscription("weekly", schedule="weekly_monday")
        self.add_subscription("utc", tz="UTC", push_time="13:00")
        self.add_subscription("already-sent", last_date="2026-10-19")
        monday = {item["subscriber_id"] for item in app.list_due_auto_push_subscriptions(NOW)}
        tuesday = {item["subscriber_id"] for item in app.list_due_auto_push_subscriptions(NOW.replace(day=20))}
        self.assertEqual(monday, {"daily", "weekly", "utc"})
        self.assertEqual(tuesday, {"daily", "utc", "already-sent"})

    def test_grace_window_can_be_widened_without_changing_push_time(self):
        self.add_subscription("daily")
        delayed = NOW.replace(minute=40)
        self.assertEqual(app.list_due_auto_push_subscriptions(delayed), [])
        with patch.dict(os.environ, {"AUTO_PUSH_GRACE_MINUTES": "60"}):
            due = app.list_due_auto_push_subscriptions(delayed)
            self.assertEqual(len(due), 1)
            self.assertEqual(due[0]["local_time"], "09:00")
            self.assertEqual(app.list_due_auto_push_subscriptions(NOW.replace(hour=12)), [])
            self.assertEqual(app.list_due_auto_push_subscriptions(NOW.replace(hour=14, minute=1)), [])


class PostgresTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.driver = MagicMock()
        self.conn = self.driver.connect.return_value
        self.conn.__enter__.return_value = self.conn
        self.cursor = self.conn.cursor.return_value.__enter__.return_value
        self.cursor.fetchall.return_value = []
        self.cursor.rowcount = 1
        for name, value in {
            "AUTO_PUSH_ENABLED": True,
            "AUTO_PUSH_MULTIUSER": True,
            "AUTO_PUSH_BACKEND": "postgres",
            "AUTO_PUSH_DATABASE_URL": "postgresql://test.invalid/example",
            "psycopg2": self.driver,
        }.items():
            self.stack.enter_context(patch.object(app, name, value))

    def test_read_connection_has_timeouts_readonly_transaction_and_is_closed(self):
        self.assertEqual(app.list_due_auto_push_subscriptions(NOW), [])
        self.driver.connect.assert_called_once_with("postgresql://test.invalid/example", connect_timeout=10)
        self.conn.set_session.assert_called_once_with(readonly=True)
        statements = [call.args[0] for call in self.cursor.execute.call_args_list]
        self.assertEqual(statements[:2], ["SET LOCAL statement_timeout = '15s'", "SET LOCAL lock_timeout = '5s'"])
        self.assertTrue(statements[2].strip().startswith("SELECT"))
        self.assertEqual(len(statements), 3)
        self.conn.close.assert_called_once()

    def test_select_failure_is_nonzero_and_connection_details_are_not_exposed(self):
        self.cursor.execute.side_effect = RuntimeError("postgresql://user:fake-sensitive-value@test.invalid/db")
        for args in [["--check"], ["--all-due"]]:
            with patch("sys.argv", ["daily_push.py", *args]):
                with self.assertRaises(SystemExit) as raised:
                    daily_push.main()
            self.assertNotIn("fake-sensitive-value", str(raised.exception))
            self.assertNotEqual(raised.exception.code, 0)
        self.assertEqual(self.conn.close.call_count, 2)

    def test_connect_failure_is_nonzero_and_sanitized(self):
        self.driver.connect.side_effect = RuntimeError("fake-sensitive-value")
        with patch("sys.argv", ["daily_push.py", "--check"]):
            with self.assertRaises(SystemExit) as raised:
                daily_push.main()
        self.assertNotIn("fake-sensitive-value", str(raised.exception))

    def test_missing_driver_is_not_reported_as_no_due_subscriptions(self):
        with patch.object(app, "psycopg2", None):
            with self.assertRaises(app.AutoPushDatabaseError):
                app.list_due_auto_push_subscriptions(NOW)

    def test_check_failure_reports_only_safe_diagnostics(self):
        error = sqlite3.OperationalError(
            "password authentication failed; postgresql://private-user:fake-password@private-host/private-db"
        )
        error.pgcode = "28P01"
        self.driver.connect.side_effect = error
        with patch("sys.argv", ["daily_push.py", "--check"]):
            with self.assertRaises(SystemExit) as raised:
                daily_push.main()
        output = str(raised.exception)
        self.assertIn("type=OperationalError; sqlstate=28P01; category=authentication", output)
        for value in ["private-user", "fake-password", "private-host", "private-db", "postgresql://"]:
            self.assertNotIn(value, output)

    def test_write_operations_use_the_same_bounded_connection_helper(self):
        operations = [
            lambda: app.init_auto_push_db(),
            lambda: app.upsert_auto_push_subscription("a", {}, True),
            lambda: app.claim_auto_push_slot("a", "2026-10-19"),
            lambda: app.mark_auto_push_run("a", "2026-10-19"),
            lambda: app.release_auto_push_slot("a", "2026-10-19"),
        ]
        for operation in operations:
            with self.subTest(operation=operation):
                self.driver.reset_mock()
                operation()
                self.assertGreater(self.driver.connect.call_count, 0)
                for call in self.driver.connect.call_args_list:
                    self.assertEqual(call.kwargs["connect_timeout"], 10)
                self.assertEqual(self.conn.close.call_count, self.driver.connect.call_count)
                for call in self.conn.set_session.call_args_list:
                    self.assertEqual(call.kwargs, {"readonly": False})
                statements = [call.args[0] for call in self.cursor.execute.call_args_list]
                self.assertEqual(statements.count("SET LOCAL statement_timeout = '15s'"), self.driver.connect.call_count)
                self.assertEqual(statements.count("SET LOCAL lock_timeout = '5s'"), self.driver.connect.call_count)


class DiagnosticTests(unittest.TestCase):
    def test_message_categories_never_return_connection_details(self):
        cases = [
            ("password authentication failed", "authentication"),
            ("Tenant or user not found", "authentication"),
            ("could not translate host name", "dns"),
            ("SSL connection error: certificate verify failed", "tls"),
            ("Connection refused", "refused_or_unreachable"),
            ("Network is unreachable", "refused_or_unreachable"),
            ("timeout expired", "timeout"),
            ("canceling statement due to lock timeout", "lock_timeout"),
            ("no such table", "schema"),
            ("attempt to write a readonly database", "read_only"),
            ("permission denied", "permission"),
            ("Max client connections reached", "connection_limit"),
            ("database system is starting up", "database_unavailable"),
            ("server closed the connection unexpectedly", "connection_failure"),
            ("invalid dsn: missing '='", "configuration"),
            ("unable to open database file", "file_unavailable"),
            ("unrecognized error", "unknown"),
        ]
        for message, category in cases:
            with self.subTest(category=category, message=message):
                error = sqlite3.OperationalError(
                    message + " postgresql://private-user:fake-password@private-host/private-db"
                )
                self.assertEqual(
                    app.auto_push_database_diagnostic(error),
                    f"type=OperationalError; sqlstate=unavailable; category={category}",
                )

    def test_sqlstate_classification_and_validation(self):
        cases = [
            ("28P01", "authentication"), ("28000", "authentication"),
            ("42P01", "schema"), ("3F000", "schema"), ("42703", "schema"),
            ("25006", "read_only"), ("42501", "permission"),
            ("55P03", "lock_timeout"), ("53300", "connection_limit"),
            ("3D000", "database_missing"), ("57P03", "database_unavailable"),
            ("25001", "transaction_state"), ("08006", "connection_failure"),
            ("57014", "query_canceled"),
        ]
        for sqlstate, category in cases:
            with self.subTest(sqlstate=sqlstate):
                error = sqlite3.OperationalError("fake-password private-host private-user")
                error.pgcode = sqlstate
                self.assertEqual(app.auto_push_database_diagnostic(error),
                                 f"type=OperationalError; sqlstate={sqlstate}; category={category}")
        for invalid in [None, 28000, "28p01", "28P01\n", "private-password", "28P01 extra"]:
            error = sqlite3.OperationalError("fake-password")
            error.pgcode = invalid
            self.assertEqual(app.auto_push_database_diagnostic(error),
                             "type=OperationalError; sqlstate=unavailable; category=unknown")

    def test_driver_subclass_and_unknown_type_names_are_not_echoed(self):
        private_type = type("fake-password-private-host", (sqlite3.OperationalError,), {})
        self.assertEqual(app.auto_push_database_diagnostic(private_type("private-user")),
                         "type=OperationalError; sqlstate=unavailable; category=unknown")

    def test_broken_exception_metadata_cannot_break_safe_diagnostics(self):
        class BrokenError(Exception):
            @property
            def pgcode(self):
                raise RuntimeError("fake-password")

            def __str__(self):
                raise RuntimeError("fake-password")

        self.assertEqual(app.auto_push_database_diagnostic(BrokenError()),
                         "type=Exception; sqlstate=unavailable; category=unknown")


if __name__ == "__main__":
    unittest.main()
