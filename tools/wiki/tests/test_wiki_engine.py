from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tools.wiki.writer as wiki_writer
from tools.wiki.indexer import build_index, write_artifacts
from tools.wiki.parser import parse_page
from tools.wiki.query import (
    backlinks_for,
    related_entities,
    search_entities,
    structured_query,
)
from tools.wiki.validator import validate_index
from tools.wiki.writer import WikiSourceWriter


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_WIKI = Path(__file__).resolve().parent / "fixtures" / "wiki"
META_ROOT = REPOSITORY_ROOT / "wiki" / "_meta"


def diagnostic_codes(index, severity: str | None = None) -> list[str]:
    return [
        item.code
        for item in validate_index(index)
        if severity is None or item.severity == severity
    ]


class WikiEngineFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = build_index(FIXTURE_WIKI, META_ROOT)

    def test_all_eight_entity_types_form_a_clean_fixture(self) -> None:
        diagnostics = validate_index(self.index)
        self.assertEqual([], diagnostics)
        self.assertEqual(
            {
                "paper",
                "method",
                "experiment",
                "claim",
                "concept",
                "benchmark",
                "model",
                "assessment",
            },
            {entity.entity_type for entity in self.index.unique_entities().values()},
        )

    def test_parser_ignores_wikilinks_inside_fenced_code(self) -> None:
        paper = parse_page(FIXTURE_WIKI / "papers" / "alpha.md", FIXTURE_WIKI)
        self.assertEqual(
            ["method:sparse-window", "experiment:alpha-ruler-32k"],
            [link.target for link in paper.links],
        )

    def test_typed_graph_contains_each_relation_family(self) -> None:
        edge_keys = {edge.key for edge in self.index.edges}
        self.assertEqual(
            {
                ("paper:alpha", "proposes", "method:sparse-window"),
                ("paper:alpha", "reports", "experiment:alpha-ruler-32k"),
                (
                    "experiment:alpha-ruler-32k",
                    "uses_method",
                    "method:sparse-window",
                ),
                (
                    "experiment:alpha-ruler-32k",
                    "uses_model",
                    "model:llama-7b",
                ),
                (
                    "experiment:alpha-ruler-32k",
                    "evaluates_on",
                    "benchmark:ruler",
                ),
                (
                    "experiment:alpha-ruler-32k",
                    "supports",
                    "claim:quality-preserved",
                ),
                (
                    "method:sparse-window",
                    "instance_of",
                    "concept:attention-sparsity",
                ),
                (
                    "assessment:context-damage",
                    "assesses_claim",
                    "claim:quality-preserved",
                ),
                (
                    "assessment:context-damage",
                    "uses_evidence",
                    "experiment:alpha-ruler-32k",
                ),
                (
                    "assessment:context-damage",
                    "assesses_on",
                    "benchmark:ruler",
                ),
            },
            edge_keys,
        )

    def test_backlinks_include_inverse_relation_metadata(self) -> None:
        backlinks = backlinks_for(self.index, "claim:quality-preserved")
        support = next(
            item for item in backlinks["structured"] if item["relation"] == "supports"
        )
        self.assertEqual(2, len(backlinks["structured"]))
        self.assertEqual("experiment:alpha-ruler-32k", support["source"])
        self.assertEqual("supported_by", support["inverse"])

    def test_nonconsensus_assessment_preserves_insufficient_evidence(self) -> None:
        entity = self.index.resolver.exact_entity("assessment:context-damage")
        self.assertIsNotNone(entity)
        assert entity is not None
        self.assertEqual("insufficient-evidence", entity.metadata["result"])
        self.assertTrue(entity.metadata["verified"])

    def test_related_traversal_crosses_paper_method_and_concept(self) -> None:
        records = related_entities(self.index, "paper:alpha", depth=2)
        by_id = {record["id"]: record for record in records}
        self.assertEqual(1, by_id["method:sparse-window"]["distance"])
        self.assertEqual(1, by_id["experiment:alpha-ruler-32k"]["distance"])
        self.assertEqual(2, by_id["concept:attention-sparsity"]["distance"])
        self.assertEqual(2, by_id["benchmark:ruler"]["distance"])

    def test_structured_experiment_query_preserves_conditions(self) -> None:
        records = structured_query(
            self.index,
            entity_type="experiment",
            benchmark="ruler",
            method="Sparse Window Attention",
            model="llama-7b",
            min_context=32000,
            max_context=33000,
            sparsity_target="attention",
            min_sparsity=0.7,
        )
        self.assertEqual(
            ["experiment:alpha-ruler-32k"], [item["id"] for item in records]
        )

    def test_search_matches_alias_and_body(self) -> None:
        alias_matches = search_entities(self.index, "Window Sparse Attention")
        body_matches = search_entities(self.index, "Table 2")
        self.assertEqual("method:sparse-window", alias_matches[0]["id"])
        self.assertEqual("experiment:alpha-ruler-32k", body_matches[0]["id"])

    def test_index_payload_is_deterministic_and_rebuildable(self) -> None:
        first = build_index(FIXTURE_WIKI, META_ROOT)
        second = build_index(FIXTURE_WIKI, META_ROOT)
        self.assertEqual(first.source_hash, second.source_hash)
        self.assertEqual(first.artifacts([]), second.artifacts([]))

    def test_generated_artifacts_do_not_change_source_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            wiki_root = Path(temporary) / "wiki"
            shutil.copytree(FIXTURE_WIKI, wiki_root)
            before = build_index(wiki_root, META_ROOT)
            written = write_artifacts(before, validate_index(before))
            after = build_index(wiki_root, META_ROOT)
            self.assertEqual(before.source_hash, after.source_hash)
            self.assertEqual(
                {
                    "aliases.json",
                    "backlinks.json",
                    "diagnostics.json",
                    "edges.json",
                    "entities.json",
                    "stats.json",
                },
                set(written),
            )
            payload = json.loads(written["stats.json"].read_text(encoding="utf-8"))
            self.assertEqual(8, payload["unique_entities"])

    def test_artifacts_cannot_be_written_outside_wiki_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "inside the Wiki root"):
                write_artifacts(
                    self.index,
                    [],
                    Path(temporary) / "outside-generated",
                )


