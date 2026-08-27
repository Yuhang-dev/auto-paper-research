"""LangGraph orchestration for the LLM-Wiki research harness."""

import os

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from .config import HarnessSettings
from .graph import ResearchHarness
from .ingest_models import IngestCandidate, PaperIngestDraft, PaperIngestResult
from .evidence_verification import EvidenceVerificationPipeline
from .nonconsensus_analysis import NonConsensusAnalysisPipeline
from .paper_ingest import PaperIngestPipeline
from .search_runtime import SearchRuntime
from .research_control import AutonomousResearchController, ResearchController
from .research_models import (
    ActionAttemptStats,
    DoneCriteria,
    NonConsensusAssessment,
    ResearchActionResult,
    ResearchDecision,
    ResearchGap,
    ResearchSnapshot,
)
from .skill_registry import SkillRegistry, SkillRegistryError, SkillResource, SkillSpec

__all__ = [
    "HarnessSettings",
    "ResearchHarness",
    "PaperIngestPipeline",
    "EvidenceVerificationPipeline",
    "NonConsensusAnalysisPipeline",
    "SearchRuntime",
    "PaperIngestDraft",
    "PaperIngestResult",
    "IngestCandidate",
    "ResearchController",
    "AutonomousResearchController",
    "ResearchSnapshot",
    "ResearchGap",
    "ResearchDecision",
    "ResearchActionResult",
    "ActionAttemptStats",
    "DoneCriteria",
    "NonConsensusAssessment",
    "SkillRegistry",
    "SkillRegistryError",
    "SkillResource",
    "SkillSpec",
]
