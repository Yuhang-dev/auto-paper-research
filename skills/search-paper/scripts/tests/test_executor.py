"""Offline integration tests for the resumable DeepXiv executor."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
SKILL_DIR = SCRIPTS_DIR.parent
REPOSITORY_ROOT = SKILL_DIR.parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

import deepxiv_search  # noqa: E402
from new_search_run import query_record  # noqa: E402
from search_common import load_yaml, write_yaml_atomic  # noqa: E402


class FakeReader:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def search(self, **kwargs):
        return {
            "status": "success",
            "total_count": 1,
            "result": [
                {
                    "arxiv_id": "2401.00001v2",
                    "title": "Sparse Attention for Long Context",
                    "authors": ["A. Author"],
                    "date": "2024-01-01",
                    "abstract": "A metadata-only test record.",
                    "score": 0.91,
                }
            ],
        }


def executor_args(path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        run=path,
        query_id=[],
        retry_failed=False,
        dry_run=False,
        timeout=5,
        max_retries=0,
        retry_delay=0.0,
        raw_dir=None,
        fail_fast=False,
    )


def make_run(path: Path) -> None:
    run = load_yaml(SKILL_DIR / "assets" / "search-run-template.yaml")
    run["run"].update(
        {
            "id": "offline-executor-test",
            "topic_slug": "offline-executor-test",
            "question": "Does the executor preserve provenance?",
            "created_at": "2026-08-26T00:00:00Z",
            "updated_at": "2026-08-26T00:00:00Z",
            "status": "planned",
        }
    )
    run["run"]["provider"]["package_version"] = "1.0.0"
    query = query_record("Q01", "sparse attention long context", "arxiv")
    query["family"] = "direct-topic"
    query["purpose"] = "Offline executor test."
    run["queries"] = [query]
    run["scope"]["included_concepts"] = ["sparse attention"]
    run["scope"]["required_facets"] = ["provenance"]
    write_yaml_atomic(path, run)


class ExecutorIntegrationTests(unittest.TestCase):
    def test_execute_normalizes_and_never_persists_token(self) -> None:
        temporary_parent = REPOSITORY_ROOT / "tmp"
        temporary_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_parent) as directory:
            run_path = Path(directory) / "run.yaml"
            make_run(run_path)
            fake_token = "unit-test-token-not-a-real-secret"
            with mock.patch.object(deepxiv_search, "Reader", FakeReader):
                with mock.patch.dict(os.environ, {"DEEPXIV_TOKEN": fake_token}, clear=False):
                    result = deepxiv_search.execute(executor_args(run_path))

            self.assertEqual(result, 0)
            updated = load_yaml(run_path)
            self.assertEqual(updated["queries"][0]["execution"]["status"], "succeeded")
            self.assertEqual(updated["candidates"][0]["candidate_id"], "arxiv:2401.00001")
            self.assertEqual(updated["run"]["status"], "needs-review")
            raw_path = (
                REPOSITORY_ROOT
                / updated["queries"][0]["execution"]["raw_result_path"]
            )
            self.assertTrue(raw_path.is_file())
            self.assertNotIn(fake_token, run_path.read_text(encoding="utf-8"))
            self.assertNotIn(fake_token, raw_path.read_text(encoding="utf-8"))

    def test_missing_token_marks_run_without_calling_reader(self) -> None:
        temporary_parent = REPOSITORY_ROOT / "tmp"
        temporary_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_parent) as directory:
            run_path = Path(directory) / "run.yaml"
            make_run(run_path)
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(deepxiv_search, "Reader") as reader:
                    result = deepxiv_search.execute(executor_args(run_path))

            self.assertEqual(result, 3)
            reader.assert_not_called()
            updated = load_yaml(run_path)
            self.assertEqual(updated["run"]["status"], "blocked-credential")
            self.assertEqual(
                updated["queries"][0]["execution"]["status"],
                "blocked-credential",
            )
            self.assertEqual(
                updated["errors"][0]["recurrence_key"],
                "deepxiv:missing-token",
            )


if __name__ == "__main__":
    unittest.main()
