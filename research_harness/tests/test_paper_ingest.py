from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import ValidationError

from research_harness.config import HarnessSettings, REPOSITORY_ROOT
from research_harness.ingest_models import (
    IngestCandidate,
    PaperIngestDraft,
    PaperIngestResult,
)
from research_harness.paper_ingest import (
    PaperIngestError,
    PaperIngestPipeline,
    extract_pdf_document,
    select_paper_excerpt,
)
from research_harness.research_control import AutonomousResearchController
from research_harness.research_evaluation import inspect_research
from research_harness.research_execution import DeterministicActionExecutor
from research_harness.research_models import ResearchDecision, ResearchGap
from tools.wiki.indexer import build_index
from tools.wiki.validator import validate_index
from tools.wiki.writer import WikiSourceWriter, WikiWriteError


LONG_LORA_PDF = REPOSITORY_ROOT / "sources" / "papers" / "longlora-iclr-2024.pdf"


def paper_draft(candidate_id: str = "arxiv:2309.12307") -> PaperIngestDraft:
    return PaperIngestDraft.model_validate(
        {
            "candidate_id": candidate_id,
            "paper": {
                "title": "LongLoRA: Efficient Fine-tuning of Long-Context Large Language Models",
                "authors": ["Yukang Chen", "Shengju Qian"],
                "year": 2024,
                "venue": "ICLR",
                "identifiers": {"arxiv": "2309.12307"},
                "urls": {"paper": "https://arxiv.org/abs/2309.12307"},
                "status": "draft",
                "facets": ["technical-taxonomy", "quality-metrics"],
                "problem": "Extend pretrained language models to long contexts at tractable fine-tuning cost.",
                "motivation": "Dense long-sequence attention makes context extension expensive.",
                "assumptions_and_scope": "Reported Llama 2 configurations and training-time sparse attention.",
                "method_overview": "Use shifted sparse attention during fine-tuning and dense attention at inference.",
                "reported_limitations": [
                    {
                        "statement": "Short-context perplexity can degrade.",
                        "evidence": {
                            "pdf_page": 7,
                            "section": "4.2",
                            "description": "The authors discuss short-context degradation.",
                        },
                    }
                ],
                "open_questions": ["How well does the method transfer beyond Llama 2?"],
            },
            "methods": [
                {
                    "key": "s2",
                    "proposed_slug": "shifted-sparse-attention",
                    "title": "Shifted Sparse Attention",
                    "aliases": ["S2-Attn"],
                    "facets": ["technical-taxonomy", "static-vs-dynamic"],
                    "evidence": {
                        "pdf_page": 5,
                        "section": "3.2",
                        "element": "Algorithm 1",
                        "description": "Shifted grouping is defined here.",
                    },
                    "definition": "Split attention into local groups and shift half of the heads by half a group.",
                    "sparsity": {"target": "attention", "pattern": "structured"},
                    "implementations": ["https://github.com/dvlab-research/LongLoRA"],
                }
            ],
            "benchmarks": [
                {
                    "key": "pg19",
                    "proposed_slug": "pg19",
                    "title": "PG-19",
                    "aliases": ["PG19"],
                    "facets": ["quality-metrics"],
                    "evidence": {
                        "pdf_page": 6,
                        "element": "Table 2",
                        "description": "PG19 validation perplexity is reported.",
                    },
                    "task": "Long-form language-model perplexity evaluation.",
                    "metrics": ["perplexity"],
                    "source_url": "https://arxiv.org/abs/1911.05507",
                }
            ],
            "models": [
                {
                    "key": "llama2_7b",
                    "proposed_slug": "llama2-7b",
                    "title": "Llama 2 7B",
                    "aliases": ["LLaMA2-7B"],
                    "facets": ["quality-metrics"],
                    "evidence": {
                        "pdf_page": 6,
                        "element": "Table 2",
                        "description": "The evaluated model is named in the table.",
                    },
                    "family": "Llama 2",
                    "parameters": 7000000000,
                    "source_url": "https://arxiv.org/abs/2307.09288",
                }
            ],
            "claims": [
                {
                    "key": "c1",
                    "statement": "Expanded LoRA reaches PG19 perplexity close to full fine-tuning at 32k context.",
                    "attribution": "author",
                    "evidence_type": "experiment-supported",
                    "evidence_status": "located",
                    "evidence": {
                        "pdf_page": 6,
                        "element": "Table 2",
                        "description": "The compared perplexity values are in the table.",
                    },
                    "scope": {
                        "model": "Llama 2 7B",
                        "benchmark": "PG19",
                        "context_length": 32768,
                    },
                    "facets": ["quality-metrics"],
                }
            ],
            "experiments": [
                {
                    "key": "e1",
                    "method_keys": ["s2"],
                    "model_keys": ["llama2_7b"],
                    "benchmark_key": "pg19",
                    "context_length": 32768,
                    "sparsity": {"target": "attention", "pattern": "shifted-groups"},
                    "metric": {
                        "name": "perplexity",
                        "direction": "lower-is-better",
                    },
                    "result": {
                        "value": 8.12,
                        "baseline": "Full fine-tuning: 8.08",
                        "comparison": "Standard rank-8 LoRA: 11.44",
                    },
                    "evidence": {
                        "pdf_page": 6,
                        "element": "Table 2",
                        "description": "The result and baselines share one table row group.",
                    },
                    "supports_claim_keys": ["c1"],
                    "facets": ["quality-metrics"],
                }
            ],
        }
    )


