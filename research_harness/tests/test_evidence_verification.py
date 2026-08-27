from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

from research_harness.config import HarnessSettings, REPOSITORY_ROOT
from research_harness.evidence_verification import (
    AssessmentVerificationDraft,
    EntityVerificationDecision,
    EvidenceVerificationPipeline,
    PaperVerificationDraft,
)
from research_harness.ingest_models import IngestCandidate
from research_harness.nonconsensus_analysis import (
    ConditionAlignment,
    NonConsensusAnalysisPipeline,
    NonConsensusAssessmentDraft,
)
from research_harness.paper_ingest import PaperIngestPipeline
from research_harness.research_evaluation import inspect_research
from research_harness.research_models import ResearchGap
from research_harness.tests.test_paper_ingest import (
    LONG_LORA_PDF,
    StaticExtractor,
    paper_draft,
)
from tools.wiki.indexer import build_index
from tools.wiki.validator import validate_index
from tools.wiki.writer import WikiSourceWriter, render_wiki_page


class StaticVerifier:
    requires_network = False

    def __init__(self) -> None:
        self.paper_calls = 0
        self.assessment_calls = 0

    def verify_paper(
        self,
        *,
        skill,
        evidence_policy,
        paper_id,
        source_contract,
        entities,
        excerpt,
    ):
        del skill, evidence_policy, source_contract, excerpt
        self.paper_calls += 1
        page_by_type = {
            "paper": 1,
            "method": 5,
            "benchmark": 6,
            "model": 6,
            "claim": 6,
            "experiment": 6,
        }
        return PaperVerificationDraft(
            paper_id=paper_id,
            decisions=tuple(
                EntityVerificationDecision(
                    entity_id=str(record["id"]),
                    verdict="supported",
                    rationale="The supplied PDF page supports the structured record.",
                    pdf_pages=(page_by_type[str(record["type"])],),
                    claim_assessment=(
                        "supported" if record["type"] == "claim" else None
                    ),
                )
                for record in entities
            ),
        )

    def verify_assessment(self, *, skill, assessment, claims, experiments):
        del skill
        self.assessment_calls += 1
        return AssessmentVerificationDraft(
            assessment_id=str(assessment["id"]),
            verdict="supported",
            confirmed_result=str(assessment["metadata"]["result"]),
            rationale="The verified records address aligned conditions.",
            claim_ids=tuple(str(item["id"]) for item in claims),
            evidence_ids=tuple(str(item["id"]) for item in experiments),
        )


class SingleEvidenceAnalyzer:
    requires_network = False

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
        experiment = experiments[0]
        benchmark_id = str(experiment["metadata"]["benchmark"])
        return NonConsensusAssessmentDraft(
            question="Is the verified LongLoRA evidence broad enough to establish general quality preservation?",
            result="insufficient-evidence",
            claim_ids=(str(claims[0]["id"]),),
            evidence_ids=(str(experiment["id"]),),
            benchmark_ids=(benchmark_id,),
            rationale=(
                "One verified experiment at one context length cannot establish a "
                "general result across models, benchmarks, and context regimes."
            ),
            condition_alignment=(
                ConditionAlignment(
                    dimension="context-length",
                    status="unknown",
                    values=(str(experiment["metadata"]["context_length"]),),
                    note="Only one context length is represented.",
                ),
            ),
        )


class EvidenceVerificationPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        (REPOSITORY_ROOT / "tmp").mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT / "tmp")
        self.project_root = Path(self.temporary.name) / "project"
        self.wiki_root = self.project_root / "wiki"
        self.research_root = self.project_root / "research"
        self.source_root = self.project_root / "sources" / "papers"
        shutil.copytree(REPOSITORY_ROOT / "wiki", self.wiki_root)
        shutil.copytree(
            REPOSITORY_ROOT / "research" / "long-context-sparse-models",
            self.research_root / "long-context-sparse-models",
        )
        self.source_root.mkdir(parents=True)
        shutil.copy2(LONG_LORA_PDF, self.source_root / LONG_LORA_PDF.name)
        self.settings = HarnessSettings(
            repository_root=self.project_root,
            wiki_root=self.wiki_root,
            wiki_meta_root=self.wiki_root / "_meta",
            skills_root=REPOSITORY_ROOT / "skills",
            research_root=self.research_root,
            database_path=self.project_root / ".harness" / "verification.sqlite3",
        )
        self.settings.validate()
        self.candidate = IngestCandidate(
            candidate_id="arxiv:2309.12307",
            title="LongLoRA: Efficient Fine-tuning of Long-Context Large Language Models",
            source="arxiv",
            source_id="2309.12307",
            authors=("Yukang Chen", "Shengju Qian"),
            year=2024,
            venue="ICLR",
            paper_url="https://arxiv.org/abs/2309.12307",
            pdf_url="https://arxiv.org/pdf/2309.12307.pdf",
            local_pdf_path=f"sources/papers/{LONG_LORA_PDF.name}",
            target_facets=("technical-taxonomy", "quality-metrics"),
            search_run_path="research/long-context-sparse-models/search-runs/v0-discovery.yaml",
        )
        ingester = PaperIngestPipeline(
            self.settings,
            extractor=StaticExtractor(paper_draft()),
            now=lambda: datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc),
        )
        self.ingest_result = ingester.ingest(self.candidate)
        self._bind_search_handoff()
        self.verifier = StaticVerifier()
        self.pipeline = EvidenceVerificationPipeline(
            self.settings,
            verifier=self.verifier,
            now=lambda: datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _bind_search_handoff(self) -> None:
        run_path = (
            self.research_root
            / "long-context-sparse-models"
            / "search-runs"
            / "v0-discovery.yaml"
        )
        run = yaml.safe_load(run_path.read_text(encoding="utf-8"))
        for query in run["queries"]:
            query["execution"]["status"] = "succeeded"
            query["execution"]["executed_at"] = "2026-08-27T09:00:00Z"
        run["candidates"] = [
            {
                "candidate_id": self.candidate.candidate_id,
                "status": "candidate",
                "source": "arxiv",
                "source_id": "2309.12307",
                "title": self.candidate.title,
                "authors": list(self.candidate.authors),
                "year": 2024,
                "review_state": "ingested",
                "local_pdf_path": self.candidate.local_pdf_path,
                "discovered_by": [{"query_id": "Q01"}],
                "relevance": {"label": "core"},
                "ingest": {
                    "paper_id": self.ingest_result.paper_id,
                    "status": "published",
                    "ingested_at": "2026-08-27T09:00:00Z",
                    "wiki_paths": [
                        f"wiki/{path}" for path in self.ingest_result.changed_paths
                    ],
                    "diagnostic_codes": [],
                },
            }
        ]
        run_path.write_text(
            yaml.safe_dump(run, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    @staticmethod
    def verify_gap() -> ResearchGap:
        return ResearchGap(
            id="gap-verify-paper",
            key="minimum-verified-papers",
            type="evidence_gap",
            question="Which ingested paper requires verification?",
            priority=0.9,
            reasons=("No verified paper.",),
            recommended_action="verify",
        )

    def test_paper_bundle_promotes_only_after_source_and_schema_gates(self) -> None:
        snapshot = inspect_research(self.settings, "long-context-sparse-models")
        result = self.pipeline.verify_next(gap=self.verify_gap(), snapshot=snapshot)
        self.assertEqual("paper-bundle", result.target_kind)
        self.assertGreaterEqual(len(result.verified_entity_ids), 6)
        self.assertEqual((), result.unresolved_entity_ids)
        index = build_index(self.wiki_root, self.wiki_root / "_meta")
        for entity_id in result.verified_entity_ids:
            self.assertEqual(
                "verified", index.unique_entities()[entity_id].metadata["status"]
            )
        errors = [item for item in validate_index(index) if item.severity == "ERROR"]
        self.assertEqual([], errors)
        self.assertEqual(1, self.verifier.paper_calls)

    def test_verified_inputs_allow_independent_assessment_promotion(self) -> None:
        snapshot = inspect_research(self.settings, "long-context-sparse-models")
        paper_result = self.pipeline.verify_next(
            gap=self.verify_gap(), snapshot=snapshot
        )
        claim_id = next(
            value
            for value in paper_result.verified_entity_ids
            if value.startswith("claim:")
        )
        experiment_id = next(
            value
            for value in paper_result.verified_entity_ids
            if value.startswith("experiment:")
        )
        timestamp = "2026-08-27T10:30:00+00:00"
        assessment_id = "assessment:longlora-quality-review"
        metadata = {
            "schema_version": "0.2",
            "id": assessment_id,
            "type": "assessment",
            "title": "Does LongLoRA preserve quality at 32k?",
            "aliases": [],
            "status": "needs-review",
            "created_at": timestamp,
            "updated_at": timestamp,
            "facets": ["limitations-and-counter-evidence"],
            "question": "Does LongLoRA preserve quality at 32k?",
            "result": "supported-consensus",
            "claim_ids": [claim_id],
            "evidence_ids": [experiment_id],
            "method_family": "shifted-sparse-attention",
            "benchmark_ids": [],
            "rationale": "The available verified experiment supports the scoped claim.",
            "verified": False,
            "relations": {},
        }
        writer = WikiSourceWriter(self.wiki_root, self.wiki_root / "_meta")
        writer.publish(
            {
                "assessments/longlora-quality-review.md": render_wiki_page(
                    metadata,
                    "# Does LongLoRA preserve quality at 32k?\n\nPending verification.",
                )
            }
        )
        gap = ResearchGap(
            id="gap-assessment",
            key="nonconsensus-review",
            type="contradiction_gap",
            question="Which assessment needs verification?",
            priority=0.9,
            reasons=("One draft assessment.",),
            recommended_action="verify",
        )
        refreshed = inspect_research(self.settings, "long-context-sparse-models")
        result = self.pipeline.verify_next(gap=gap, snapshot=refreshed)
        self.assertEqual("assessment", result.target_kind)
        self.assertEqual((assessment_id,), result.verified_entity_ids)
        entity = build_index(self.wiki_root).unique_entities()[assessment_id]
        self.assertEqual("verified", entity.metadata["status"])
        self.assertTrue(entity.metadata["verified"])
        self.assertEqual(1, self.verifier.assessment_calls)

    def test_local_ingest_verify_analyze_verify_cycle_reaches_verified_assessment(
        self,
    ) -> None:
        snapshot = inspect_research(self.settings, "long-context-sparse-models")
        self.pipeline.verify_next(gap=self.verify_gap(), snapshot=snapshot)
        analysis_gap = ResearchGap(
            id="gap-cycle-analysis",
            key="nonconsensus-review",
            type="contradiction_gap",
            question="Which non-consensus question should be assessed?",
            priority=0.9,
            reasons=("No assessment covers this evidence.",),
            recommended_action="analyze_claims",
            blocking=True,
        )
        analyzer = NonConsensusAnalysisPipeline(
            self.settings,
            analyzer=SingleEvidenceAnalyzer(),
            now=lambda: datetime(2026, 8, 27, 10, 15, tzinfo=timezone.utc),
        )
        after_verify = inspect_research(self.settings, "long-context-sparse-models")
        analysis = analyzer.analyze(gap=analysis_gap, snapshot=after_verify)
        self.assertEqual("published", analysis.status)
        verify_gap = analysis_gap.model_copy(
            update={"id": "gap-cycle-assessment", "recommended_action": "verify"}
        )
        after_analysis = inspect_research(self.settings, "long-context-sparse-models")
        verification = self.pipeline.verify_next(
            gap=verify_gap,
            snapshot=after_analysis,
        )
        self.assertEqual((analysis.assessment_id,), verification.verified_entity_ids)
        final_snapshot = inspect_research(self.settings, "long-context-sparse-models")
        self.assertGreaterEqual(
            final_snapshot.evidence.verified_nonconsensus_assessments,
            1,
        )


if __name__ == "__main__":
    unittest.main()
