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

        def tag_fake(target, dry_run=False):
            events.append("tag")
            return self.tag_result()

        def curate_fake(target, run_id, previous_cutoff=None):
            events.append("curate")
            return SimpleNamespace(counts_by_topic={"technology-ai": 2, "travel": 0})

        def send_fake(target, run_id, dry_run=False):
            events.append("send")
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
                metrics = pipeline.execute(conn)
            row = conn.execute("SELECT * FROM run WHERE id=?", (metrics.run_id,)).fetchone()

        self.assertEqual(events, ["fetch", "tag", "curate", "send"])
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


if __name__ == "__main__":
    unittest.main()
