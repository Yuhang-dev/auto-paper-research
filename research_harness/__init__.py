"""LangGraph orchestration for the LLM-Wiki research harness."""

import os

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from .config import HarnessSettings
from .graph import ResearchHarness
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
