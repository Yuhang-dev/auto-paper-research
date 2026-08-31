from __future__ import annotations

import json
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from research_harness.config import (
    HarnessSettings,
    REPOSITORY_ROOT,
    resolve_database_path,
)
from research_harness.cli import main as cli_main
from research_harness.graph import ResearchHarness
from research_harness.memory import list_notes, recall_notes, remember_note
from research_harness.model_client import create_chat_model
from research_harness.persistence import HarnessPersistence
from research_harness.research_control import (
    AutonomousResearchController,
    ResearchController,
)
from research_harness.research_evaluation import (
    check_done,
    decide_next_action,
    evaluate_gaps,
    inspect_research,
    load_done_criteria,
    measure_progress,
)
from research_harness.research_execution import DeterministicActionExecutor
from research_harness.research_models import (
    ActionAttemptStats,
    DoneCriteria,
    NonConsensusAssessment,
    ResearchActionResult,
    ResearchDecision,
    ResearchGap,
    SearchYield,
)
from research_harness.skill_registry import SkillRegistry, SkillRegistryError
from research_harness.tools import build_tools


class ScriptedModel:
    """Minimal tool-calling model double; it never touches a provider."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.seen_messages = []
        self.bound_tool_names = []

    def bind_tools(self, tools):
        self.bound_tool_names = [item.name for item in tools]
        return self

    def invoke(self, messages, *args, **kwargs):
        self.seen_messages.append(list(messages))
        if not self.responses:
            raise AssertionError("ScriptedModel has no response left")
        return self.responses.pop(0)


class HarnessTestCase(unittest.TestCase):
    def setUp(self) -> None:
        (REPOSITORY_ROOT / "tmp").mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT / "tmp")
        self.database_path = Path(self.temporary.name) / "harness.sqlite3"
        self.settings = HarnessSettings(
            database_path=self.database_path,
            context_token_budget=1024,
            max_tool_iterations=4,
            tool_output_chars=8000,
        )
        self.settings.validate()

    def tearDown(self) -> None:
        self.temporary.cleanup()


class ConfigurationAndPersistenceTests(HarnessTestCase):
    def test_default_database_is_on_project_d_drive(self) -> None:
        settings = HarnessSettings.from_env()
        self.assertEqual("d:", settings.database_path.drive.casefold())
        self.assertTrue(str(settings.database_path).startswith(str(REPOSITORY_ROOT)))

    def test_c_drive_database_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be on C"):
            resolve_database_path(r"C:\research\memory.sqlite3")

    def test_openai_compatible_local_model_configuration(self) -> None:
        settings = replace(
            self.settings,
            model="openai:qwen2.5:32b",
            model_base_url="http://127.0.0.1:8000/v1",
        )
        settings.validate()
        self.assertEqual("qwen2.5:32b", settings.openai_model_name)
        self.assertEqual("127.0.0.1", settings.model_endpoint_host)

        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "local-test-key"},
            clear=False,
        ):
            model = create_chat_model(settings)
        self.assertEqual("ChatOpenAI", type(model).__name__)
        self.assertEqual("qwen2.5:32b", model.model_name)
        self.assertEqual(
            "http://127.0.0.1:8000/v1",
            str(model.openai_api_base).rstrip("/"),
        )

    def test_model_configuration_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "openai:<served-model-name>"):
            replace(
                self.settings,
                model="deepseek-v4-flash",
                model_base_url="https://api.deepseek.com",
            ).validate()
        with self.assertRaisesRegex(ValueError, "openai:<served-model-name>"):
            replace(
                self.settings,
                model="OpenAI:local-model",
                model_base_url="http://localhost:8000/v1",
            ).validate()
        with self.assertRaisesRegex(ValueError, "BASE_URL"):
            replace(self.settings, model="openai:local-model").validate()
        with self.assertRaisesRegex(ValueError, "absolute http"):
            replace(
                self.settings,
                model="openai:local-model",
                model_base_url="localhost:8000/v1",
            ).validate()
        with self.assertRaisesRegex(ValueError, "must not contain credentials"):
            replace(
                self.settings,
                model="openai:local-model",
                model_base_url="http://user:password@localhost:8000/v1",
            ).validate()
        with self.assertRaisesRegex(ValueError, "only deepseek-v4-flash"):
            replace(
                self.settings,
                model="openai:another-model",
                model_base_url="https://api.deepseek.com/v1",
            ).validate()
        with self.assertRaisesRegex(ValueError, "only deepseek-v4-flash"):
            replace(
                self.settings,
                model="openai:another-model",
                model_base_url="https://api.deepseek.com./v1",
            ).validate()
        replace(
            self.settings,
            model="openai:deepseek-v4-flash",
            model_base_url="https://api.deepseek.com",
        ).validate()

    def test_model_base_url_environment_contract(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "HARNESS_MODEL": "openai:local-model",
                "HARNESS_MODEL_BASE_URL": "http://localhost:8000/v1",
                "OPENAI_API_BASE": "http://legacy.invalid/v1",
            },
            clear=True,
        ):
            settings = HarnessSettings.from_env()
        self.assertEqual("http://localhost:8000/v1", settings.model_base_url)

        with mock.patch.dict(
            os.environ,
            {
                "OPENAI_API_BASE": "http://localhost:8000/v1",
                "OPENAI_BASE_URL": "http://localhost:9000/v1",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "must match"):
                HarnessSettings.from_env()

    def test_model_client_requires_api_key(self) -> None:
        settings = replace(
            self.settings,
            model="openai:local-model",
            model_base_url="http://127.0.0.1:8000/v1",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
                create_chat_model(settings)

    def test_socket_model_requires_explicit_network_authority(self) -> None:
        settings = replace(
            self.settings,
            model="openai:local-model",
            model_base_url="http://127.0.0.1:8000/v1",
        )
        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "local-test-key"},
            clear=False,
        ):
            with ResearchHarness(settings) as harness:
                with self.assertRaisesRegex(ValueError, "including localhost"):
                    harness.invoke(
                        "do not send this prompt",
                        thread_id="local-model-denied",
                        allow_network=False,
                    )

    def test_sqlite_checkpoint_and_store_share_one_file(self) -> None:
        with HarnessPersistence(self.settings) as persistence:
            self.assertIsNotNone(persistence.checkpointer)
            self.assertIsNotNone(persistence.store)
            counts = persistence.checkpoint_counts()
            self.assertEqual(0, counts["threads"])
        self.assertTrue(self.database_path.is_file())
        self.assertEqual("d:", self.database_path.drive.casefold())

    def test_research_memory_deduplicates_and_survives_reopen(self) -> None:
        with HarnessPersistence(self.settings) as persistence:
            assert persistence.store is not None
            first = remember_note(
                persistence.store,
                "workspace-a",
                text="RULER requires scoped context-length reporting.",
                topic="evaluation",
                evidence_ids=["benchmark:ruler"],
            )
            second = remember_note(
                persistence.store,
                "workspace-a",
                text="RULER requires scoped context-length reporting.",
                topic="evaluation",
                evidence_ids=["benchmark:ruler"],
            )
            self.assertEqual(first["key"], second["key"])
            self.assertEqual(2, second["confirmations"])
        with HarnessPersistence(self.settings) as persistence:
            assert persistence.store is not None
            records = recall_notes(
                persistence.store,
                "workspace-a",
                query="RULER context",
            )
            self.assertEqual(1, len(records))
            self.assertEqual(2, records[0]["confirmations"])


class SkillRegistryTests(HarnessTestCase):
    def test_registry_discovers_and_parses_repository_skills(self) -> None:
        registry = SkillRegistry(self.settings.skills_root)
        self.assertEqual(
            (
                "analyze-claims",
                "evidence-extract",
                "ingest-paper",
                "project-audit",
                "review-synthesize",
                "revise-evidence",
                "search-paper",
                "source-skim",
                "verify-evidence",
                "wiki-link",
            ),
            registry.names,
        )

        search = registry.get("search-paper")
        self.assertTrue(search.instructions.startswith("# Search Paper"))
        self.assertIn("traceable", search.description)
        self.assertIn(
            "references/search-strategy.md",
            {item.relative_path for item in search.resources},
        )
        self.assertTrue(search.references)
        self.assertTrue(search.scripts)

    def test_reference_content_is_read_only_on_demand(self) -> None:
        registry = SkillRegistry(self.settings.skills_root)
        search = registry.get("search-paper")
        content = search.read_reference("search-strategy.md")
        self.assertIn("search", content.casefold())
        with self.assertRaisesRegex(SkillRegistryError, "Unsafe"):
            search.read_resource("../ingest-paper/SKILL.md")
        with self.assertRaisesRegex(SkillRegistryError, "Unsafe"):
            search.read_resource(".")

    def test_non_utf8_resource_does_not_break_discovery(self) -> None:
        skills_root = Path(self.temporary.name) / "skills"
        skill_root = skills_root / "fixture-skill"
        (skill_root / "assets").mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            "---\nname: fixture-skill\ndescription: Fixture.\n---\n\n# Fixture\n",
            encoding="utf-8",
        )
        (skill_root / "assets" / "binary.dat").write_bytes(b"\xff\xfe")

        registry = SkillRegistry(skills_root)
        fixture = registry.get("fixture-skill")
        self.assertEqual(1, len(fixture.assets))
        with self.assertRaisesRegex(SkillRegistryError, "not valid UTF-8"):
            fixture.read_resource("assets/binary.dat")

    def test_directory_name_must_match_frontmatter_name(self) -> None:
        skills_root = Path(self.temporary.name) / "skills"
        skill_root = skills_root / "wrong-directory"
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            "---\nname: declared-name\ndescription: Fixture.\n---\n\n# Fixture\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SkillRegistryError, "must match"):
            SkillRegistry(skills_root)

    def test_harness_exposes_registry_without_automatic_routing(self) -> None:
        harness = ResearchHarness(
            self.settings,
            model=ScriptedModel([AIMessage(content="unused")]),
        )
        self.assertEqual(
            (
                "analyze-claims",
                "evidence-extract",
                "ingest-paper",
                "project-audit",
                "review-synthesize",
                "revise-evidence",
                "search-paper",
                "source-skim",
                "verify-evidence",
                "wiki-link",
            ),
            harness.skill_registry.names,
        )
        self.assertFalse(hasattr(harness, "skill_router"))


class DeterministicToolTests(HarnessTestCase):
    def test_wiki_search_tool_wraps_existing_engine(self) -> None:
        by_name = {item.name: item for item in build_tools(self.settings)}
        payload = json.loads(by_name["wiki_search"].invoke({"query": "LongLoRA"}))
        self.assertTrue(payload["ok"])
        self.assertEqual("paper:longlora", payload["entities"][0]["id"])

    def test_search_run_status_is_read_only_and_structured(self) -> None:
        by_name = {item.name: item for item in build_tools(self.settings)}
        payload = json.loads(
            by_name["search_run_status"].invoke(
                {
                    "run_path": (
                        "research/long-context-sparse-models/search-runs/"
                        "v0-discovery.yaml"
                    )
                }
            )
        )
        self.assertTrue(payload["ok"])
        self.assertIn("candidate_count", payload)
        self.assertIn("query_statuses", payload)

    def test_deepxiv_preview_is_offline_and_does_not_need_a_token(self) -> None:
        by_name = {item.name: item for item in build_tools(self.settings)}
        payload = json.loads(
            by_name["deepxiv_search_run"].invoke(
                {
                    "run_path": (
                        "research/long-context-sparse-models/search-runs/"
                        "v0-discovery.yaml"
                    ),
                    "dry_run": True,
                }
            )
        )
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dry_run"])
        self.assertIn("Q01", payload["stdout"])


class ResearchEvaluationTests(HarnessTestCase):
    def test_snapshot_measures_real_wiki_and_search_state(self) -> None:
        snapshot = inspect_research(self.settings, "long-context-sparse-models")
        self.assertEqual(1, snapshot.corpus.search_run_count)
        self.assertEqual(8, snapshot.corpus.pending_queries)
        self.assertEqual(0, snapshot.corpus.unique_candidates)
        self.assertEqual(1, snapshot.corpus.ingested_papers)
        self.assertEqual(0, snapshot.corpus.verified_papers)
        self.assertEqual(10, len(snapshot.taxonomy.required_facets))
        self.assertTrue(
            all(
                status == "missing"
                for status in snapshot.taxonomy.candidate_facet_coverage.values()
            )
        )
        self.assertTrue(
            all(
                status == "missing"
                for status in snapshot.taxonomy.evidence_facet_coverage.values()
            )
        )
        self.assertEqual(0, snapshot.evidence.experiments_total)
        self.assertEqual(0, snapshot.evidence.nonconsensus_assessments)
        self.assertEqual(0, snapshot.evidence.verified_nonconsensus_assessments)
        self.assertEqual(0, snapshot.quality.schema_errors)
        self.assertEqual(8, snapshot.quality.schema_warnings)

    def test_snapshot_derives_evidence_facets_and_assessments_from_wiki(self) -> None:
        fixture_settings = replace(
            self.settings,
            wiki_root=(
                REPOSITORY_ROOT / "tools" / "wiki" / "tests" / "fixtures" / "wiki"
            ),
            wiki_meta_root=REPOSITORY_ROOT / "wiki" / "_meta",
        )
        fixture_settings.validate()
        snapshot = inspect_research(fixture_settings, "long-context-sparse-models")
        self.assertEqual(
            "partial", snapshot.taxonomy.evidence_facet_coverage["technical-taxonomy"]
        )
        self.assertEqual(
            "covered", snapshot.taxonomy.evidence_facet_coverage["quality-metrics"]
        )
        self.assertEqual(2, snapshot.taxonomy.evidence_facet_counts["quality-metrics"])
        self.assertEqual(1, snapshot.evidence.nonconsensus_assessments)
        self.assertEqual(1, snapshot.evidence.verified_nonconsensus_assessments)
        self.assertEqual(
            1,
            snapshot.evidence.assessments_by_result["insufficient-evidence"],
        )
        self.assertEqual(0, snapshot.quality.schema_errors)

    def test_gap_evaluation_selects_existing_search_plan(self) -> None:
        snapshot = inspect_research(self.settings, "long-context-sparse-models")
        criteria = load_done_criteria(self.settings, "long-context-sparse-models")
        gaps = evaluate_gaps(snapshot, criteria)
        evaluation = check_done(snapshot, criteria, gaps, research_iteration=1)
        decision = decide_next_action(gaps, evaluation)

        self.assertFalse(evaluation.complete)
        self.assertFalse(evaluation.coverage_passed)
        self.assertFalse(evaluation.quality_passed)
        self.assertFalse(evaluation.saturation_passed)
        self.assertEqual("planned-search-queries", gaps[0].key)
        self.assertEqual("search", decision.action)
        self.assertEqual(gaps[0].id, decision.target_gap_id)

    def test_draft_criteria_prevents_automatic_finish(self) -> None:
        snapshot = inspect_research(self.settings, "long-context-sparse-models")
        saturated_corpus = snapshot.corpus.model_copy(
            update={
                "search_yields": (
                    SearchYield(
                        run_id="fixture",
                        round=1,
                        new_core_papers=0,
                        novelty_yield=0,
                    ),
                )
            }
        )
        saturated = snapshot.model_copy(update={"corpus": saturated_corpus})
        draft = DoneCriteria(
            status="draft",
            facet_requirements={},
            minimum_method_families=0,
            minimum_core_candidates=0,
            minimum_ingested_papers=0,
            minimum_verified_papers=0,
            minimum_experiments=0,
            minimum_verified_claims=0,
            minimum_evidence_locator_ratio=0,
            require_nonconsensus_review=False,
            context_bucket_requirements={},
            engineering_metric_requirements={},
            minimum_completed_search_rounds=1,
            saturation_window=1,
        )
        draft_check = check_done(saturated, draft, (), research_iteration=1)
        self.assertFalse(draft_check.complete)
        self.assertIn("status is draft", "\n".join(draft_check.failures))

        active = draft.model_copy(update={"status": "active"})
        active_check = check_done(saturated, active, (), research_iteration=1)
        self.assertTrue(active_check.complete)
        self.assertEqual("completed", active_check.stop_reason)
        self.assertEqual("finish", decide_next_action((), active_check).action)

    def test_invalid_search_round_does_not_satisfy_saturation(self) -> None:
        snapshot = inspect_research(self.settings, "long-context-sparse-models")
        invalid_corpus = snapshot.corpus.model_copy(
            update={
                "search_yields": (
                    SearchYield(
                        run_id="failed-provider-round",
                        round=1,
                        new_core_papers=0,
                        valid_discovery_round=False,
                        invalid_reasons=("query-round-not-terminal-or-has-failure",),
                        query_statuses={"failed": 1},
                        screening_complete=True,
                    ),
                )
            }
        )
        criteria = DoneCriteria(
            status="active",
            facet_requirements={},
            minimum_method_families=0,
            minimum_core_candidates=0,
            minimum_ingested_papers=0,
            minimum_verified_papers=0,
            minimum_experiments=0,
            minimum_verified_claims=0,
            minimum_evidence_locator_ratio=0,
            require_nonconsensus_review=False,
            context_bucket_requirements={},
            engineering_metric_requirements={},
            minimum_completed_search_rounds=1,
            saturation_window=1,
        )
        staged = snapshot.model_copy(update={"corpus": invalid_corpus})
        evaluation = check_done(staged, criteria, (), research_iteration=1)
        self.assertFalse(evaluation.saturation_passed)
        self.assertIn("valid completed discovery rounds 0", "\n".join(evaluation.failures))

    def test_search_round_validity_is_computed_per_round(self) -> None:
        research_root = Path(self.temporary.name) / "research"
        search_root = research_root / "round-validity" / "search-runs"
        search_root.mkdir(parents=True)
        payload = {
            "schema_version": "0.1",
            "run": {"id": "two-rounds", "status": "partial", "round": 2},
            "scope": {"required_facets": []},
            "queries": [
                {
                    "id": "Q01",
                    "round": 1,
                    "execution": {
                        "status": "succeeded",
                        "retained_count": 1,
                    },
                },
                {
                    "id": "Q02",
                    "round": 2,
                    "execution": {"status": "failed", "retained_count": 0},
                },
            ],
            "candidates": [
                {
                    "candidate_id": "arxiv:fake",
                    "discovered_by": [{"query_id": "Q01"}],
                    "relevance": {"label": "core"},
                    "review_state": "abstract-screened",
                }
            ],
            "coverage": {
                "metrics": {
                    "new_core_by_round": [
                        {"round": 1, "count": 1},
                        {"round": 2, "count": 0},
                    ]
                }
            },
        }
        (search_root / "two-rounds.yaml").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        settings = replace(self.settings, research_root=research_root)
        settings.validate()
        snapshot = inspect_research(settings, "round-validity")
        yields = {item.round: item for item in snapshot.corpus.search_yields}
        self.assertTrue(yields[1].valid_discovery_round)
        self.assertEqual({"succeeded": 1}, yields[1].query_statuses)
        self.assertFalse(yields[2].valid_discovery_round)
        self.assertIn(
            "query-round-not-terminal-or-has-failure",
            yields[2].invalid_reasons,
        )

    def test_facet_gap_routes_candidate_and_evidence_stages_differently(self) -> None:
        snapshot = inspect_research(self.settings, "long-context-sparse-models")
        criteria = DoneCriteria(
            facet_requirements={"latency-throughput": "covered"},
            minimum_method_families=0,
            minimum_core_candidates=0,
            minimum_ingested_papers=0,
            minimum_verified_papers=0,
            minimum_experiments=0,
            minimum_verified_claims=0,
            minimum_evidence_locator_ratio=0,
            require_nonconsensus_review=False,
            context_bucket_requirements={},
            engineering_metric_requirements={},
            minimum_completed_search_rounds=1,
            saturation_window=1,
        )

        def facet_action(candidate_status, evidence_status):
            taxonomy = snapshot.taxonomy.model_copy(
                update={
                    "candidate_facet_coverage": {
                        **snapshot.taxonomy.candidate_facet_coverage,
                        "latency-throughput": candidate_status,
                    },
                    "candidate_facet_counts": {
                        **snapshot.taxonomy.candidate_facet_counts,
                        "latency-throughput": int(candidate_status != "missing"),
                    },
                    "evidence_facet_coverage": {
                        **snapshot.taxonomy.evidence_facet_coverage,
                        "latency-throughput": evidence_status,
                    },
                    "evidence_facet_counts": {
                        **snapshot.taxonomy.evidence_facet_counts,
                        "latency-throughput": int(evidence_status == "covered"),
                    },
                }
            )
            staged = snapshot.model_copy(update={"taxonomy": taxonomy})
            facet_gap = next(
                gap
                for gap in evaluate_gaps(staged, criteria)
                if gap.key == "facet:latency-throughput"
            )
            self.assertTrue(facet_gap.blocking)
            return facet_gap.recommended_action

        self.assertEqual("search", facet_action("missing", "missing"))
        self.assertEqual("ingest", facet_action("covered", "missing"))
        self.assertEqual("verify", facet_action("covered", "partial"))

    def test_done_uses_evidence_coverage_and_open_blocking_gaps(self) -> None:
        snapshot = inspect_research(self.settings, "long-context-sparse-models")
        saturated = snapshot.model_copy(
            update={
                "corpus": snapshot.corpus.model_copy(
                    update={
                        "search_yields": (
                            SearchYield(
                                run_id="fixture",
                                round=1,
                                new_core_papers=0,
                                novelty_yield=0,
                            ),
                        )
                    }
                ),
                "taxonomy": snapshot.taxonomy.model_copy(
                    update={
                        "candidate_facet_coverage": {"quality-metrics": "covered"},
                        "candidate_facet_counts": {"quality-metrics": 4},
                        "evidence_facet_coverage": {"quality-metrics": "missing"},
                        "evidence_facet_counts": {"quality-metrics": 0},
                        "facet_next_queries": {"quality-metrics": ()},
                    }
                ),
            }
        )
        criteria = DoneCriteria(
            status="active",
            facet_requirements={"quality-metrics": "covered"},
            minimum_method_families=0,
            minimum_core_candidates=0,
            minimum_ingested_papers=0,
            minimum_verified_papers=0,
            minimum_experiments=0,
            minimum_verified_claims=0,
            minimum_evidence_locator_ratio=0,
            require_nonconsensus_review=False,
            context_bucket_requirements={},
            engineering_metric_requirements={},
            minimum_completed_search_rounds=1,
            saturation_window=1,
        )
        gaps = evaluate_gaps(saturated, criteria)
        done = check_done(saturated, criteria, gaps, research_iteration=0)
        self.assertFalse(done.coverage_passed)
        self.assertFalse(done.blocking_gaps_passed)
        self.assertTrue(done.blocking_gap_ids)
        self.assertFalse(done.complete)

        noncoverage_blocker = ResearchGap(
            id="gap-manual-blocker",
            key="manual-blocker",
            type="evidence_gap",
            question="Is a core question still unresolved?",
            priority=0.5,
            reasons=("Explicit review policy marked this gap blocking.",),
            recommended_action="verify",
            blocking=True,
        )
        evidence_taxonomy = saturated.taxonomy.model_copy(
            update={
                "evidence_facet_coverage": {"quality-metrics": "covered"},
                "evidence_facet_counts": {"quality-metrics": 1},
            }
        )
        evidence_ready = saturated.model_copy(update={"taxonomy": evidence_taxonomy})
        blocked = check_done(
            evidence_ready,
            criteria,
            (noncoverage_blocker,),
            research_iteration=0,
        )
        self.assertTrue(blocked.coverage_passed)
        self.assertFalse(blocked.blocking_gaps_passed)
        self.assertFalse(blocked.complete)

    def test_nonconsensus_assessment_accepts_insufficient_evidence(self) -> None:
        assessment = NonConsensusAssessment(
            id="assessment:context-damage",
            question="Does degradation increase with context length?",
            result="insufficient-evidence",
            claim_ids=("claim:quality-preserved",),
            evidence_ids=("experiment:alpha-ruler-32k",),
            benchmark_ids=("benchmark:ruler",),
            rationale="Only one controlled context bucket is available.",
            verified=True,
        )
        self.assertEqual("insufficient-evidence", assessment.result)
        self.assertTrue(assessment.verified)

    def test_progress_counts_only_attempted_no_progress_rounds(self) -> None:
        snapshot = inspect_research(self.settings, "long-context-sparse-models")
        passive = measure_progress(
            snapshot,
            snapshot,
            previous_no_progress_rounds=1,
            action_attempted=False,
        )
        attempted = measure_progress(
            snapshot,
            snapshot,
            previous_no_progress_rounds=1,
            action_attempted=True,
        )
        self.assertEqual(1, passive.no_progress_rounds)
        self.assertEqual(2, attempted.no_progress_rounds)

        advanced = snapshot.model_copy(
            update={
                "snapshot_id": "changed",
                "corpus": snapshot.corpus.model_copy(
                    update={"core_candidates": snapshot.corpus.core_candidates + 1}
                ),
            }
        )
        progress = measure_progress(
            snapshot,
            advanced,
            previous_no_progress_rounds=2,
            action_attempted=True,
        )
        self.assertTrue(progress.made_progress)
        self.assertEqual(1.0, progress.novelty_yield)
        self.assertEqual(0, progress.no_progress_rounds)

        queued = snapshot.model_copy(
            update={
                "corpus": snapshot.corpus.model_copy(update={"selected_for_ingest": 1})
            }
        )
        dequeued = snapshot.model_copy(update={"snapshot_id": "queue-drained"})
        queue_progress = measure_progress(
            queued,
            dequeued,
            action_attempted=True,
        )
        self.assertTrue(queue_progress.made_progress)
        self.assertEqual(0.5, queue_progress.novelty_yield)
        self.assertIn("search-runs", queue_progress.changed_sources)

    def test_action_frontier_skips_unsupported_and_stalls_per_pair(self) -> None:
        snapshot = inspect_research(self.settings, "long-context-sparse-models")
        criteria = load_done_criteria(self.settings, "long-context-sparse-models")
        ingest_gap = ResearchGap(
            id="gap-ingest",
            key="ingest",
            type="workflow_gap",
            question="Ingest a selected paper?",
            priority=0.99,
            reasons=("candidate ready",),
            recommended_action="ingest",
        )
        search_gap = ResearchGap(
            id="gap-search",
            key="search",
            type="coverage_gap",
            question="Search another facet?",
            priority=0.8,
            reasons=("facet missing",),
            recommended_action="search",
        )
        gaps = (ingest_gap, search_gap)
        evaluation = check_done(
            snapshot,
            criteria,
            gaps,
            research_iteration=0,
            no_progress_rounds=criteria.max_no_progress_rounds,
            supported_actions=frozenset({"search"}),
        )
        self.assertIsNone(evaluation.stop_reason)
        decision = decide_next_action(
            gaps,
            evaluation,
            supported_actions=frozenset({"search"}),
        )
        self.assertEqual("search", decision.action)
        self.assertEqual(search_gap.id, decision.target_gap_id)

        attempt_key = f"{search_gap.id}:search"
        attempts = {
            attempt_key: ActionAttemptStats(
                attempt_key=attempt_key,
                target_gap_id=search_gap.id,
                action="search",
                attempts=criteria.max_no_progress_rounds,
                no_progress=criteria.max_no_progress_rounds,
            ).model_dump(mode="json")
        }
        stalled = check_done(
            snapshot,
            criteria,
            gaps,
            research_iteration=0,
            attempts_by_gap_action=attempts,
            supported_actions=frozenset({"search"}),
        )
        self.assertEqual("stalled", stalled.stop_reason)

        blocked = check_done(
            snapshot,
            criteria,
            (ingest_gap,),
            research_iteration=0,
            supported_actions=frozenset({"search"}),
        )
        self.assertEqual("blocked", blocked.stop_reason)

    def test_research_control_pass_is_checkpointed_without_a_model(self) -> None:
        with ResearchController(
            self.settings,
            research_id="long-context-sparse-models",
        ) as controller:
            first = controller.invoke(thread_id="outer-loop-test")
            second = controller.invoke(thread_id="outer-loop-test")
            checkpoint = controller.get_state("outer-loop-test")
        self.assertEqual(1, first["control_passes"])
        self.assertEqual("search", first["decision"]["action"])
        self.assertEqual(2, second["control_passes"])
        self.assertEqual(0, second.get("research_iterations", 0))
        self.assertEqual(0, second.get("no_progress_rounds", 0))
        self.assertEqual(0, second.get("tool_calls", 0))
        self.assertEqual(2, len(second["decision_history"]))
        self.assertEqual(0, len(second.get("action_history", [])))
        self.assertEqual(2, len(second["gap_history"]))
        self.assertEqual("planned", second["phase"])
        self.assertEqual("planned-search-queries", second["current_gap"]["key"])
        self.assertEqual(2, checkpoint.values["control_passes"])

    def test_autonomous_loop_blocks_before_writing_without_network_authorization(
        self,
    ) -> None:
        run_path = (
            REPOSITORY_ROOT
            / "research"
            / "long-context-sparse-models"
            / "search-runs"
            / "v0-discovery.yaml"
        )
        before = run_path.read_bytes()
        with AutonomousResearchController(
            self.settings,
            research_id="long-context-sparse-models",
        ) as controller:
            state = controller.invoke(
                thread_id="autonomous-offline",
                allow_network=False,
            )
        self.assertEqual(before, run_path.read_bytes())
        self.assertEqual("blocked", state["stop_reason"])
        self.assertEqual("precondition_blocked", state["action_result"]["outcome"])
        self.assertEqual(["network-disabled"], state["action_result"]["error_codes"])
        self.assertFalse(state["action_result"]["attempted"])
        self.assertEqual(0, state.get("research_iterations", 0))
        self.assertEqual(0, state.get("tool_calls", 0))

    def test_search_executor_missing_token_is_a_non_mutating_precondition(self) -> None:
        run_path = (
            REPOSITORY_ROOT
            / "research"
            / "long-context-sparse-models"
            / "search-runs"
            / "v0-discovery.yaml"
        )
        before = run_path.read_bytes()
        snapshot = inspect_research(self.settings, "long-context-sparse-models")
        criteria = load_done_criteria(self.settings, "long-context-sparse-models")
        gap = evaluate_gaps(snapshot, criteria)[0]
        decision = ResearchDecision(
            action="search",
            target_gap_id=gap.id,
            reason="test planned search",
            expected_information_gain=0.9,
        )
        executor = DeterministicActionExecutor(self.settings)
        with mock.patch.dict(os.environ, {}, clear=True):
            result = executor.execute(
                decision=decision,
                gap=gap,
                snapshot=snapshot,
                action_id="action-token-preflight",
                allow_network=True,
            )
        self.assertEqual(before, run_path.read_bytes())
        self.assertEqual("precondition_blocked", result.outcome)
        self.assertEqual(("deepxiv-token-missing",), result.error_codes)
        self.assertFalse(result.attempted)

    def test_autonomous_loop_reobserves_and_tracks_attempt_per_gap_action(self) -> None:
        initial = inspect_research(self.settings, "long-context-sparse-models")
        after = initial.model_copy(
            update={
                "snapshot_id": "snapshot-after-search",
                "corpus": initial.corpus.model_copy(
                    update={
                        "pending_queries": 0,
                        "planned_query_ids": (),
                        "query_statuses": {"succeeded": initial.corpus.query_count},
                        "unique_candidates": initial.corpus.unique_candidates + 4,
                    }
                ),
            }
        )
        snapshots = [initial, after]

        def inspector(settings, research_id):
            del settings, research_id
            return snapshots.pop(0) if snapshots else after

        class Executor:
            calls = 0
            supported_actions = frozenset({"search"})

            def execute(self, *, decision, gap, snapshot, action_id, allow_network):
                del gap, snapshot, allow_network
                self.calls += 1
                if self.calls == 1:
                    return ResearchActionResult(
                        action_id=action_id,
                        action=decision.action,
                        target_gap_id=decision.target_gap_id,
                        status="success",
                        outcome="positive",
                        attempted=True,
                        tool_calls=2,
                        changed_sources=("research/fixture.yaml",),
                        metrics={"new_candidates": 4},
                    )
                return ResearchActionResult(
                    action_id=action_id,
                    action=decision.action,
                    target_gap_id=decision.target_gap_id,
                    status="blocked",
                    outcome="precondition_blocked",
                    attempted=False,
                    error_codes=("search-plan-required",),
                )

        criteria = load_done_criteria(self.settings, "long-context-sparse-models")
        initial_gap = evaluate_gaps(initial, criteria)[0]
        executor = Executor()
        with AutonomousResearchController(
            self.settings,
            research_id="long-context-sparse-models",
            action_executor=executor,
            inspector=inspector,
        ) as controller:
            state = controller.invoke(
                thread_id="autonomous-progress",
                allow_network=False,
            )
        attempt_key = f"{initial_gap.id}:search"
        self.assertEqual(2, state["control_passes"])
        self.assertEqual(1, state["research_iterations"])
        self.assertEqual(2, state["tool_calls"])
        self.assertTrue(state["progress"]["made_progress"])
        self.assertEqual(2.0, state["progress"]["progress_score"])
        self.assertEqual(1, state["attempts_by_gap_action"][attempt_key]["attempts"])
        self.assertEqual(0, state["attempts_by_gap_action"][attempt_key]["no_progress"])
        self.assertEqual(2, len(state["action_history"]))
        self.assertEqual("blocked", state["stop_reason"])

    def test_checkpoint_resume_continues_pending_node_without_reinspection(
        self,
    ) -> None:
        snapshot = inspect_research(self.settings, "long-context-sparse-models")
        inspection_count = 0

        def inspector(settings, research_id):
            nonlocal inspection_count
            del settings, research_id
            inspection_count += 1
            return snapshot

        class InterruptOnceExecutor:
            supported_actions = frozenset({"search"})

            def __init__(self) -> None:
                self.calls = 0

            def execute(
                self,
                *,
                decision,
                gap,
                snapshot,
                action_id,
                allow_network,
            ):
                del gap, snapshot, allow_network
                self.calls += 1
                if self.calls == 1:
                    raise KeyboardInterrupt("simulated outer-loop interruption")
                return ResearchActionResult(
                    action_id=action_id,
                    action=decision.action,
                    target_gap_id=decision.target_gap_id,
                    status="blocked",
                    outcome="precondition_blocked",
                    attempted=False,
                    error_codes=("fixture-stop",),
                )

        executor = InterruptOnceExecutor()
        with AutonomousResearchController(
            self.settings,
            research_id="long-context-sparse-models",
            action_executor=executor,
            inspector=inspector,
        ) as controller:
            with self.assertRaisesRegex(
                KeyboardInterrupt,
                "simulated outer-loop interruption",
            ):
                controller.invoke(
                    thread_id="checkpoint-resume",
                    allow_network=True,
                )
            pending = controller.get_state("checkpoint-resume")
            self.assertIn("execute_action", pending.next)
            self.assertEqual(
                self.settings.model_runtime_fingerprint,
                pending.values["model_runtime_fingerprint"],
            )

            with self.assertRaisesRegex(
                ValueError,
                "preserve its network authority",
            ):
                controller.resume(
                    thread_id="checkpoint-resume",
                    allow_network=False,
                    mode="checkpoint",
                )

        changed_runtime = replace(
            self.settings,
            model="openai:changed-local-model",
            model_base_url="http://127.0.0.1:8000/v1",
        )
        with AutonomousResearchController(
            changed_runtime,
            research_id="long-context-sparse-models",
            action_executor=executor,
            inspector=inspector,
        ) as controller:
            with self.assertRaisesRegex(
                ValueError,
                "preserve its model and endpoint",
            ):
                controller.resume(
                    thread_id="checkpoint-resume",
                    allow_network=True,
                    mode="checkpoint",
                )

        with AutonomousResearchController(
            self.settings,
            research_id="long-context-sparse-models",
            action_executor=executor,
            inspector=inspector,
        ) as controller:
            resumed = controller.resume(
                thread_id="checkpoint-resume",
                allow_network=True,
                mode="checkpoint",
            )

        self.assertEqual(1, inspection_count)
        self.assertEqual(2, executor.calls)
        self.assertEqual("blocked", resumed["stop_reason"])
        self.assertEqual("fixture-stop", resumed["action_result"]["error_codes"][0])

    def test_action_contract_requires_targets_and_consistent_blocking(self) -> None:
        with self.assertRaisesRegex(ValueError, "must target"):
            ResearchDecision(
                action="search",
                reason="missing target",
                expected_information_gain=0.5,
            )
        with self.assertRaisesRegex(ValueError, "non-attempted"):
            ResearchActionResult(
                action_id="action-0001",
                action="search",
                target_gap_id="gap-1",
                status="blocked",
                outcome="precondition_blocked",
                attempted=False,
                tool_calls=1,
            )


class LangGraphLoopTests(HarnessTestCase):
    def test_tool_loop_is_checkpointed_and_resumes_same_thread(self) -> None:
        model = ScriptedModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "wiki_search",
                            "args": {"query": "LongLoRA"},
                            "id": "call-search-1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Located paper:longlora."),
            ]
        )
        with ResearchHarness(self.settings, model=model) as harness:
            first = harness.invoke("Find LongLoRA.", thread_id="thread-a")
            self.assertEqual(1, first["iteration"])
            self.assertEqual(0, first["tool_failures"])
            self.assertIn("wiki_search", model.bound_tool_names)
            self.assertTrue(
                any(isinstance(message, ToolMessage) for message in first["messages"])
            )
            snapshot = harness.get_state("thread-a")
            self.assertEqual(
                "Located paper:longlora.", snapshot.values["messages"][-1].content
            )

        resumed_model = ScriptedModel(
            [AIMessage(content="The same thread was resumed.")]
        )
        with ResearchHarness(self.settings, model=resumed_model) as harness:
            second = harness.invoke("Continue.", thread_id="thread-a")
            self.assertEqual(
                "The same thread was resumed.", second["messages"][-1].content
            )
        visible = resumed_model.seen_messages[0]
        visible_text = "\n".join(str(message.content) for message in visible)
        self.assertIn("Find LongLoRA.", visible_text)
        self.assertIn("Continue.", visible_text)

    def test_different_threads_do_not_share_short_term_messages(self) -> None:
        first_model = ScriptedModel([AIMessage(content="thread one")])
        with ResearchHarness(self.settings, model=first_model) as harness:
            harness.invoke("Secret thread-one context.", thread_id="thread-one")
        second_model = ScriptedModel([AIMessage(content="thread two")])
        with ResearchHarness(self.settings, model=second_model) as harness:
            harness.invoke("Fresh context.", thread_id="thread-two")
        visible_text = "\n".join(
            str(message.content) for message in second_model.seen_messages[0]
        )
        self.assertNotIn("Secret thread-one context", visible_text)

    def test_cross_thread_memory_is_explicit_and_shared(self) -> None:
        writer_model = ScriptedModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "remember_research_memory",
                            "args": {
                                "text": "Always preserve context length with a result.",
                                "topic": "evidence-policy",
                                "kind": "decision",
                                "evidence_ids": ["benchmark:ruler"],
                            },
                            "id": "call-memory-write",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Memory stored."),
            ]
        )
        with ResearchHarness(self.settings, model=writer_model) as harness:
            harness.invoke("Remember the evidence rule.", thread_id="memory-writer")

        reader_model = ScriptedModel([AIMessage(content="Memory recalled in prompt.")])
        with ResearchHarness(self.settings, model=reader_model) as harness:
            result = harness.invoke(
                "What evidence policy applies to context length?",
                thread_id="memory-reader",
            )
            self.assertEqual(1, result["recalled_memory_count"])
        system_messages = [
            message
            for message in reader_model.seen_messages[0]
            if isinstance(message, SystemMessage)
        ]
        self.assertIn("Always preserve context length", system_messages[0].content)

    def test_network_tool_is_blocked_without_runtime_authority(self) -> None:
        model = ScriptedModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "deepxiv_search_run",
                            "args": {
                                "run_path": (
                                    "research/long-context-sparse-models/search-runs/"
                                    "v0-discovery.yaml"
                                ),
                                "dry_run": False,
                            },
                            "id": "call-network-denied",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Network was denied."),
            ]
        )
        with ResearchHarness(self.settings, model=model) as harness:
            result = harness.invoke(
                "Run the provider.",
                thread_id="network-denied",
                allow_network=False,
            )
        tool_messages = [
            message
            for message in result["messages"]
            if isinstance(message, ToolMessage)
        ]
        payload = json.loads(tool_messages[-1].content)
        self.assertFalse(payload["ok"])
        self.assertIn("disabled", payload["error"])
        self.assertEqual(1, result["tool_failures"])

    def test_tool_iteration_limit_forces_a_final_model_answer(self) -> None:
        limited_settings = HarnessSettings(
            database_path=self.database_path,
            context_token_budget=1024,
            max_tool_iterations=1,
            tool_output_chars=8000,
        )
        model = ScriptedModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "wiki_stats",
                            "args": {},
                            "id": "call-limit-stats",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Final synthesis after the limit."),
            ]
        )
        with ResearchHarness(limited_settings, model=model) as harness:
            result = harness.invoke("Inspect the corpus.", thread_id="limit-thread")
        self.assertEqual("max-tool-iterations", result["stop_reason"])
        self.assertEqual(
            "Final synthesis after the limit.", result["messages"][-1].content
        )

    def test_model_context_is_trimmed_but_checkpoint_keeps_thread_history(self) -> None:
        first_model = ScriptedModel([AIMessage(content="first answer " + "x " * 1500)])
        with ResearchHarness(self.settings, model=first_model) as harness:
            harness.invoke("first question " + "a " * 1500, thread_id="trim-thread")
        second_model = ScriptedModel([AIMessage(content="trimmed")])
        with ResearchHarness(self.settings, model=second_model) as harness:
            state = harness.invoke("latest question", thread_id="trim-thread")
            snapshot = harness.get_state("trim-thread")
        self.assertLess(
            state["context_message_count"], len(snapshot.values["messages"])
        )
        self.assertTrue(
            any(
                isinstance(message, HumanMessage)
                and "latest question" in str(message.content)
                for message in second_model.seen_messages[0]
            )
        )


class HarnessCliTests(HarnessTestCase):
    def test_research_cli_inspect_and_step_need_no_model(self) -> None:
        inspect_output = io.StringIO()
        with redirect_stdout(inspect_output):
            inspect_exit = cli_main(
                [
                    "--db",
                    str(self.database_path),
                    "research",
                    "inspect",
                    "long-context-sparse-models",
                    "--format",
                    "json",
                ]
            )
        inspected = json.loads(inspect_output.getvalue())
        self.assertEqual(0, inspect_exit)
        self.assertEqual(8, inspected["corpus"]["pending_queries"])

        step_output = io.StringIO()
        with redirect_stdout(step_output):
            step_exit = cli_main(
                [
                    "--db",
                    str(self.database_path),
                    "research",
                    "step",
                    "long-context-sparse-models",
                    "--thread",
                    "research-cli-test",
                    "--format",
                    "json",
                ]
            )
        stepped = json.loads(step_output.getvalue())
        self.assertEqual(0, step_exit)
        self.assertEqual(1, stepped["control_passes"])
        self.assertEqual(0, stepped["research_iterations"])
        self.assertEqual("search", stepped["decision"]["action"])

        run_output = io.StringIO()
        with redirect_stdout(run_output):
            run_exit = cli_main(
                [
                    "--db",
                    str(self.database_path),
                    "research",
                    "run",
                    "long-context-sparse-models",
                    "--thread",
                    "research-cli-run-test",
                    "--format",
                    "json",
                ]
            )
        ran = json.loads(run_output.getvalue())
        self.assertEqual(0, run_exit)
        self.assertEqual("blocked", ran["stop_reason"])
        self.assertEqual("network-disabled", ran["action_result"]["error_codes"][0])

        resume_output = io.StringIO()
        with redirect_stdout(resume_output):
            resume_exit = cli_main(
                [
                    "--db",
                    str(self.database_path),
                    "research",
                    "resume",
                    "long-context-sparse-models",
                    "--thread",
                    "research-cli-run-test",
                    "--format",
                    "json",
                ]
            )
        resumed = json.loads(resume_output.getvalue())
        self.assertEqual(0, resume_exit)
        self.assertEqual(2, resumed["control_passes"])
        self.assertEqual("network-disabled", resumed["action_result"]["error_codes"][0])

    def test_research_cli_interrupt_prints_resume_guidance(self) -> None:
        initial_output = io.StringIO()
        with redirect_stdout(initial_output):
            self.assertEqual(
                0,
                cli_main(
                    [
                        "--db",
                        str(self.database_path),
                        "research",
                        "run",
                        "long-context-sparse-models",
                        "--thread",
                        "outer-v2",
                        "--format",
                        "json",
                    ]
                ),
            )

        error_output = io.StringIO()
        with mock.patch.object(
            AutonomousResearchController,
            "invoke",
            side_effect=KeyboardInterrupt,
        ), redirect_stderr(error_output):
            exit_code = cli_main(
                [
                    "--db",
                    str(self.database_path),
                    "research",
                    "run",
                    "long-context-sparse-models",
                    "--thread",
                    "outer-v2",
                ]
            )

        self.assertEqual(130, exit_code)
        self.assertEqual(
            "Interrupted.\n"
            "Thread: outer-v2\n"
            "Checkpoint preserved.\n"
            "Resume with the same --thread.\n",
            error_output.getvalue(),
        )

    def test_skills_commands_need_no_model_provider(self) -> None:
        list_output = io.StringIO()
        with redirect_stdout(list_output):
            list_exit = cli_main(
                [
                    "--db",
                    str(self.database_path),
                    "skills",
                    "list",
                    "--format",
                    "json",
                ]
            )
        listed = json.loads(list_output.getvalue())
        self.assertEqual(0, list_exit)
        self.assertEqual(10, listed["count"])

        show_output = io.StringIO()
        with redirect_stdout(show_output):
            show_exit = cli_main(
                [
                    "--db",
                    str(self.database_path),
                    "skills",
                    "show",
                    "ingest-paper",
                    "--format",
                    "json",
                ]
            )
        shown = json.loads(show_output.getvalue())
        self.assertEqual(0, show_exit)
        self.assertTrue(shown["instructions"].startswith("# Ingest Paper"))

        read_output = io.StringIO()
        with redirect_stdout(read_output):
            read_exit = cli_main(
                [
                    "--db",
                    str(self.database_path),
                    "skills",
                    "read",
                    "ingest-paper",
                    "references/evidence-policy.md",
                ]
            )
        self.assertEqual(0, read_exit)
        self.assertIn("evidence", read_output.getvalue().casefold())

    def test_state_and_memory_commands_need_no_model_provider(self) -> None:
        model = ScriptedModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "remember_research_memory",
                            "args": {
                                "text": "CLI persistence smoke note.",
                                "topic": "test",
                                "kind": "observation",
                            },
                            "id": "call-cli-memory",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="stored"),
            ]
        )
        with ResearchHarness(self.settings, model=model) as harness:
            harness.invoke("Store a note.", thread_id="cli-thread")

        state_output = io.StringIO()
        with redirect_stdout(state_output):
            state_exit = cli_main(
                [
                    "--db",
                    str(self.database_path),
                    "state",
                    "--thread",
                    "cli-thread",
                    "--format",
                    "json",
                ]
            )
        state_payload = json.loads(state_output.getvalue())
        self.assertEqual(0, state_exit)
        self.assertEqual("cli-thread", state_payload["thread_id"])

        memory_output = io.StringIO()
        with redirect_stdout(memory_output):
            memory_exit = cli_main(
                [
                    "--db",
                    str(self.database_path),
                    "--workspace",
                    "long-context-sparse-models",
                    "memories",
                    "--format",
                    "json",
                ]
            )
        memory_payload = json.loads(memory_output.getvalue())
        self.assertEqual(0, memory_exit)
        self.assertEqual(1, memory_payload["count"])


if __name__ == "__main__":
    unittest.main()
