"""Offline tests for deterministic search-paper scripts."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from search_common import (  # noqa: E402
    canonical_source_id,
    link_possible_versions,
    merge_candidates,
    normalize_doi,
    normalize_provider_item,
    query_signature,
    recompute_metrics,
    sanitize_message,
)
from validate_search_run import validate_run  # noqa: E402


class SearchCommonTests(unittest.TestCase):
    def test_arxiv_identifier_normalization_preserves_exact_version(self) -> None:
        canonical, exact = canonical_source_id(
            "arxiv",
            "https://arxiv.org/pdf/2309.12307v2.pdf",
        )
        self.assertEqual(canonical, "2309.12307")
        self.assertEqual(exact, "https://arxiv.org/pdf/2309.12307v2.pdf")

    def test_exact_id_dedup_merges_query_provenance(self) -> None:
        first = normalize_provider_item(
            {
                "arxiv_id": "2309.12307v1",
                "title": "A Sparse Long Context Model",
                "authors": ["A. Author"],
                "date": "2023-09-20",
                "score": 0.8,
            },
            "arxiv",
            "Q01",
            1,
            "2026-08-26T00:00:00Z",
        )
        second = normalize_provider_item(
            {
                "arxiv_id": "2309.12307v2",
                "title": "A Sparse Long Context Model",
                "authors": ["A. Author", "B. Author"],
                "date": "2023-09-20",
                "score": 0.9,
            },
            "arxiv",
            "Q02",
            2,
            "2026-08-26T00:01:00Z",
        )
        merged = merge_candidates([first], [second])
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0]["discovered_by"]), 2)
        self.assertEqual(
            merged[0]["alternate_identifiers"]["returned_source_ids"],
            ["2309.12307v1", "2309.12307v2"],
        )
        self.assertEqual(merged[0]["authors"], ["A. Author", "B. Author"])

    def test_exact_doi_dedup_merges_cross_source_candidates(self) -> None:
        arxiv = normalize_provider_item(
            {
                "arxiv_id": "2401.00001",
                "title": "A Cross-Published Sparse Model",
                "date": "2024-01-01",
                "doi": "https://doi.org/10.1000/Example.DOI",
            },
            "arxiv",
            "Q01",
            1,
            "2026-08-26T00:00:00Z",
        )
        biorxiv = normalize_provider_item(
            {
                "biorxiv_id": "10.1101/2024.01.01.123456v2",
                "title": "A Cross-Published Sparse Model",
                "date": "2024-01-01",
                "doi": "doi:10.1000/example.doi",
            },
            "biorxiv",
            "Q02",
            1,
            "2026-08-26T00:01:00Z",
        )
        merged = merge_candidates([arxiv], [biorxiv])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["doi"], "10.1000/example.doi")
        self.assertIn(
            "biorxiv:10.1101/2024.01.01.123456",
            merged[0]["alternate_identifiers"]["candidate_ids"],
        )
        self.assertEqual(len(merged[0]["discovered_by"]), 2)

    def test_doi_normalization(self) -> None:
        self.assertEqual(
            normalize_doi("https://doi.org/10.1000/Example.DOI"),
            "10.1000/example.doi",
        )

    def test_exact_title_year_only_links_possible_versions(self) -> None:
        first = normalize_provider_item(
            {
                "arxiv_id": "2401.00001",
                "title": "Dynamic Sparse Attention for Very Long Documents",
                "date": "2024-01-01",
            },
            "arxiv",
            "Q01",
            1,
            "2026-08-26T00:00:00Z",
        )
        second = normalize_provider_item(
            {
                "arxiv_id": "2402.00002",
                "title": "Dynamic Sparse Attention for Very Long Documents",
                "date": "2024-02-01",
            },
            "arxiv",
            "Q02",
            1,
            "2026-08-26T00:00:00Z",
        )
        candidates = [first, second]
        link_possible_versions(candidates)
        self.assertEqual(first["possible_version_of"], [second["candidate_id"]])
        self.assertEqual(second["possible_version_of"], [first["candidate_id"]])

    def test_metrics_count_duplicates_and_untriaged(self) -> None:
        run = {
            "queries": [
                {
                    "id": "Q01",
                    "round": 1,
                    "execution": {"status": "succeeded", "retrieved_count": 2},
                },
                {
                    "id": "Q02",
                    "round": 2,
                    "execution": {"status": "succeeded", "retrieved_count": 1},
                },
            ],
            "candidates": [
                {
                    "relevance": {"label": "core"},
                    "title": "One",
                    "authors": ["A"],
                    "year": 2024,
                    "discovered_by": [{"query_id": "Q01"}],
                },
                {
                    "relevance": {"label": None},
                    "title": "Two",
                    "authors": [],
                    "year": 2025,
                    "discovered_by": [{"query_id": "Q02"}],
                },
            ],
            "coverage": {},
        }
        metrics = recompute_metrics(run)
        self.assertEqual(metrics["executed_queries"], 2)
        self.assertEqual(metrics["raw_retrieved_hits"], 3)
        self.assertEqual(metrics["unique_candidates"], 2)
        self.assertAlmostEqual(metrics["duplicate_rate"], 1 / 3, places=6)
        self.assertEqual(metrics["relevance_counts"]["core"], 1)
        self.assertEqual(metrics["relevance_counts"]["untriaged"], 1)
        self.assertEqual(metrics["missing_metadata_count"], 1)

    def test_sanitizer_redacts_explicit_secret(self) -> None:
        secret = "unit-test-secret-value"
        cleaned = sanitize_message(
            f"request failed at ?token={secret} Authorization: Bearer {secret}",
            secrets=[secret],
        )
        self.assertNotIn(secret, cleaned)
        self.assertIn("<redacted>", cleaned)

    def test_query_signature_ignores_execution_state(self) -> None:
        first = {
            "text": " sparse attention ",
            "filters": {"source": "arxiv", "size": 20},
            "execution": {"status": "planned"},
        }
        second = copy.deepcopy(first)
        second["text"] = "SPARSE   ATTENTION"
        second["execution"]["status"] = "succeeded"
        self.assertEqual(query_signature(first), query_signature(second))


class ValidatorTests(unittest.TestCase):
    def _valid_run(self):
        candidate = normalize_provider_item(
            {
                "arxiv_id": "2401.00001",
                "title": "Sparse Attention for Long Context",
                "authors": ["A. Author"],
                "date": "2024-01-01",
            },
            "arxiv",
            "Q01",
            1,
            "2026-08-26T00:00:00Z",
        )
        candidate["relevance"] = {
            "label": "core",
            "scores": {
                "sparsity_alignment": 2,
                "long_context_alignment": 2,
                "evidence_value": 1,
                "engineering_value": 1,
                "challenge_value": 0,
            },
            "reason": "Directly studies sparse attention in a long-context setting.",
            "basis": "title-and-abstract",
        }
        candidate["review_state"] = "abstract-screened"

        run = {
            "schema_version": "0.1",
            "run": {
                "id": "test-run",
                "topic_slug": "test-topic",
                "question": "What is the trade-off?",
                "created_at": "2026-08-26T00:00:00Z",
                "updated_at": "2026-08-26T00:00:00Z",
                "status": "needs-review",
                "round": 1,
                "provider": {
                    "name": "deepxiv",
                    "interface": "deepxiv-sdk",
                    "package_version": "1.0.0",
                    "source": "arxiv",
                },
                "budget": {"max_queries": 8, "max_candidates": None, "max_rounds": 3},
                "stop_reason": None,
            },
            "scope": {
                "included_concepts": ["sparse attention"],
                "excluded_concepts": ["weight pruning"],
                "required_facets": ["quality"],
                "years": {"from": None, "to": None},
                "venues": [],
                "categories": [],
                "sources": ["arxiv"],
                "assumptions": ["metadata screening only"],
                "unresolved_questions": [],
            },
            "seeds": [],
            "queries": [
                {
                    "id": "Q01",
                    "round": 1,
                    "family": "direct-topic",
                    "text": "sparse attention long context",
                    "purpose": "Find direct work.",
                    "target_facets": ["quality"],
                    "derived_from": None,
                    "filters": {
                        "source": "arxiv",
                        "size": 20,
                        "offset": 0,
                        "use_fine_rerank": False,
                    },
                    "execution": {
                        "status": "succeeded",
                        "executed_at": "2026-08-26T00:00:00Z",
                        "provider_total_count": 1,
                        "retrieved_count": 1,
                        "retained_count": 1,
                        "raw_result_path": "raw/test-run/Q01.json",
                        "error_id": None,
                    },
                }
            ],
            "candidates": [candidate],
            "coverage": {
                "facets": [
                    {
                        "name": "quality",
                        "status": "partial",
                        "candidate_ids": [candidate["candidate_id"]],
                        "note": "One candidate found.",
                        "next_query": None,
                    }
                ],
                "metrics": {},
                "gaps": [],
            },
            "citation_expansion": {
                "performed": False,
                "provider": None,
                "records": [],
                "gap_reason": None,
            },
            "errors": [],
            "limitations": ["Metadata screening only."],
            "loop_review": {
                "repeated_failure_keys": [],
                "error_book_candidates": [],
                "proposed_rules": [],
                "proposed_scripts": [],
                "skill_change_requested": False,
            },
        }
        recompute_metrics(run)
        return run

    def test_valid_run_has_no_errors(self) -> None:
        issues, _ = validate_run(self._valid_run())
        self.assertFalse([issue for issue in issues if issue.severity == "error"])

    def test_secret_field_is_rejected_without_echoing_value(self) -> None:
        run = self._valid_run()
        run["run"]["provider"]["token"] = "example-secret"
        issues, _ = validate_run(run)
        self.assertTrue(
            any(issue.path.endswith(".token") and issue.severity == "error" for issue in issues)
        )
        self.assertFalse(any("example-secret" in issue.message for issue in issues))


if __name__ == "__main__":
    unittest.main()
