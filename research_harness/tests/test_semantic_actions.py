from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from research_harness.config import HarnessSettings, REPOSITORY_ROOT
from research_harness.evidence_verification import EvidenceVerificationResult
from research_harness.ingest_models import PaperIngestResult
from research_harness.nonconsensus_analysis import NonConsensusAnalysisResult
from research_harness.paper_sources import AcquiredPaperSource
from research_harness.research_evaluation import inspect_research
from research_harness.research_execution import DeterministicActionExecutor
from research_harness.research_models import ResearchDecision, ResearchGap


class FakeVerificationPipeline:
    requires_network = False

    def verify_next(self, *, gap, snapshot):
        del gap, snapshot
        return EvidenceVerificationResult(
            target_id="paper:fixture",
            target_kind="paper-bundle",
            status="published",
            verified_entity_ids=("paper:fixture",),
            unresolved_entity_ids=(),
            changed_paths=("papers/fixture.md",),
            diagnostic_codes=(),
            model_calls=1,
        )


class FakeAnalysisPipeline:
    requires_network = False

    def analyze(self, *, gap, snapshot):
        del gap, snapshot
        return NonConsensusAnalysisResult(
            assessment_id="assessment:fixture",
            status="published",
            result="insufficient-evidence",
            changed_paths=("assessments/fixture.md",),
            diagnostic_codes=(),
            model_calls=1,
        )


class FakeIngestPipeline:
    requires_network = False

    def __init__(self) -> None:
        self.candidate = None

    def ingest(self, candidate, *, preview=False):
        del preview
        self.candidate = candidate
        return PaperIngestResult(
            candidate_id=candidate.candidate_id,
            paper_id="paper:fixture",
            status="published",
            created_entity_ids=("paper:fixture",),
            reused_entity_ids=(),
            changed_paths=("papers/fixture.md",),
            pdf_pages=10,
            selected_pages=(1, 2),
        )


class FakeAcquirer:
    requires_network = False

    def acquire(self, candidate):
        self.candidate = candidate
        return AcquiredPaperSource(
            relative_path="sources/papers/arxiv-2601.00001.pdf",
            source_url="https://arxiv.org/pdf/2601.00001.pdf",
            sha256="a" * 64,
            size_bytes=100,
            downloaded=False,
        )


class SemanticActionExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        (REPOSITORY_ROOT / "tmp").mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT / "tmp")
        self.root = Path(self.temporary.name)
        self.settings = HarnessSettings(database_path=self.root / "actions.sqlite3")
        self.settings.validate()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_executor_routes_verify_and_analyze_claims(self) -> None:
        executor = DeterministicActionExecutor(
            self.settings,
            verification_pipeline=FakeVerificationPipeline(),
            claim_analysis_pipeline=FakeAnalysisPipeline(),
        )
        self.assertIn("verify", executor.supported_actions)
        self.assertIn("analyze_claims", executor.supported_actions)
        snapshot = inspect_research(self.settings, "long-context-sparse-models")
        for action in ("verify", "analyze_claims"):
            gap = ResearchGap(
                id=f"gap-{action}",
                key=f"test-{action}",
                type="evidence_gap" if action == "verify" else "contradiction_gap",
                question=f"Execute {action}?",
                priority=0.9,
                reasons=("test",),
                recommended_action=action,
            )
            result = executor.execute(
                decision=ResearchDecision(
                    action=action,
                    target_gap_id=gap.id,
                    reason="test routing",
                    expected_information_gain=0.8,
                ),
                gap=gap,
                snapshot=snapshot,
                action_id=f"action-{action}",
                allow_network=False,
            )
            self.assertEqual("positive", result.outcome)
            self.assertEqual(1, result.tool_calls)

    def test_ingest_acquires_selected_arxiv_source_before_pipeline(self) -> None:
        run_path = self.root / "selected-without-pdf.yaml"
        run_path.write_text(
            yaml.safe_dump(
                {
                    "run": {"updated_at": "2026-08-27T00:00:00Z"},
                    "queries": [
                        {
                            "id": "Q1",
                            "target_facets": ["technical-taxonomy"],
                            "execution": {"status": "succeeded"},
                        }
                    ],
                    "candidates": [
                        {
                            "candidate_id": "arxiv:2601.00001",
                            "status": "candidate",
                            "source": "arxiv",
                            "source_id": "2601.00001",
                            "title": "Fixture Paper",
                            "authors": ["A. Researcher"],
                            "year": 2026,
                            "review_state": "selected-for-ingest",
                            "discovered_by": [{"query_id": "Q1"}],
                        }
                    ],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        snapshot = inspect_research(self.settings, "long-context-sparse-models")
        relative = run_path.relative_to(REPOSITORY_ROOT).as_posix()
        snapshot = snapshot.model_copy(
            update={
                "corpus": snapshot.corpus.model_copy(
                    update={"search_run_paths": (relative,)}
                )
            }
        )
        gap = ResearchGap(
            id="gap-ingest-acquire",
            key="selected-papers-pending-ingest",
            type="workflow_gap",
            question="Ingest selected source?",
            priority=0.9,
            reasons=("one selected candidate",),
            recommended_action="ingest",
        )
        pipeline = FakeIngestPipeline()
        executor = DeterministicActionExecutor(
            self.settings,
            ingest_pipeline=pipeline,
            paper_source_acquirer=FakeAcquirer(),
        )
        result = executor.execute(
            decision=ResearchDecision(
                action="ingest",
                target_gap_id=gap.id,
                reason="test acquisition",
                expected_information_gain=0.9,
            ),
            gap=gap,
            snapshot=snapshot,
            action_id="action-ingest-acquire",
            allow_network=False,
        )
        self.assertEqual("positive", result.outcome)
        self.assertEqual(2, result.tool_calls)
        self.assertEqual(
            "sources/papers/arxiv-2601.00001.pdf",
            pipeline.candidate.local_pdf_path,
        )
        updated = yaml.safe_load(run_path.read_text(encoding="utf-8"))
        self.assertEqual("ingested", updated["candidates"][0]["review_state"])
        self.assertIn("source_acquisition", updated["candidates"][0])


if __name__ == "__main__":
    unittest.main()