class StaticExtractor:
    requires_network = False

    def __init__(self, draft: PaperIngestDraft):
        self.draft = draft
        self.calls = []

    def extract(self, **kwargs):
        self.calls.append(kwargs)
        return self.draft


class PaperIngestPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        (REPOSITORY_ROOT / "tmp").mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT / "tmp")
        self.root = Path(self.temporary.name)
        self.wiki_root = self.root / "wiki"
        shutil.copytree(REPOSITORY_ROOT / "wiki", self.wiki_root)
        self.settings = HarnessSettings(
            repository_root=REPOSITORY_ROOT,
            wiki_root=self.wiki_root,
            wiki_meta_root=self.wiki_root / "_meta",
            skills_root=REPOSITORY_ROOT / "skills",
            research_root=REPOSITORY_ROOT / "research",
            database_path=self.root / "harness.sqlite3",
        )
        self.settings.validate()
        self.candidate = IngestCandidate(
            candidate_id="arxiv:2309.12307",
            title="LongLoRA",
            source="arxiv",
            source_id="2309.12307",
            authors=("Yukang Chen",),
            year=2024,
            paper_url="https://arxiv.org/abs/2309.12307",
            pdf_url="https://arxiv.org/pdf/2309.12307",
            local_pdf_path="sources/papers/longlora-iclr-2024.pdf",
            target_facets=("technical-taxonomy",),
            search_run_path="research/test/search-run.yaml",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def pipeline(self, extractor: StaticExtractor) -> PaperIngestPipeline:
        return PaperIngestPipeline(
            self.settings,
            extractor=extractor,
            now=lambda: datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc),
        )

    def test_real_pdf_extraction_preserves_page_markers(self) -> None:
        document = extract_pdf_document(LONG_LORA_PDF, REPOSITORY_ROOT)
        excerpt = select_paper_excerpt(document, max_pages=12, max_chars=50_000)
        self.assertGreaterEqual(len(document.pages), 15)
        self.assertEqual(1, document.pages[0].pdf_page)
        self.assertIn("--- PDF p. 1 ---", excerpt.text)
        self.assertIn(1, excerpt.selected_pages)
        self.assertTrue(document.sha256)

    def test_draft_rejects_unknown_cross_references(self) -> None:
        payload = paper_draft().model_dump(mode="json")
        payload["experiments"][0]["method_keys"] = ["missing"]
        with self.assertRaisesRegex(ValidationError, "unknown method"):
            PaperIngestDraft.model_validate(payload)

    def test_pipeline_publishes_valid_v02_entities_and_is_idempotent(self) -> None:
        extractor = StaticExtractor(paper_draft())
        pipeline = self.pipeline(extractor)
        first = pipeline.ingest(self.candidate)
        self.assertEqual("published", first.status)
        self.assertEqual("paper:longlora", first.paper_id)
        self.assertEqual(5, len(first.changed_paths))
        self.assertIn("paper:longlora", first.reused_entity_ids)

        index = build_index(self.wiki_root, self.wiki_root / "_meta")
        errors = [item for item in validate_index(index) if item.severity == "ERROR"]
        self.assertEqual([], errors)
        generated = [
            entity
            for entity in index.unique_entities().values()
            if entity.entity_id in first.created_entity_ids
        ]
        self.assertEqual(5, len(generated))
        self.assertEqual({"draft"}, {entity.metadata["status"] for entity in generated})
        experiment = next(
            item for item in generated if item.entity_type == "experiment"
        )
        self.assertEqual(6, experiment.metadata["evidence"]["pdf_page"])
        self.assertIn("Table 2", experiment.metadata["evidence"]["locator"])

        second = pipeline.ingest(self.candidate)
        self.assertEqual("no-change", second.status)
        self.assertEqual((), second.changed_paths)
        self.assertEqual(
            len(index.unique_entities()),
            len(
                build_index(self.wiki_root, self.wiki_root / "_meta").unique_entities()
            ),
        )

    def test_new_paper_can_publish_as_needs_review(self) -> None:
        draft = paper_draft("arxiv:9999.00001")
        draft = draft.model_copy(
            update={
                "paper": draft.paper.model_copy(
                    update={
                        "title": "A Synthetic Sparse Context Paper",
                        "identifiers": draft.paper.identifiers.model_copy(
                            update={"arxiv": "9999.00001"}
                        ),
                        "status": "needs-review",
                    }
                ),
                "methods": (
                    draft.methods[0].model_copy(
                        update={
                            "title": "Synthetic Shifted Attention",
                            "proposed_slug": "synthetic-shifted-attention",
                        }
                    ),
                ),
            }
        )
        candidate = self.candidate.model_copy(
            update={
                "candidate_id": "arxiv:9999.00001",
                "source_id": "9999.00001",
                "title": "A Synthetic Sparse Context Paper",
            }
        )
        result = self.pipeline(StaticExtractor(draft)).ingest(candidate)
        self.assertEqual("published", result.status)
        self.assertEqual(6, len(result.changed_paths))
        paper = build_index(
            self.wiki_root, self.wiki_root / "_meta"
        ).resolver.exact_entity(result.paper_id)
        self.assertIsNotNone(paper)
        assert paper is not None
        self.assertEqual("needs-review", paper.metadata["status"])
        self.assertEqual("0.2", paper.metadata["schema_version"])
        self.assertEqual("ICLR", paper.metadata["venue"])

    def test_candidate_mismatch_does_not_write(self) -> None:
        before = build_index(self.wiki_root, self.wiki_root / "_meta").source_hash
        extractor = StaticExtractor(paper_draft("arxiv:wrong"))
        with self.assertRaisesRegex(PaperIngestError, "does not match"):
            self.pipeline(extractor).ingest(self.candidate)
        after = build_index(self.wiki_root, self.wiki_root / "_meta").source_hash
        self.assertEqual(before, after)

    def test_writer_rejects_invalid_shadow_page_without_mutation(self) -> None:
        writer = WikiSourceWriter(self.wiki_root, self.wiki_root / "_meta")
        target = self.wiki_root / "papers" / "invalid-ingest.md"
        with self.assertRaisesRegex(WikiWriteError, "validation failed"):
            writer.publish({"papers/invalid-ingest.md": "# Missing frontmatter\n"})
        self.assertFalse(target.exists())


class IngestExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        (REPOSITORY_ROOT / "tmp").mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT / "tmp")
        self.root = Path(self.temporary.name)
        self.settings = HarnessSettings(database_path=self.root / "harness.sqlite3")
        self.settings.validate()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_executor_routes_selected_local_candidate_to_ingest_pipeline(self) -> None:
        run_path = self.root / "selected.yaml"
        run_path.write_text(
            yaml.safe_dump(
                {
                    "queries": [
                        {
                            "id": "Q1",
                            "target_facets": ["technical-taxonomy"],
                            "execution": {"status": "succeeded"},
                        }
                    ],
                    "candidates": [
                        {
                            "candidate_id": "arxiv:2309.12307",
                            "status": "candidate",
                            "source": "arxiv",
                            "source_id": "2309.12307",
                            "title": "LongLoRA",
                            "authors": ["Yukang Chen"],
                            "year": 2024,
                            "review_state": "selected-for-ingest",
                            "local_pdf_path": "sources/papers/longlora-iclr-2024.pdf",
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
            id="gap-selected-ingest",
            key="selected-papers-pending-ingest",
            type="workflow_gap",
            question="Which selected paper should be ingested?",
            priority=0.97,
            reasons=("one selected candidate",),
            recommended_action="ingest",
            search_focus=("technical-taxonomy",),
        )
        decision = ResearchDecision(
            action="ingest",
            target_gap_id=gap.id,
            reason="ingest selected paper",
            expected_information_gain=0.9,
        )

        class FakePipeline:
            requires_network = False

            def __init__(self):
                self.candidate = None

            def ingest(self, candidate, *, preview=False):
                self.candidate = candidate
                return PaperIngestResult(
                    candidate_id=candidate.candidate_id,
                    paper_id="paper:longlora",
                    status="published",
                    created_entity_ids=("experiment:longlora-e1",),
                    reused_entity_ids=("paper:longlora",),
                    changed_paths=("experiments/longlora-e1.md",),
                    pdf_pages=19,
                    selected_pages=(1, 2, 6),
                )

        pipeline = FakePipeline()
        executor = DeterministicActionExecutor(
            self.settings,
            ingest_pipeline=pipeline,
        )
        result = executor.execute(
            decision=decision,
            gap=gap,
            snapshot=snapshot,
            action_id="action-ingest-1",
            allow_network=False,
        )
        self.assertIn("ingest", executor.supported_actions)
        self.assertEqual("positive", result.outcome)
        self.assertEqual(
            ("wiki/experiments/longlora-e1.md", relative),
            result.changed_sources,
        )
        self.assertIsNotNone(pipeline.candidate)
        self.assertEqual("arxiv:2309.12307", pipeline.candidate.candidate_id)
        updated_run = yaml.safe_load(run_path.read_text(encoding="utf-8"))
        self.assertEqual("ingested", updated_run["candidates"][0]["review_state"])
        self.assertEqual(
            "paper:longlora",
            updated_run["candidates"][0]["ingest"]["paper_id"],
        )

    def test_outer_loop_ingests_reobserves_and_closes_candidate_handoff(self) -> None:
        project_root = self.root / "project"
        wiki_root = project_root / "wiki"
        research_root = project_root / "research"
        source_root = project_root / "sources" / "papers"
        shutil.copytree(REPOSITORY_ROOT / "wiki", wiki_root)
        shutil.copytree(
            REPOSITORY_ROOT / "research" / "long-context-sparse-models",
            research_root / "long-context-sparse-models",
        )
        source_root.mkdir(parents=True)
        shutil.copy2(LONG_LORA_PDF, source_root / LONG_LORA_PDF.name)
        settings = HarnessSettings(
            repository_root=project_root,
            wiki_root=wiki_root,
            wiki_meta_root=wiki_root / "_meta",
            skills_root=REPOSITORY_ROOT / "skills",
            research_root=research_root,
            database_path=project_root / ".harness" / "outer.sqlite3",
        )
        settings.validate()
        run_path = (
            research_root
            / "long-context-sparse-models"
            / "search-runs"
            / "v0-discovery.yaml"
        )
        run = yaml.safe_load(run_path.read_text(encoding="utf-8"))
        for query in run["queries"]:
            query["execution"]["status"] = "succeeded"
        candidate_id = "arxiv:2309.12307"
        run["candidates"] = [
            {
                "candidate_id": candidate_id,
                "status": "candidate",
                "source": "arxiv",
                "source_id": "2309.12307",
                "title": "LongLoRA",
                "authors": ["Yukang Chen"],
                "year": 2024,
                "review_state": "selected-for-ingest",
                "local_pdf_path": "sources/papers/longlora-iclr-2024.pdf",
                "discovered_by": [{"query_id": "Q01"}],
                "relevance": {"label": "core"},
            }
        ]
        for facet in run["coverage"]["facets"]:
            if facet["name"] in {"technical-taxonomy", "quality-metrics"}:
                facet["status"] = "partial"
                facet["candidate_ids"] = [candidate_id]
        run_path.write_text(
            yaml.safe_dump(run, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        pipeline = PaperIngestPipeline(
            settings,
            extractor=StaticExtractor(paper_draft()),
            now=lambda: datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc),
        )
        executor = DeterministicActionExecutor(
            settings,
            ingest_pipeline=pipeline,
        )
        with AutonomousResearchController(
            settings,
            research_id="long-context-sparse-models",
            action_executor=executor,
        ) as controller:
            state = controller.invoke(
                thread_id="outer-ingest-integration",
                allow_network=False,
            )

        self.assertEqual("ingest", state["action_history"][0]["result"]["action"])
        self.assertEqual("positive", state["action_history"][0]["result"]["outcome"])
        self.assertEqual(0, state["snapshot"]["corpus"]["selected_for_ingest"])
        self.assertGreaterEqual(state["snapshot"]["evidence"]["experiments_total"], 1)
        closed = yaml.safe_load(run_path.read_text(encoding="utf-8"))["candidates"][0]
        self.assertEqual("ingested", closed["review_state"])
        self.assertEqual("paper:longlora", closed["ingest"]["paper_id"])


if __name__ == "__main__":
    unittest.main()
