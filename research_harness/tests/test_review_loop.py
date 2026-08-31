from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
import urllib.parse
from dataclasses import replace
from pathlib import Path
from unittest import mock

from research_harness.config import HarnessSettings, REPOSITORY_ROOT
from research_harness.evidence_verification import (
    EntityVerificationDecision,
    PaperVerificationDraft,
)
from research_harness.model_client import ReviewModelBundle
from research_harness.review_control import ReviewController
from research_harness.review_errorbook import aggregate_review_error_book
from research_harness.review_logic import (
    merge_sources,
    validate_nonconsensus_assessment,
)
from research_harness.review_models import (
    DiscoveryRecord,
    EvidenceCard,
    EvidenceExtraction,
    EvidenceLocator,
    NonConsensusAssessment,
    PromotionItem,
    PromotionManifest,
    QueryPlan,
    ReasoningUpdate,
    RetrievalQuery,
    ReviewErrorEvent,
    ReviewRunConfig,
    ReviewScope,
    ReviewSynthesisDraft,
    SourceRecord,
    SourceMaterial,
    SourceScreening,
    SourceScreeningBatch,
    SourceSkim,
    SynthesisStatement,
    UnderstandingClaim,
    validate_synthesis_references,
)
from research_harness.review_promotion import ReviewPromoter
from research_harness.review_providers import (
    ReviewProviderRegistry,
    SemanticScholarProvider,
    _semantic_scholar_paper_details,
)
from research_harness.review_storage import ReviewArtifactStore


NOW = "2026-08-31T00:00:00Z"
FACETS = ("technical-taxonomy", "latency-throughput")


def _discovery(query_id: str, rank: int = 1) -> DiscoveryRecord:
    return DiscoveryRecord(
        query_id=query_id,
        provider="tavily",
        rank=rank,
        retrieved_at=NOW,
    )


def _web_source(index: int, query_id: str = "R01Q01") -> SourceRecord:
    return SourceRecord(
        source_id=f"web:source-{index}",
        source_type="web",
        provider="tavily",
        title=f"Official sparse context source {index}",
        canonical_url=f"https://example.org/source-{index}",
        content_preview=f"Located technical material for sparse context source {index}.",
        target_facets=FACETS,
        discoveries=(_discovery(query_id, index),),
    )


class FakeTavilyProvider:
    name = "tavily"

    def __init__(self):
        self.calls = 0

    async def search(self, query: RetrievalQuery, *, limit: int):
        self.calls += 1
        return tuple(_web_source(index, query.id) for index in range(1, min(limit, 2) + 1))


class FakeFailingGitHubProvider:
    name = "github"

    async def search(self, query: RetrievalQuery, *, limit: int):
        del query, limit
        raise RuntimeError("sanitized provider fixture failure")


