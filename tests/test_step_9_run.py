"""Pipeline order, clock semantics, and observability gates."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "block-7-run"))
sys.path.insert(0, str(ROOT / "block-1-setup"))

import config  # noqa: E402
import db  # noqa: E402
import run as pipeline  # noqa: E402


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "run.db"
        self.log_path = Path(self.temp.name) / "runs.jsonl"
        db.init_db(self.path)
        self.log_patch = patch.object(config, "RUN_LOG_PATH", self.log_path)
        self.log_patch.start()
        self.tag_limits = []
        self.delivery_emails = []

    def tearDown(self):
        self.log_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def fetch_stage(events, conn, run_id):
        events.append("fetch")
        conn.execute(
            "UPDATE run SET fetch_cutoff_at='2026-08-16 10:00:00' WHERE id=?",
            (run_id,),
        )
        conn.commit()
        return SimpleNamespace(shows=240, after_filter=747)

    @staticmethod
    def tag_result():
        return SimpleNamespace(
            tagged=3,
            untagged_left=2,
            abandoned=1,
            tokens_used=1234,
            rows=[(1, 50, [], "a"), (2, 70, [], "b"), (3, 90, [], "c")],
        )

    def patches(self, conn, events, delivery=None):
        delivery = delivery or SimpleNamespace(sent=2, failed=0)

        def fetch_fake(target, run_id):
            return self.fetch_stage(events, target, run_id)

        def tag_fake(target, limit=None, dry_run=False):
            events.append("tag")
            self.tag_limits.append(limit)
            return self.tag_result()

        def curate_fake(target, run_id, previous_cutoff=None):
            events.append("curate")
            return SimpleNamespace(counts_by_topic={"technology-ai": 2, "travel": 0})

        def send_fake(target, run_id, dry_run=False, email=None):
            events.append("send")
            self.delivery_emails.append(email)
            return delivery

        return (
            patch.object(pipeline.fetch, "fetch_all", side_effect=fetch_fake),
            patch.object(pipeline.tag, "tag_all", side_effect=tag_fake),
            patch.object(pipeline.curate, "curate", side_effect=curate_fake),
            patch.object(pipeline.email_out, "deliver_all", side_effect=send_fake),
        )

    def test_success_runs_in_order_updates_row_and_logs_all_metrics(self):
        events = []
        with db.session(self.path) as conn:
            conn.execute(
                "INSERT INTO subscriber (email, unsub_token, confirm_token, status) "
                "VALUES ('active@example.com', 'u', 'c', 'active')"
            )
            fetch_patch, tag_patch, curate_patch, send_patch = self.patches(conn, events)
            with fetch_patch, tag_patch, curate_patch, send_patch:
                metrics = pipeline.execute(
                    conn,
                    tag_limit=100,
                    delivery_email="active@example.com",
                )
            row = conn.execute("SELECT * FROM run WHERE id=?", (metrics.run_id,)).fetchone()

        self.assertEqual(events, ["fetch", "tag", "curate", "send"])
        self.assertEqual(self.tag_limits, [100])
        self.assertEqual(self.delivery_emails, ["active@example.com"])
        self.assertEqual(metrics.status, "ok")
        self.assertEqual((metrics.score_p50, metrics.score_p90), (70, 90))
        self.assertEqual((row["status"], row["fetched"], row["tagged"]), ("ok", 747, 3))
        self.assertIsNotNone(row["finished_at"])
        logged = json.loads(self.log_path.read_text().strip())
        required = {
            "run_id", "fetch_cutoff_at", "shows", "fetched", "tagged",
            "untagged_left", "tag_abandoned", "tokens_used", "score_p50",
            "score_p90", "picks_by_topic", "subscribers", "emails_sent",
            "emails_failed", "status",
        }
        self.assertTrue(required <= logged.keys())
        self.assertEqual(logged["subscribers"], 1)

    def test_pipeline_failure_stops_send_and_does_not_advance_clock(self):
        for failed_stage in ("fetch", "tag", "curate"):
            with self.subTest(stage=failed_stage):
                nested = Path(self.temp.name) / f"{failed_stage}.db"
                db.init_db(nested)
                with db.session(nested) as conn:
                    conn.execute(
                        "INSERT INTO run (fetch_cutoff_at, status, finished_at) "
                        "VALUES ('2026-08-10 10:00:00', 'ok', datetime('now'))"
                    )
                    events = []

                    def fetch_fake(target, run_id):
                        events.append("fetch")
                        if failed_stage == "fetch":
                            raise RuntimeError("fetch broke")
                        return self.fetch_stage([], target, run_id)

                    def tag_fake(*_args, **_kwargs):
                        events.append("tag")
                        if failed_stage == "tag":
                            raise RuntimeError("tag broke")
                        return self.tag_result()

                    def curate_fake(*_args, **_kwargs):
                        events.append("curate")
                        if failed_stage == "curate":
                            raise RuntimeError("curate broke")
                        return SimpleNamespace(counts_by_topic={})

                    with patch.object(pipeline.fetch, "fetch_all", side_effect=fetch_fake), patch.object(
                        pipeline.tag, "tag_all", side_effect=tag_fake
                    ), patch.object(pipeline.curate, "curate", side_effect=curate_fake), patch.object(
                        pipeline.email_out, "deliver_all"
                    ) as send:
                        with self.assertRaises(RuntimeError):
                            pipeline.execute(conn)
                    latest = conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT 1").fetchone()
                    cutoff = db.last_good_cutoff(conn)
                self.assertEqual(latest["status"], "failed")
                self.assertEqual(cutoff, "2026-08-10 10:00:00")
                send.assert_not_called()

    def test_cleanup_rollback_failure_does_not_mask_pipeline_error(self):
        class RollbackFailingConnection:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def rollback(self):
                raise RuntimeError("cleanup stream expired")

        with db.session(self.path) as conn:
            proxy = RollbackFailingConnection(conn)
            with patch.object(
                pipeline.fetch,
                "fetch_all",
                side_effect=RuntimeError("original fetch failure"),
            ), patch.object(pipeline.email_out, "deliver_all") as send:
                with self.assertRaisesRegex(RuntimeError, "original fetch failure"):
                    pipeline.execute(proxy)
            latest = conn.execute("SELECT status FROM run ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(latest["status"], "failed")
        send.assert_not_called()

    def test_fetch_transaction_is_committed_before_tagging(self):
        observed_cutoffs = []

        def fetch_without_commit(target, run_id):
            target.execute(
                "UPDATE run SET fetch_cutoff_at='2026-08-16 10:00:00' WHERE id=?",
                (run_id,),
            )
            return SimpleNamespace(shows=240, after_filter=10)

        def observe_from_separate_connection(*_args, **_kwargs):
            with db.session(self.path) as observer:
                observed_cutoffs.append(
                    observer.execute(
                        "SELECT fetch_cutoff_at FROM run ORDER BY id DESC LIMIT 1"
                    ).fetchone()[0]
                )
            return self.tag_result()

        with db.session(self.path) as conn, patch.object(
            pipeline.fetch, "fetch_all", side_effect=fetch_without_commit
        ), patch.object(
            pipeline.tag, "tag_all", side_effect=observe_from_separate_connection
        ), patch.object(
            pipeline.curate,
            "curate",
            return_value=SimpleNamespace(counts_by_topic={}),
        ), patch.object(
            pipeline.email_out,
            "deliver_all",
            return_value=SimpleNamespace(sent=0, failed=0),
        ):
            pipeline.execute(conn)
        self.assertEqual(observed_cutoffs, ["2026-08-16 10:00:00"])

    def test_delivery_failure_is_partial_and_advances_clock(self):
        events = []
        delivery = SimpleNamespace(sent=2, failed=1)
        with db.session(self.path) as conn:
            patches = self.patches(conn, events, delivery)
            with patches[0], patches[1], patches[2], patches[3]:
                metrics = pipeline.execute(conn)
            latest = conn.execute("SELECT * FROM run WHERE id=?", (metrics.run_id,)).fetchone()
            cutoff = db.last_good_cutoff(conn)
        self.assertEqual((metrics.status, latest["status"]), ("partial", "partial"))
        self.assertEqual(cutoff, "2026-08-16 10:00:00")
        self.assertEqual(events[-1], "send")

    def test_skip_flags_make_no_fetch_or_tag_calls(self):
        with db.session(self.path) as conn:
            conn.execute(
                "INSERT INTO run (fetch_cutoff_at, fetched, status) "
                "VALUES ('2026-08-16 10:00:00', 25, 'failed')"
            )
            with patch.object(pipeline.fetch, "fetch_all") as fetch_call, patch.object(
                pipeline.tag, "tag_all"
            ) as tag_call, patch.object(
                pipeline.curate,
                "curate",
                return_value=SimpleNamespace(counts_by_topic={"science": 0}),
            ), patch.object(
                pipeline.email_out,
                "deliver_all",
                return_value=SimpleNamespace(sent=0, failed=0),
            ):
                metrics = pipeline.execute(conn, skip_fetch=True, skip_tag=True)
        fetch_call.assert_not_called()
        tag_call.assert_not_called()
        self.assertEqual((metrics.status, metrics.fetched), ("ok", 25))

    def test_vercel_uses_zero_config_root_flask_entrypoint(self):
        entrypoint = (ROOT / "index.py").read_text(encoding="utf-8")
        self.assertIn("from app import app", entrypoint)
        self.assertFalse((ROOT / "vercel.json").exists())

    def test_manual_workflow_defaults_safe_and_supports_tag_limit(self):
        workflow = (ROOT / ".github/workflows/run.yml").read_text(encoding="utf-8")
        self.assertIn("tag_limit:", workflow)
        self.assertIn("recipient:", workflow)
        self.assertIn("args+=(--tag-limit", workflow)
        self.assertIn("args+=(--email", workflow)
        self.assertIn('description: "Preview without sending or persisting tags"', workflow)
        dry_run_section = workflow.split("dry_run:", 1)[1].split("tag_limit:", 1)[0]
        self.assertIn("default: true", dry_run_section)

    def test_short_digest_skips_fetch_and_cannot_send(self):
        workflow = (ROOT / ".github/workflows/short-digest.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("name: short digest", workflow)
        self.assertIn("--skip-fetch", workflow)
        self.assertIn("--tag-limit 20", workflow)
        self.assertIn("--dry-run", workflow)
        self.assertNotIn("PODCASTINDEX_KEY", workflow)
        self.assertIn('paths:\n      - ".github/workflows/short-digest.yml"', workflow)


if __name__ == "__main__":
    unittest.main()
