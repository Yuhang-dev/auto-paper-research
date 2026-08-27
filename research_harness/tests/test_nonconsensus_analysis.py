from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from research_harness.config import HarnessSettings, REPOSITORY_ROOT
from research_harness.nonconsensus_analysis import (
    ConditionAlignment,
    NonConsensusAnalysisError,
    NonConsensusAnalysisPipeline,
    NonConsensusAssessmentDraft,
)
from research_harness.research_evaluation import inspect_research
from research_harness.research_models import ResearchGap
from tools.wiki.indexer import build_index
from tools.wiki.validator import validate_index


class StaticAnalyzer:
    requires_network = False

    def __init__(self) -> None:
        self.calls = 0

    def analyze(
        self,
        *,
        skill,
        gap,
        claims,
        experiments,
        existing_assessments,
    ):
        del skill, gap, existing_assessments
        self.calls += 1
        return NonConsensusAssessmentDraft(
            question="Is one 32k RULER experiment enough to establish robust quality preservation?",
            result="insufficient-evidence",
            claim_ids=(str(claims[0]["id"]),),
            evidence_ids=(str(experiments[0]["id"]),),
            method_family="structured-sparse-attention",
            benchmark_ids=(str(experiments[0]["metadata"]["benchmark"]),),
            rationale=(
                "The verified evidence covers one model, benchmark, and context length, "
                "so it cannot establish robustness across conditions."
            ),
            condition_alignment=(
                ConditionAlignment(
                    dimension="context-length",
                    status="unknown",
                    values=("32768",),
                    note="Only one context bucket is represented.",
                ),
                ConditionAlignment(
                    dimension="benchmark",
                    status="aligned",
                    values=("benchmark:ruler",),
                    note="The claim and experiment use the same benchmark.",
                ),
            ),
        )


class NonConsensusAnalysisPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        (REPOSITORY_ROOT / "tmp").mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT / "tmp")
        self.project_root = Path(self.temporary.name) / "project"
        self.wiki_root = self.project_root / "wiki"
        self.research_root = self.project_root / "research"
        shutil.copytree(
            REPOSITORY_ROOT / "tools" / "wiki" / "tests" / "fixtures" / "wiki",
            self.wiki_root,
        )
        shutil.copytree(
            REPOSITORY_ROOT / "research" / "long-context-sparse-models",
            self.research_root / "long-context-sparse-models",
        )
        self.settings = HarnessSettings(
            repository_root=self.project_root,
            wiki_root=self.wiki_root,
            wiki_meta_root=REPOSITORY_ROOT / "wiki" / "_meta",
            skills_root=REPOSITORY_ROOT / "skills",
            research_root=self.research_root,
            database_path=self.project_root / ".harness" / "analysis.sqlite3",
        )
        self.settings.validate()
        self.analyzer = StaticAnalyzer()
        self.pipeline = NonConsensusAnalysisPipeline(
            self.settings,
            analyzer=self.analyzer,
            now=lambda: datetime(2026, 8, 27, 11, 0, tzinfo=timezone.utc),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def gap() -> ResearchGap:
        return ResearchGap(
            id="gap-nonconsensus",
            key="nonconsensus-review",
            type="contradiction_gap",
            question="Which non-consensus question needs assessment?",
            priority=0.9,
            reasons=("Assessment coverage is incomplete.",),
            recommended_action="analyze_claims",
            blocking=True,
        )

    def test_analysis_creates_needs_review_assessment_from_verified_inputs(
        self,
    ) -> None:
        snapshot = inspect_research(self.settings, "long-context-sparse-models")
        result = self.pipeline.analyze(gap=self.gap(), snapshot=snapshot)
        self.assertEqual("insufficient-evidence", result.result)
        index = build_index(self.wiki_root, self.settings.wiki_meta_root)
        entity = index.unique_entities()[result.assessment_id]
        self.assertEqual("needs-review", entity.metadata["status"])
        self.assertFalse(entity.metadata["verified"])
        self.assertEqual("analyze-claims", entity.metadata["analysis"]["skill"])
        errors = [item for item in validate_index(index) if item.severity == "ERROR"]
        self.assertEqual([], errors)
        self.assertEqual(1, self.analyzer.calls)

    def test_duplicate_assessment_fingerprint_is_rejected(self) -> None:
        snapshot = inspect_research(self.settings, "long-context-sparse-models")
        self.pipeline.analyze(gap=self.gap(), snapshot=snapshot)
        with self.assertRaisesRegex(
            NonConsensusAnalysisError, "existing assessment fingerprint"
        ):
            self.pipeline.analyze(gap=self.gap(), snapshot=snapshot)

    def test_contested_cannot_hide_mismatched_core_conditions(self) -> None:
        draft = NonConsensusAssessmentDraft(
            question="Do aligned sparse methods preserve quality?",
            result="contested",
            claim_ids=("claim:a", "claim:b"),
            evidence_ids=("experiment:a", "experiment:b"),
            rationale="Two records appear incompatible but use different benchmarks.",
            condition_alignment=(
                ConditionAlignment(
                    dimension="benchmark",
                    status="mismatched",
                    values=("benchmark:a", "benchmark:b"),
                    note="The task definitions differ materially.",
                ),
            ),
        )
        with self.assertRaisesRegex(NonConsensusAnalysisError, "mismatched core"):
            self.pipeline._validate_contested_alignment(draft)


if __name__ == "__main__":
    unittest.main()
