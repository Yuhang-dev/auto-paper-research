from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from research_harness.progress import ConsoleProgress


class _TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class ConsoleProgressTests(unittest.TestCase):
    def test_live_progress_reuses_one_line_and_persists_latest_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "progress.json"
            stream = _TTYBuffer()

            with ConsoleProgress(
                path=path,
                stream=stream,
                mode="live",
                heartbeat_seconds=60,
            ) as progress:
                progress.update(
                    stage="retrieval",
                    detail="Running provider queries",
                    completed=1,
                    total=3,
                )
                progress.update(
                    stage="skim",
                    detail="Reading source metadata",
                    completed=2,
                    total=4,
                )

            payload = json.loads(path.read_text(encoding="utf-8"))
            output = stream.getvalue()

            self.assertEqual("completed", payload["status"])
            self.assertEqual("skim", payload["stage"])
            self.assertEqual(2, payload["completed"])
            self.assertEqual(4, payload["total"])
            self.assertIn("heartbeat_at", payload)
            self.assertIn("last_progress_at", payload)
            self.assertEqual(0.0, payload["seconds_since_progress"])
            self.assertIn("\r[retrieval] 1/3", output)
            self.assertIn("done\n", output)
            self.assertIn("\r[skim] 2/4", output)
            self.assertTrue(output.endswith("\n"))

    def test_interrupt_status_is_persisted_and_propagated(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "progress.json"
            stream = _TTYBuffer()

            with self.assertRaises(KeyboardInterrupt):
                with ConsoleProgress(
                    path=path,
                    stream=stream,
                    mode="off",
                    heartbeat_seconds=60,
                ) as progress:
                    progress.update(
                        stage="deep-read",
                        detail="Extracting EvidenceCards",
                        completed=1,
                        total=10,
                    )
                    raise KeyboardInterrupt

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("interrupted", payload["status"])
            self.assertEqual("deep-read", payload["stage"])
            self.assertIn("checkpoint preserved", payload["detail"])


if __name__ == "__main__":
    unittest.main()