class WikiEngineFailureModeTests(unittest.TestCase):
    def temporary_fixture(self):
        temporary = tempfile.TemporaryDirectory()
        wiki_root = Path(temporary.name) / "wiki"
        shutil.copytree(FIXTURE_WIKI, wiki_root)
        return temporary, wiki_root

    def test_unresolved_structured_relation_is_an_error(self) -> None:
        temporary, wiki_root = self.temporary_fixture()
        with temporary:
            page = wiki_root / "papers" / "alpha.md"
            text = page.read_text(encoding="utf-8")
            page.write_text(
                text.replace("method:sparse-window", "method:not-present", 1),
                encoding="utf-8",
            )
            index = build_index(wiki_root, META_ROOT)
            self.assertIn(
                "unresolved_relation_target", diagnostic_codes(index, "ERROR")
            )

    def test_duplicate_canonical_id_is_an_error(self) -> None:
        temporary, wiki_root = self.temporary_fixture()
        with temporary:
            shutil.copy2(
                wiki_root / "methods" / "sparse-window.md",
                wiki_root / "methods" / "duplicate.md",
            )
            index = build_index(wiki_root, META_ROOT)
            self.assertIn("duplicate_id", diagnostic_codes(index, "ERROR"))

    def test_ambiguous_alias_is_a_warning(self) -> None:
        temporary, wiki_root = self.temporary_fixture()
        with temporary:
            page = wiki_root / "benchmarks" / "ruler.md"
            text = page.read_text(encoding="utf-8")
            page.write_text(
                text.replace("aliases: []", "aliases:\n  - Alpha"), encoding="utf-8"
            )
            index = build_index(wiki_root, META_ROOT)
            self.assertIn("ambiguous_alias", diagnostic_codes(index, "WARNING"))

    def test_parent_traversal_wikilink_is_an_error(self) -> None:
        temporary, wiki_root = self.temporary_fixture()
        with temporary:
            page = wiki_root / "papers" / "alpha.md"
            page.write_text(
                page.read_text(encoding="utf-8") + "\nUnsafe [[../outside]].\n",
                encoding="utf-8",
            )
            index = build_index(wiki_root, META_ROOT)
            self.assertIn("illegal_wikilink_path", diagnostic_codes(index, "ERROR"))

    def test_verified_claim_requires_an_experiment_evidence_edge(self) -> None:
        temporary, wiki_root = self.temporary_fixture()
        with temporary:
            page = wiki_root / "experiments" / "alpha-ruler-32k.md"
            text = page.read_text(encoding="utf-8")
            page.write_text(
                text.replace(
                    "relations:\n  supports:\n    - claim:quality-preserved",
                    "relations: {}",
                ),
                encoding="utf-8",
            )
            index = build_index(wiki_root, META_ROOT)
            errors = diagnostic_codes(index, "ERROR")
            warnings = diagnostic_codes(index, "WARNING")
            self.assertIn("verified_claim_insufficient_evidence", errors)
            self.assertIn("claim_lacks_evidence", warnings)

    def test_verified_author_stated_claim_accepts_located_direct_evidence(
        self,
    ) -> None:
        temporary, wiki_root = self.temporary_fixture()
        with temporary:
            experiment = wiki_root / "experiments" / "alpha-ruler-32k.md"
            experiment.write_text(
                experiment.read_text(encoding="utf-8").replace(
                    "relations:\n  supports:\n    - claim:quality-preserved",
                    "relations: {}",
                ),
                encoding="utf-8",
            )
            claim = wiki_root / "claims" / "quality-preserved.md"
            text = claim.read_text(encoding="utf-8")
            claim.write_text(
                text.replace(
                    "assessment: supported\n",
                    "assessment: supported\n"
                    "attribution: author\n"
                    "evidence_type: author-stated\n"
                    "evidence_status: located\n"
                    "evidence:\n"
                    "  locator: Section 4, PDF p. 6\n"
                    "  pdf_page: 6\n"
                    "source_paper: paper:alpha\n",
                ),
                encoding="utf-8",
            )
            index = build_index(wiki_root, META_ROOT)
            errors = diagnostic_codes(index, "ERROR")
            warnings = diagnostic_codes(index, "WARNING")
            self.assertNotIn("verified_claim_insufficient_evidence", errors)
            self.assertNotIn("verified_claim_missing_direct_evidence", errors)
            self.assertNotIn("claim_lacks_evidence", warnings)
            self.assertTrue(
                any(
                    edge.source == "paper:alpha"
                    and edge.target == "claim:quality-preserved"
                    and edge.relation == "states"
                    for edge in index.edges
                )
            )

    def test_verified_experiment_requires_precise_evidence_locator(self) -> None:
        temporary, wiki_root = self.temporary_fixture()
        with temporary:
            page = wiki_root / "experiments" / "alpha-ruler-32k.md"
            text = page.read_text(encoding="utf-8")
            page.write_text(
                text.replace("locator: Table 2, row RULER-32k", 'locator: ""'),
                encoding="utf-8",
            )
            index = build_index(wiki_root, META_ROOT)
            self.assertIn(
                "verified_missing_evidence_field", diagnostic_codes(index, "ERROR")
            )

    def test_assessment_rejects_unknown_result(self) -> None:
        temporary, wiki_root = self.temporary_fixture()
        with temporary:
            page = wiki_root / "assessments" / "context-damage.md"
            text = page.read_text(encoding="utf-8")
            page.write_text(
                text.replace(
                    "result: insufficient-evidence", "result: manufactured-conflict"
                ),
                encoding="utf-8",
            )
            index = build_index(wiki_root, META_ROOT)
            self.assertIn(
                "invalid_nonconsensus_result", diagnostic_codes(index, "ERROR")
            )

    def test_verified_assessment_requires_explicit_verified_flag(self) -> None:
        temporary, wiki_root = self.temporary_fixture()
        with temporary:
            page = wiki_root / "assessments" / "context-damage.md"
            text = page.read_text(encoding="utf-8")
            page.write_text(
                text.replace("verified: true", "verified: false"),
                encoding="utf-8",
            )
            index = build_index(wiki_root, META_ROOT)
            self.assertIn(
                "verified_assessment_flag_required",
                diagnostic_codes(index, "ERROR"),
            )

    def test_publish_rolls_back_all_pages_when_keyboard_interrupts_batch(self) -> None:
        temporary, wiki_root = self.temporary_fixture()
        with temporary:
            existing = wiki_root / "methods" / "sparse-window.md"
            previous_bytes = existing.read_bytes()
            previous_hash = build_index(wiki_root, META_ROOT).source_hash
            new_page = wiki_root / "papers" / "interrupt-probe.md"

            modified_method = previous_bytes.decode("utf-8") + "\nInterrupt probe.\n"
            paper_template = (wiki_root / "papers" / "alpha.md").read_text(
                encoding="utf-8"
            )
            new_paper = (
                paper_template.replace("paper:alpha", "paper:interrupt-probe")
                .replace("Alpha Sparse Attention", "Interrupt Probe")
                .replace("  - Alpha\n", "  - Interrupt Probe Paper\n")
                .replace("2608.00001", "2608.99999")
            )

            real_atomic_write = wiki_writer._atomic_write
            calls = 0

            def interrupt_on_second_write(path: Path, content: str) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise KeyboardInterrupt("simulated publish interruption")
                real_atomic_write(path, content)

            writer = WikiSourceWriter(wiki_root, META_ROOT)
            with patch(
                "tools.wiki.writer._atomic_write",
                side_effect=interrupt_on_second_write,
            ):
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "simulated publish interruption",
                ):
                    writer.publish(
                        {
                            "methods/sparse-window.md": modified_method,
                            "papers/interrupt-probe.md": new_paper,
                        },
                        allow_overwrite=True,
                    )

            self.assertEqual(previous_bytes, existing.read_bytes())
            self.assertFalse(new_page.exists())
            self.assertEqual(
                previous_hash, build_index(wiki_root, META_ROOT).source_hash
            )


class LegacyCompatibilityTests(unittest.TestCase):
    def test_existing_wiki_is_read_without_destructive_migration(self) -> None:
        index = build_index(REPOSITORY_ROOT / "wiki", META_ROOT)
        diagnostics = validate_index(index)
        self.assertFalse([item for item in diagnostics if item.severity == "ERROR"])
        warning_codes = [
            item.code for item in diagnostics if item.severity == "WARNING"
        ]
        self.assertEqual(2, warning_codes.count("legacy_schema_version"))
        self.assertIn("legacy_method_as_concept", warning_codes)
        self.assertIn("legacy_path_link", warning_codes)

    def test_legacy_path_link_resolves_to_canonical_id(self) -> None:
        index = build_index(REPOSITORY_ROOT / "wiki", META_ROOT)
        resolution, target, candidates = index.resolver.resolve_reference(
            "concepts/shifted-sparse-attention"
        )
        self.assertEqual("legacy-path", resolution)
        self.assertEqual("concept:shifted-sparse-attention", target)
        self.assertEqual(("concept:shifted-sparse-attention",), candidates)


if __name__ == "__main__":
    unittest.main()
