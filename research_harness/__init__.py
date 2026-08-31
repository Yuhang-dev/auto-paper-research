"""LangGraph orchestration for the LLM-Wiki research harness."""

import os

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from .config import HarnessSettings
from .graph import ResearchHarness
from .ingest_models import IngestCandidate, PaperIngestDraft, PaperIngestResult
from .evidence_verification import EvidenceVerificationPipeline
from .evidence_revision import EvidenceRevisionPipeline
from .nonconsensus_analysis import NonConsensusAnalysisPipeline
from .paper_ingest import PaperIngestPipeline, StagedWikiPublisher
from .staged_ingest import StagedPaperRecord, StagedPaperStore
from .search_runtime import SearchRuntime
from .research_control import AutonomousResearchController, ResearchController
from .review_control import ReviewController
from .review_models import (
    EvidenceCard,
    PromotionManifest,
    ReviewRunConfig,
    SourceRecord,
    SourceSkim,
)
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
    "StagedWikiPublisher",
    "StagedPaperRecord",
    "StagedPaperStore",
    "EvidenceVerificationPipeline",
    "EvidenceRevisionPipeline",
    "NonConsensusAnalysisPipeline",
    "SearchRuntime",
    "PaperIngestDraft",
    "PaperIngestResult",
    "IngestCandidate",
    "ResearchController",
    "AutonomousResearchController",
    "ReviewController",
    "ReviewRunConfig",
    "SourceRecord",
    "SourceSkim",
    "EvidenceCard",
    "PromotionManifest",
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