class FakeReviewEngine:
    requires_network = False
    model_fingerprint = "fake-review-engine-v1"

    def __init__(self, *, interrupt_screen_once: bool = False):
        self.interrupt_screen_once = interrupt_screen_once
        self.screen_calls = 0
        self.evidence_calls = 0

    def plan_queries(self, *, scope, config, round_number, **_kwargs):
        return QueryPlan(
            rationale="Target the highest-priority uncertainty.",
            queries=(
                RetrievalQuery(
                    id=f"R{round_number:02d}Q01",
                    round=round_number,
                    provider="tavily",
                    text="sparse long context engineering evidence",
                    purpose="Find independent located engineering evidence.",
                    target_facets=FACETS,
                ),
            ),
        )
    def screen_batch(self, *, sources, **_kwargs):
        self.screen_calls += 1
        if self.interrupt_screen_once:
            self.interrupt_screen_once = False
            raise KeyboardInterrupt()
        return SourceScreeningBatch(
            screenings=tuple(
                SourceScreening(
                    source_id=item.source_id,
                    label="core",
                    relevance_score=0.9,
                    evidence_potential=0.9,
                    engineering_value=0.8,
                    counterevidence_value=0.5,
                    reason="Directly addresses the framed sparse-context question.",
                    target_facets=FACETS,
                )
                for item in sources
            )
        )

    def skim_source(self, *, source, **_kwargs):
        return SourceSkim(
            source_id=source.source_id,
            source_type=source.source_type,
            label="core",
            relevance_score=0.9,
            why_relevant="Likely contains directly relevant engineering evidence.",
            method_families=("sparse-attention",),
            key_findings=("Requires full-text confirmation.",),
            questions_raised=("Are conditions comparable across sources?",),
            target_facets=FACETS,
            select_for_deep_read=True,
            basis="source-excerpt",
        )

    def reason(self, *, cards, uncertainties, **_kwargs):
        if not cards:
            return ReasoningUpdate(summary="Skims identify a route but are not evidence.")
        card_ids = tuple(item.card_id for item in cards)
        source_ids = tuple(sorted({item.source_id for item in cards}))
        claim = UnderstandingClaim(
            claim_id="claim:cross-source-observation",
            statement="Two official sources report sparse-context engineering trade-offs.",
            scope=("long-context", "engineering"),
            confidence=0.7,
            supporting_card_ids=card_ids,
            status="supported",
        )
        resolved = tuple(
            item.model_copy(
                update={
                    "status": "resolved",
                    "resolution": "Bounded evidence was located for the smoke run.",
                    "supporting_card_ids": card_ids,
                }
            )
            for item in uncertainties
        )
        assessment = NonConsensusAssessment(
            assessment_id="assessment:smoke",
            question="Do independent sources establish a stable cross-setting result?",
            result="insufficient-evidence",
            comparable=False,
            independent_source_ids=source_ids,
            supporting_card_ids=card_ids,
            rationale="The sources are independent, but the smoke fixtures are not comparable.",
        )
        return ReasoningUpdate(
            summary="Evidence replaced provisional skim-only understanding.",
            claims=(claim,),
            uncertainties=resolved,
            assessments=(assessment,),
            new_method_families=("sparse-attention",),
            resolved_uncertainty_ids=tuple(item.uncertainty_id for item in uncertainties),
            found_independent_counterevidence=True,
        )

    def extract_evidence(self, *, source, material, **_kwargs):
        self.evidence_calls += 1
        card = EvidenceCard(
            card_id=f"card:{source.source_id}",
            source_id=source.source_id,
            source_url=source.canonical_url,
            source_version=source.version or "fixture-v1",
            source_sha256=material.sha256,
            statement=f"{source.title} documents a sparse-context engineering trade-off.",
            attribution="author",
            evidence_type="documentation",
            status="located",
            locator=EvidenceLocator(kind="url", value=source.canonical_url),
            target_facets=FACETS,
        )
        return EvidenceExtraction(source_id=source.source_id, cards=(card,))

    def synthesize(self, *, scope, cards, **_kwargs):
        return ReviewSynthesisDraft(
            title=scope.title,
            scope_summary=scope.question,
            core_findings=(
                SynthesisStatement(
                    statement_id="finding:smoke",
                    statement="Independent sources document engineering trade-offs, but comparability remains bounded.",
                    evidence_card_ids=tuple(item.card_id for item in cards),
                    claim_kind="comparison",
                    scope=("long-context", "engineering"),
                    confidence="medium",
                ),
            ),
            open_questions=("How do controlled hardware and context settings change the result?",),
            limitations=("This is a bounded offline smoke fixture, not a domain conclusion.",),
        )


class FakePromotionVerifier:
    def verify_paper(self, *, paper_id, entities, **_kwargs):
        return PaperVerificationDraft(
            paper_id=paper_id,
            decisions=tuple(
                EntityVerificationDecision(
                    entity_id=item["entity_id"],
                    verdict="supported",
                    rationale="The located statement is visible on the retained page.",
                    pdf_pages=(1,),
                )
                for item in entities
            ),
        )


