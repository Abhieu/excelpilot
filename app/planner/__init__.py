"""Planning: natural language to a validated execution plan.

Two implementations behind one protocol. The deterministic compiler is the
default and needs no credentials; the LLM path is opt-in and its output is
treated as untrusted input (ADR-0003).
"""

from app.planner.deterministic import (
    COLUMN_SYNONYMS,
    DeterministicPlanner,
    Intent,
    Planner,
)
from app.planner.llm import (
    AnthropicCompatibleProvider,
    LLMPlanner,
    ModelProvider,
    OpenAICompatibleProvider,
    StaticProvider,
    build_planner,
)

__all__ = [
    "COLUMN_SYNONYMS",
    "AnthropicCompatibleProvider",
    "DeterministicPlanner",
    "Intent",
    "LLMPlanner",
    "ModelProvider",
    "OpenAICompatibleProvider",
    "Planner",
    "StaticProvider",
    "build_planner",
]
