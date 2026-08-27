from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from research_harness.config import HarnessSettings, REPOSITORY_ROOT
from research_harness.paper_sources import ArxivPaperSourceAcquirer, PaperSourceError
from research_harness.research_evaluation import inspect_research
from research_harness.research_models import ResearchGap
from research_harness.search_runtime import (
    CandidateScores,
    CandidateScreening,
    CandidateScreeningBatch,
    SearchPlanDraft,
    SearchQueryDraft,
    SearchRuntime,
)


class StaticSearchEngine:
    requires_network = False

    def __init__(self) -> None:
        self.plan_calls = 0
        self.screen_calls = 0

    def plan(self, *, gap, snapshot, skill, scope, prior_queries):
        del gap, snapshot, skill, scope, prior_queries
        self.plan_calls += 1
        return SearchPlanDraft(
            rationale="Target the missing hardware evidence with a distinct systems query.",
            assumptions=("Metadata screening remains provisional.",),
            queries=(
                SearchQueryDraft(
                    family="hardware-follow-up",
                    text="sparse long context attention measured GPU latency kernel",
                    purpose="Find measured systems evidence on concrete hardware.",
                    target_facets=("kernels-and-hardware", "latency-throughput"),
                ),
            ),
        )

    def screen(self, *, gap, skill, scope, candidates, existing_papers):
        del gap, skill, scope, existing_papers
        self.screen_calls += 1
        return CandidateScreeningBatch(
            screenings=tuple(
                CandidateScreening(
                    candidate_id=str(item["candidate_id"]),
                    label="core",
                    scores=CandidateScores(
                        sparsity_alignment=2,
                        long_context_alignment=2,
                        evidence_value=2,
                        engineering_value=2,
                        challenge_value=1,
                    ),
                    reason="Direct sparse long-context systems study with measured evidence.",
                    basis="title-and-abstract",
                    target_facets=("kernels-and-hardware", "latency-throughput"),
                    select_for_ingest=True,
                )
                for item in candidates
            )
        )


class SearchRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        (REPOSITORY_ROOT / "tmp").mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT / "tmp")
        self.project_root = Path(self.temporary.name) / "project"
        self.wiki_root = self.project_root / "wiki"
        self.research_root = self.project_root / "research"
        shutil.copytree(REPOSITORY_ROOT / "wiki", self.wiki_root)
        shutil.copytree(
            REPOSITORY_ROOT / "research" / "long-context-sparse-models",
            self.research_root / "long-context-sparse-models",
        )
        self.settings = HarnessSettings(
            repository_root=self.project_root,
            wiki_root=self.wiki_root,
            wiki_meta_root=self.wiki_root / "_meta",
            skills_root=REPOSITORY_ROOT / "skills",
            research_root=self.research_root,
            database_path=self.project_root / ".harness" / "runtime.sqlite3",
        )
        self.settings.validate()
        self.engine = StaticSearchEngine()
        self.runtime = SearchRuntime(self.settings, engine=self.engine)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def gap() -> ResearchGap:
        return ResearchGap(
            id="gap-search-runtime",
            key="engineering:latency",
            type="engineering_gap",
            question="What measured latency evidence is missing?",
            priority=0.9,
            reasons=("No verified latency evidence.",),
            recommended_action="search",
            search_focus=("latency", "kernels-and-hardware"),
            blocking=True,
        )

    def test_planner_creates_a_validated_follow_up_run(self) -> None:
        snapshot = inspect_research(self.settings, "long-context-sparse-models")
        result = self.runtime.plan_run(gap=self.gap(), snapshot=snapshot)
        self.assertTrue(result.run_path.is_file())
        self.assertEqual(("R02Q01",), result.query_ids)
        self.assertEqual(1, result.model_calls)
        run = yaml.safe_load(result.run_path.read_text(encoding="utf-8"))
        self.assertEqual(2, run["run"]["round"])
        self.assertEqual("planned", run["queries"][0]["execution"]["status"])
        self.assertEqual(1, run["run"]["budget"]["max_queries"])
        self.assertEqual(1, self.engine.plan_calls)

    def test_screening_triages_and_selects_core_candidate_atomically(self) -> None:
        snapshot = inspect_research(self.settings, "long-context-sparse-models")
        planned = self.runtime.plan_run(gap=self.gap(), snapshot=snapshot)
        run = yaml.safe_load(planned.run_path.read_text(encoding="utf-8"))
        query = run["queries"][0]
        query["execution"].update(
            {
                "status": "succeeded",
                "executed_at": "2026-08-27T10:00:00Z",
                "provider_total_count": 1,
                "retrieved_count": 1,
                "retained_count": 1,
            }
        )
        run["candidates"] = [
            {
                "candidate_id": "arxiv:2601.00001",
                "status": "candidate",
                "source": "arxiv",
                "source_id": "2601.00001",
                "title": "Measured Sparse Attention Kernels at Long Context",
                "authors": ["A. Researcher"],
                "year": 2026,
                "abstract": "We measure sparse attention kernels from 32k to 128k.",
                "discovered_by": [
                    {
                        "query_id": "R02Q01",
                        "provider_rank": 1,
                        "provider_score": 0.9,
                        "returned_source_id": "2601.00001",
                        "retrieved_at": "2026-08-27T10:00:00Z",
                    }
                ],
                "relevance": {
                    "label": None,
                    "scores": {
                        "sparsity_alignment": None,
                        "long_context_alignment": None,
                        "evidence_value": None,
                        "engineering_value": None,
                        "challenge_value": None,
                    },
                    "reason": None,
                    "basis": None,
                },
                "review_state": "metadata-only",
            }
        ]
        planned.run_path.write_text(
            yaml.safe_dump(run, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        result = self.runtime.screen_run(run_path=planned.run_path, gap=self.gap())
        self.assertTrue(result.changed)
        self.assertEqual(1, result.triaged_candidates)
        self.assertEqual(1, result.selected_candidates)
        screened = yaml.safe_load(planned.run_path.read_text(encoding="utf-8"))
        candidate = screened["candidates"][0]
        self.assertEqual("core", candidate["relevance"]["label"])
        self.assertEqual("selected-for-ingest", candidate["review_state"])
        facets = {item["name"]: item for item in screened["coverage"]["facets"]}
        self.assertEqual("partial", facets["kernels-and-hardware"]["status"])


class PaperSourceAcquirerTests(unittest.TestCase):
    def setUp(self) -> None:
        (REPOSITORY_ROOT / "tmp").mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT / "tmp")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_selected_arxiv_pdf_is_bounded_and_stored_under_repository(self) -> None:
        class Response:
            headers = {"Content-Length": "18"}

            def __init__(self):
                self.parts = [b"%PDF-1.7\nfixture\n", b""]

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def geturl(self):
                return "https://arxiv.org/pdf/2601.00001.pdf"

            def read(self, size):
                del size
                return self.parts.pop(0)

        acquirer = ArxivPaperSourceAcquirer(self.root)
        candidate = {
            "candidate_id": "arxiv:2601.00001",
            "source": "arxiv",
            "source_id": "2601.00001",
            "review_state": "selected-for-ingest",
        }
        with mock.patch(
            "research_harness.paper_sources.urllib.request.urlopen",
            return_value=Response(),
        ):
            result = acquirer.acquire(candidate)
        self.assertTrue(result.downloaded)
        self.assertTrue((self.root / result.relative_path).is_file())
        self.assertTrue(result.relative_path.startswith("sources/papers/"))

    def test_unselected_candidate_is_rejected_without_network(self) -> None:
        acquirer = ArxivPaperSourceAcquirer(self.root)
        with self.assertRaisesRegex(PaperSourceError, "selected-for-ingest"):
            acquirer.acquire(
                {
                    "source": "arxiv",
                    "source_id": "2601.00001",
                    "review_state": "metadata-only",
                }
            )


if __name__ == "__main__":
    unittest.main()