class ReviewLoopTests(unittest.TestCase):
    def setUp(self):
        (REPOSITORY_ROOT / "tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT / "tmp")
        self.root = Path(self.temporary.name)
        for path in (
            self.root / "wiki" / "_meta",
            self.root / "research",
            self.root / "error_book" / "_generated",
        ):
            path.mkdir(parents=True, exist_ok=True)
        (self.root / "wiki" / "sentinel.md").write_text(
            "wiki source of truth\n", encoding="utf-8"
        )
        self.settings = HarnessSettings(
            repository_root=self.root,
            wiki_root=self.root / "wiki",
            wiki_meta_root=self.root / "wiki" / "_meta",
            skills_root=REPOSITORY_ROOT / "skills",
            research_root=self.root / "research",
            database_path=self.root / ".harness" / "review.sqlite3",
        )
        self.settings.validate()
        self.scope = ReviewScope(
            research_id="sparse-review",
            title="Sparse long-context review",
            question="What are the performance and engineering limits of sparse long-context models?",
            required_facets=FACETS,
            candidate_hypotheses=("Sparse speedups may depend on kernels.",),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _config(self, run_id="smoke-run", thread_id="smoke-thread"):
        return ReviewRunConfig.for_profile(
            research_id=self.scope.research_id,
            run_id=run_id,
            thread_id=thread_id,
            profile="smoke",
            question=self.scope.question,
            title=self.scope.title,
            required_facets=self.scope.required_facets,
            candidate_hypotheses=self.scope.candidate_hypotheses,
            allow_network=False,
            allow_single_model_fallback=False,
            canary=True,
            stop_after="synthesis",
            created_at=NOW,
        )

    def _providers(self, config):
        store = ReviewArtifactStore(self.settings, config)
        return ReviewProviderRegistry(
            self.settings.repository_root,
            store.working_root,
            providers={"tavily": FakeTavilyProvider()},
            network_concurrency=2,
        )

    def test_source_skim_is_never_citation_eligible(self):
        skim = FakeReviewEngine().skim_source(source=_web_source(1), scope=self.scope)
        self.assertTrue(skim.provisional)
        self.assertFalse(skim.citation_eligible)

    def test_standard_model_profile_requires_reasoning_or_explicit_fallback(self):
        configured = replace(
            self.settings,
            model="openai:fast-fixture",
            model_base_url="http://127.0.0.1:8000/v1",
        )
        environment = {
            "OPENAI_API_KEY": "fixture-key",
            "HARNESS_FAST_MODEL": "",
            "HARNESS_FAST_MODEL_BASE_URL": "",
            "HARNESS_FAST_API_KEY": "",
            "HARNESS_REASONING_MODEL": "",
            "HARNESS_REASONING_MODEL_BASE_URL": "",
            "HARNESS_REASONING_API_KEY": "",
        }
        with mock.patch.dict("os.environ", environment, clear=False):
            with self.assertRaisesRegex(ValueError, "requires a reasoning-model"):
                ReviewModelBundle.from_env(
                    configured,
                    allow_single_model_fallback=False,
                    require_reasoning=True,
                )
            bundle = ReviewModelBundle.from_env(
                configured,
                allow_single_model_fallback=True,
                require_reasoning=True,
            )
        self.assertTrue(bundle.single_model_fallback)
        self.assertEqual(bundle.fast.model, bundle.reasoning.model)

    def test_provider_failure_is_isolated_and_results_are_stably_sorted(self):
        config = self._config(run_id="provider-run")
        store = ReviewArtifactStore(self.settings, config)
        tavily = FakeTavilyProvider()
        registry = ReviewProviderRegistry(
            self.settings.repository_root,
            store.working_root,
            providers={
                "tavily": tavily,
                "github": FakeFailingGitHubProvider(),
            },
            network_concurrency=2,
        )
        queries = (
            RetrievalQuery(
                id="R01Q01",
                round=1,
                provider="github",
                text="sparse attention repository",
                purpose="Find an official implementation.",
            ),
            RetrievalQuery(
                id="R01Q02",
                round=1,
                provider="tavily",
                text="sparse attention official evidence",
                purpose="Find official technical evidence.",
            ),
        )
        batch = asyncio.run(
            registry.search(queries, limits={"github": 1, "tavily": 2})
        )
        self.assertEqual(2, len(batch.sources))
        self.assertEqual(sorted(item.source_id for item in batch.sources), [item.source_id for item in batch.sources])
        self.assertEqual("R01Q01", batch.errors[0][0])
        repeated = asyncio.run(
            registry.search(queries[1:], limits={"tavily": 2})
        )
        self.assertEqual(2, len(repeated.sources))
        self.assertEqual(1, tavily.calls)

    def test_semantic_scholar_enrichment_preserves_discovery_identity(self):
        source = SourceRecord(
            source_id="paper:arxiv:2401.00001",
            source_type="paper",
            provider="deepxiv",
            title="Sparse Attention Fixture",
            canonical_url="https://arxiv.org/abs/2401.00001",
            arxiv_id="2401.00001",
            pdf_url="https://arxiv.org/pdf/2401.00001",
            discoveries=(
                DiscoveryRecord(
                    query_id="R01Q01",
                    provider="deepxiv",
                    rank=1,
                    retrieved_at=NOW,
                ),
            ),
        )
        payload = {
            "paperId": "s2-fixture-paper",
            "corpusId": 123,
            "title": "Sparse Attention Fixture",
            "abstract": "A fixture abstract.",
            "year": 2024,
            "authors": [{"authorId": "1", "name": "Ada Example"}],
            "url": "https://www.semanticscholar.org/paper/s2-fixture-paper",
            "venue": "FixtureConf",
            "externalIds": {
                "ArXiv": "2401.00001v2",
                "DOI": "10.0000/FIXTURE",
            },
            "openAccessPdf": {"url": "https://arxiv.org/pdf/2401.00001"},
            "citationCount": 7,
            "influentialCitationCount": 2,
            "publicationDate": "2024-01-02",
        }
        with mock.patch(
            "research_harness.review_providers._semantic_scholar_paper_details",
            return_value=payload,
        ) as request:
            enriched = asyncio.run(
                SemanticScholarProvider("fixture-s2-key").enrich(source)
            )
        request.assert_called_once_with(
            "ARXIV:2401.00001",
            "fixture-s2-key",
        )
        self.assertEqual(source.source_id, enriched.source_id)
        self.assertEqual("deepxiv", enriched.provider)
        self.assertEqual("10.0000/fixture", enriched.doi)
        self.assertEqual("Ada Example", enriched.authors[0])
        self.assertEqual(7, enriched.metadata["citation_count"])
        self.assertEqual(2, enriched.metadata["influential_citation_count"])

    def test_semantic_scholar_enrichment_uses_one_bounded_detail_request(self):
        payload = json.dumps(
            {"paperId": "s2-fixture", "title": "Fixture"}
        ).encode("utf-8")

        class Response:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def iter_content(self, *, chunk_size):
                self.chunk_size = chunk_size
                yield payload

        with mock.patch(
            "research_harness.review_providers.requests.get",
            return_value=Response(),
        ) as requested:
            result = _semantic_scholar_paper_details(
                "ARXIV:2401.00001",
                "fixture-s2-key",
            )
        self.assertEqual("s2-fixture", result["paperId"])
        url = requested.call_args.args[0]
        request_options = requested.call_args.kwargs
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual("/graph/v1/paper/ARXIV:2401.00001", parsed.path)
        self.assertIn("authors", request_options["params"]["fields"])
        self.assertNotIn("query", request_options["params"])
        self.assertEqual(
            "fixture-s2-key",
            request_options["headers"]["x-api-key"],
        )
        self.assertTrue(request_options["stream"])

    def test_semantic_scholar_http_error_retains_safe_service_message(self):
        payload = b'{"message":"Too Many Requests","code":"429"}'

        class Response:
            status_code = 429

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def iter_content(self, *, chunk_size):
                del chunk_size
                yield payload

        with mock.patch(
            "research_harness.review_providers.requests.get",
            return_value=Response(),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "Semantic Scholar HTTP 429.*Too Many Requests",
            ):
                _semantic_scholar_paper_details(
                    "ARXIV:2401.00001",
                    "fixture-s2-key",
                )

    def test_semantic_scholar_is_not_a_retrieval_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ReviewProviderRegistry(
                root,
                root / "working",
                providers={"tavily": FakeTavilyProvider()},
                semantic_scholar=SemanticScholarProvider("fixture-s2-key"),
            )

        self.assertEqual(("tavily",), registry.names)

    def test_evidence_and_synthesis_contracts_reject_untraceable_claims(self):
        with self.assertRaisesRegex(ValueError, "experimental conditions"):
            EvidenceCard(
                card_id="bad-card",
                source_id="paper:one",
                source_url="https://example.org/paper-one",
                source_version="v1",
                source_sha256="a" * 64,
                statement="Latency is 10 ms.",
                attribution="author",
                evidence_type="experiment",
                status="located",
                metric="latency",
                value="10",
                unit="ms",
                locator=EvidenceLocator(kind="table", value="Table 2"),
            )
        draft = ReviewSynthesisDraft(
            title="Fixture",
            scope_summary="Fixture",
            core_findings=(
                SynthesisStatement(
                    statement_id="unknown-evidence",
                    statement="An unsupported statement.",
                    evidence_card_ids=("missing-card",),
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "unknown EvidenceCards"):
            validate_synthesis_references(draft, {})

    def test_cross_provider_paper_identity_deduplicates(self):
        first = SourceRecord(
            source_id="paper:first",
            source_type="paper",
            provider="deepxiv",
            title="Paper",
            canonical_url="https://arxiv.org/abs/2401.00001",
            arxiv_id="2401.00001",
            doi="10.0000/fixture",
            discoveries=(
                DiscoveryRecord(
                    query_id="Q1", provider="deepxiv", rank=1, retrieved_at=NOW
                ),
            ),
        )
        second = first.model_copy(
            update={
                "source_id": "paper:second",
                "provider": "semantic_scholar",
                "canonical_url": "https://export.arxiv.org/abs/2401.00001v2",
                "doi": None,
                "discoveries": (
                    DiscoveryRecord(
                        query_id="Q2",
                        provider="semantic_scholar",
                        rank=2,
                        retrieved_at=NOW,
                    ),
                ),
            }
        )
        merged = merge_sources((second, first))
        self.assertEqual(1, len(merged))
        self.assertEqual({"Q1", "Q2"}, {item.query_id for item in merged[0].discoveries})

    def test_same_paper_configurations_cannot_be_cross_paper_consensus(self):
        digest = "a" * 64
        cards = {
            "c1": EvidenceCard(
                card_id="c1",
                source_id="paper:one",
                source_url="https://example.org/paper-one",
                source_version="v1",
                source_sha256=digest,
                statement="Configuration SCCA reports a result.",
                attribution="author",
                evidence_type="experiment",
                status="located",
                locator=EvidenceLocator(kind="table", value="Table 1"),
            ),
            "c2": EvidenceCard(
                card_id="c2",
                source_id="paper:one",
                source_url="https://example.org/paper-one",
                source_version="v1",
                source_sha256=digest,
                statement="Configuration LongMixed reports a result.",
                attribution="author",
                evidence_type="experiment",
                status="located",
                locator=EvidenceLocator(kind="table", value="Table 1"),
            ),
        }
        assessment = NonConsensusAssessment(
            assessment_id="a1",
            question="Do the configurations establish consensus?",
            result="insufficient-evidence",
            comparable=True,
            independent_source_ids=("paper:one",),
            supporting_card_ids=("c1", "c2"),
            rationale="Both configurations are from the same paper.",
        )
        validate_nonconsensus_assessment(assessment, cards)
        with self.assertRaisesRegex(ValueError, "two comparable independent sources"):
            NonConsensusAssessment.model_validate(
                {**assessment.model_dump(mode="json"), "result": "supported-consensus"}
            )

        independent_but_incomparable = {
            "left": cards["c1"].model_copy(
                update={
                    "card_id": "left",
                    "source_id": "paper:left",
                    "model": "Model-A",
                    "benchmark": "RULER",
                    "metric": "accuracy",
                    "value": "0.80",
                    "conditions": {"hardware": "A100"},
                }
            ),
            "right": cards["c2"].model_copy(
                update={
                    "card_id": "right",
                    "source_id": "paper:right",
                    "model": "Model-B",
                    "benchmark": "LongBench",
                    "metric": "latency",
                    "value": "10 ms",
                    "conditions": {"hardware": "H100"},
                }
            ),
        }
        contested = NonConsensusAssessment(
            assessment_id="a2",
            question="Do two incomparable experiments establish a conflict?",
            result="contested",
            comparable=True,
            independent_source_ids=("paper:left", "paper:right"),
            supporting_card_ids=("left",),
            opposing_card_ids=("right",),
            rationale="The model proposed a conflict despite different settings.",
        )
        with self.assertRaisesRegex(ValueError, "comparable cross-source"):
            validate_nonconsensus_assessment(contested, independent_but_incomparable)

    def test_offline_review_generates_cited_report_without_wiki_writes(self):
        config = self._config()
        engine = FakeReviewEngine()
        wiki_before = (self.root / "wiki" / "sentinel.md").read_bytes()
        with ReviewController(
            self.settings,
            config=config,
            scope=self.scope,
            semantic_engine=engine,
            providers=self._providers(config),
        ) as controller:
            state = controller.start()
            resumed = controller.resume(mode="checkpoint")
        store = ReviewArtifactStore(self.settings, config)
        self.assertTrue(state["completed"])
        self.assertTrue(resumed["completed"])
        self.assertEqual(2, len(store.sources()))
        self.assertEqual(2, len(store.skims()))
        self.assertEqual(2, len(store.cards()))
        report = store.report_path.read_text(encoding="utf-8")
        self.assertIn("## 10. 证据索引", report)
        self.assertIn("[E1](#e1)", report)
        self.assertEqual(wiki_before, (self.root / "wiki" / "sentinel.md").read_bytes())

    def test_keyboard_interrupt_checkpoint_resumes_only_unfinished_batch(self):
        config = self._config(run_id="interrupt-run", thread_id="interrupt-thread")
        interrupted = FakeReviewEngine(interrupt_screen_once=True)
        with self.assertRaises(KeyboardInterrupt):
            with ReviewController(
                self.settings,
                config=config,
                scope=self.scope,
                semantic_engine=interrupted,
                providers=self._providers(config),
            ) as controller:
                controller.start()
        resumed_engine = FakeReviewEngine()
        with ReviewController(
            self.settings,
            config=config,
            scope=self.scope,
            semantic_engine=resumed_engine,
            providers=self._providers(config),
        ) as controller:
            state = controller.resume(mode="checkpoint")
        store = ReviewArtifactStore(self.settings, config)
        self.assertTrue(state["completed"])
        self.assertEqual(2, len(store.sources()))
        self.assertEqual(2, len(store.deep_read_completed()))

    def test_error_book_promotes_only_cross_run_recurrence(self):
        for run_id in ("run-a", "run-b"):
            path = self.root / ".harness" / "review-runs" / run_id / "state"
            path.mkdir(parents=True, exist_ok=True)
            event = ReviewErrorEvent(
                run_id=run_id,
                research_id=self.scope.research_id,
                stage="skim",
                recurrence_key="review-skim:structured-output",
                observed="Fixture output failed validation.",
                timestamp=NOW,
            )
            (path / "errors.jsonl").write_text(
                json.dumps(event.model_dump(mode="json")) + "\n",
                encoding="utf-8",
            )
        summaries = aggregate_review_error_book(
            self.settings, research_id=self.scope.research_id
        )
        self.assertEqual(1, len(summaries))
        self.assertEqual(("run-a", "run-b"), summaries[0].distinct_run_ids)
        generated = self.root / "error_book" / "_generated" / "review-recurrences.yaml"
        self.assertTrue(generated.is_file())

    def test_promotion_preview_has_no_wiki_side_effect(self):
        config = ReviewRunConfig.model_validate(
            {
                **self._config(run_id="promotion-run").model_dump(mode="json"),
                "max_promotions": 1,
            }
        )
        store = ReviewArtifactStore(self.settings, config)
        store.initialize()
        source = _web_source(1)
        store.write_sources((source,))
        manifest = PromotionManifest(
            research_id=config.research_id,
            run_id=config.run_id,
            max_promotions=1,
            items=(
                PromotionItem(
                    source_id=source.source_id,
                    evidence_card_ids=(),
                    rationale="Preview fixture",
                    approved=True,
                    status="approved",
                ),
            ),
            created_at=NOW,
        )
        store.write_promotion_manifest(manifest)
        wiki_before = (self.root / "wiki" / "sentinel.md").read_bytes()
        result = ReviewPromoter(self.settings, store).preview()
        self.assertFalse(result["wiki_changed"])
        self.assertEqual(wiki_before, (self.root / "wiki" / "sentinel.md").read_bytes())

    def test_promotion_verifies_approved_cards_before_staging(self):
        config = ReviewRunConfig.model_validate(
            {
                **self._config(run_id="verified-promotion-run").model_dump(mode="json"),
                "max_promotions": 1,
            }
        )
        store = ReviewArtifactStore(self.settings, config)
        store.initialize()
        digest = hashlib.sha256(b"fixture PDF text").hexdigest()
        source = SourceRecord(
            source_id="paper:arxiv:2401.00001",
            source_type="paper",
            provider="deepxiv",
            title="Fixture paper",
            canonical_url="https://arxiv.org/abs/2401.00001",
            arxiv_id="2401.00001",
            pdf_url="https://arxiv.org/pdf/2401.00001",
            local_path="sources/papers/fixture.pdf",
            content_sha256=digest,
            discoveries=(
                DiscoveryRecord(
                    query_id="R01Q01", provider="deepxiv", rank=1, retrieved_at=NOW
                ),
            ),
        )
        card = EvidenceCard(
            card_id="evidence-card-fixture",
            source_id=source.source_id,
            source_url=source.canonical_url,
            source_version="v1",
            source_sha256=digest,
            statement="The fixture contains a located result.",
            attribution="author",
            evidence_type="experiment",
            status="located",
            locator=EvidenceLocator(kind="pdf-page", value="1"),
            target_facets=FACETS,
        )
        store.write_sources((source,))
        store.write_cards((card,))
        store.write_material(
            SourceMaterial(
                source_id=source.source_id,
                media_type="pdf-text",
                sha256=digest,
                text="--- PDF p. 1 ---\nThe fixture contains a located result.",
                local_path=source.local_path,
                page_count=1,
                selected_pages=(1,),
                acquired_at=NOW,
            )
        )
        item = PromotionItem(
            source_id=source.source_id,
            evidence_card_ids=(card.card_id,),
            rationale="Report-critical fixture",
            approved=True,
            status="approved",
        )
        result = ReviewPromoter(self.settings, store)._verify_evidence_cards(
            item=item,
            source=source,
            cards={card.card_id: card},
            verifier=FakePromotionVerifier(),
        )
        self.assertEqual(1, result["decisions"])
        self.assertTrue((self.root / result["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
