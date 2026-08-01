"""LLM único da squad, servido pelo OpenCode Zen (API compatível com OpenAI).

O CrewAI usa LiteLLM por baixo; o prefixo "openai/" indica endpoint
OpenAI-compatível com base_url customizada.
"""
import os

from crewai import LLM

ZEN_BASE_URL = "https://opencode.ai/zen/v1"


def zen_llm(model: str | None = None) -> LLM:
    return LLM(
        model=model or os.getenv("MODEL", "openai/kimi-k2.7-code"),
        base_url=os.getenv("OPENCODE_BASE_URL", ZEN_BASE_URL),
        api_key=os.environ["OPENCODE_API_KEY"],
    )
