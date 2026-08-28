"""Offline tests for Canary isolation, checkpoints, and semantic artifacts."""

from __future__ import annotations

import hashlib
import json
import shutil
import unittest
import uuid
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from research_harness.artifacts import (
    SemanticArtifactContext,
    SemanticArtifactRecorder,
)
from research_harness.canary import (
    _build_canary_graph,
    prepare_canary_workspace,
)
from research_harness.canary_models import CanaryLimits
from research_harness.config import HarnessSettings
from research_harness.persistence import HarnessPersistence
from research_harness.research_models import ResearchActionResult
from research_harness.skill_registry import SkillRegistry
from research_harness.trajectory import (
    annotation_freshness,
    ensure_annotation_sidecar,
    export_checkpoint_trajectory,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeExecutor:
    supported_actions = frozenset({"search"})

    def __init__(self, *, fail: bool = False):
        self.fail = fail

    def execute(self, **kwargs):
        decision = kwargs["decision"]
        return ResearchActionResult(
            action_id=kwargs["action_id"],
            action="search",
            target_gap_id=decision.target_gap_id,
            status="partial" if self.fail else "success",
            outcome="tool_failure" if self.fail else "positive",
            attempted=True,
            tool_calls=1,
            metrics={"queries_attempted": 1, "new_candidates": 1},
        )


class CanaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = HarnessSettings.from_env(
            database_path=REPOSITORY_ROOT / ".harness" / "unit-base.sqlite3",
            model=None,
        )
        self.run_id = f"unit-{uuid.uuid4().hex[:12]}"
        self.canary_root = REPOSITORY_ROOT / ".harness" / "canary" / self.run_id

    def tearDown(self) -> None:
        if self.canary_root.is_dir():
            shutil.rmtree(self.canary_root)

    def test_workspace_copy_does_not_mutate_formal_search_run(self) -> None:
        with self.assertRaisesRegex(ValueError, "research_id"):
            prepare_canary_workspace(
                self.settings,
                research_id="../escape",
                run_id=f"{self.run_id}-escape",
                limits=CanaryLimits(stop_after="retrieval", max_actions=1),
            )
        source = (
            REPOSITORY_ROOT
            / "research"
            / "long-context-sparse-models"
            / "search-runs"
            / "v0-discovery.yaml"
        )
        before = _sha256(source)
        isolated, root, run_path = prepare_canary_workspace(
            self.settings,
            research_id="long-context-sparse-models",
            run_id=self.run_id,
            limits=CanaryLimits(stop_after="retrieval", max_actions=1),
            source_run=source,
        )
        self.assertEqual(root, self.canary_root)
        self.assertTrue(run_path.is_file())
        self.assertEqual(isolated.database_path, root / "canary.sqlite3")
        payload = run_path.read_text(encoding="utf-8")
        self.assertEqual(payload.count("status: planned"), 2)
        run_path.write_text(payload + "\n# isolated mutation\n", encoding="utf-8")
        self.assertEqual(before, _sha256(source))

    def test_fake_executor_produces_sqlite_checkpoint_trajectory(self) -> None:
        isolated, root, run_path = prepare_canary_workspace(
            self.settings,
            research_id="long-context-sparse-models",
            run_id=self.run_id,
            limits=CanaryLimits(stop_after="retrieval", max_actions=1),
        )
        with HarnessPersistence(isolated) as persistence:
            graph = _build_canary_graph(
                settings=isolated,
                research_id="long-context-sparse-models",
                run_path=run_path,
                limits=CanaryLimits(stop_after="retrieval", max_actions=1),
                executor=FakeExecutor(),
                persistence=persistence,
            )
            config = {"configurable": {"thread_id": f"canary:{self.run_id}"}}
            state = graph.invoke({"research_id": "long-context-sparse-models"}, config)
            trajectory = root / "trajectory.jsonl"
            count = export_checkpoint_trajectory(
                graph.get_state_history(config),
                destination=trajectory,
                research_id="long-context-sparse-models",
                thread_id=f"canary:{self.run_id}",
            )
        self.assertEqual(state["stage_reached"], "retrieval")
        records = [
            json.loads(line)
            for line in trajectory.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(count, len(records))
        self.assertEqual(records[0]["research_id"], "long-context-sparse-models")
        self.assertGreaterEqual(len(records), 3)
        self.assertTrue((root / "canary.sqlite3").is_file())
        sidecar = root / "human-annotations.yaml"
        ensure_annotation_sidecar(
            sidecar,
            research_id="long-context-sparse-models",
            thread_id=f"canary:{self.run_id}",
        )
        annotations = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
        self.assertIn("promotion_correct", annotations["field_contract"]["verification"])
        source_hash = "a" * 64
        self.assertEqual(
            annotation_freshness(
                {"source_sha256": source_hash},
                current_source_sha256=source_hash,
            ),
            "current",
        )
        self.assertEqual(
            annotation_freshness(
                {"source_sha256": source_hash},
                current_source_sha256="b" * 64,
            ),
            "stale",
        )

    def test_partial_tool_failure_does_not_complete_observation_boundary(self) -> None:
        isolated, _, run_path = prepare_canary_workspace(
            self.settings,
            research_id="long-context-sparse-models",
            run_id=self.run_id,
            limits=CanaryLimits(stop_after="retrieval", max_actions=1),
        )
        with HarnessPersistence(isolated) as persistence:
            graph = _build_canary_graph(
                settings=isolated,
                research_id="long-context-sparse-models",
                run_path=run_path,
                limits=CanaryLimits(stop_after="retrieval", max_actions=1),
                executor=FakeExecutor(fail=True),
                persistence=persistence,
            )
            state = graph.invoke(
                {"research_id": "long-context-sparse-models"},
                {"configurable": {"thread_id": f"canary:{self.run_id}"}},
            )
        self.assertEqual(state["stage_reached"], "not-started")
        self.assertEqual(state["action_results"][-1]["outcome"], "tool_failure")

    def test_semantic_artifact_is_immutable_and_manifest_is_separate(self) -> None:
        root = REPOSITORY_ROOT / ".harness" / "canary" / self.run_id
        root.mkdir(parents=True)
        recorder = SemanticArtifactRecorder(
            REPOSITORY_ROOT,
            root / "artifacts",
            model_name="fake:model",
        )
        skill = SkillRegistry(REPOSITORY_ROOT / "skills").get("search-paper")
        context = SemanticArtifactContext(
            research_id="long-context-sparse-models",
            action_id="action-1",
            snapshot_id="snapshot-1",
            wiki_source_hash="wiki-hash",
            source_ids=("Q01",),
        )
        first = recorder.record(
            kind="search-plan",
            context=context,
            skill=skill,
            schema_resources=("references/search-output-schema.md",),
            output={
                "queries": [{"text": "bounded fake query"}],
                "api_key": "must-not-persist",
            },
        )
        artifact_path = REPOSITORY_ROOT / first.relative_path
        before = _sha256(artifact_path)
        second = recorder.record(
            kind="search-plan",
            context=context,
            skill=skill,
            schema_resources=("references/search-output-schema.md",),
            output={
                "queries": [{"text": "bounded fake query"}],
                "api_key": "must-not-persist",
            },
        )
        recorder.link_publication(
            (first.artifact_id,),
            action_id="action-1",
            changed_sources=("research/fake.yaml",),
        )
        self.assertEqual(first.artifact_id, second.artifact_id)
        self.assertEqual(before, _sha256(artifact_path))
        artifact_text = artifact_path.read_text(encoding="utf-8")
        self.assertNotIn("must-not-persist", artifact_text)
        self.assertEqual(json.loads(artifact_text)["output"]["api_key"], "[REDACTED]")
        manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["artifacts"][first.artifact_id]["publications"][0][
                "changed_sources"
            ],
            ["research/fake.yaml"],
        )


if __name__ == "__main__":
    unittest.main()
