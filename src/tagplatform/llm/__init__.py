"""LLM 客户端包出口：`make_llm_client` 是唯一构造入口，契约类型供注解。"""

from tagplatform.llm.client import (
    LLMResponse,
    OpenAIClient,
    Transport,
    make_llm_client,
)
from tagplatform.llm.mock import MockLLMClient

__all__ = ["LLMResponse", "MockLLMClient", "OpenAIClient", "Transport",
           "make_llm_client"]
