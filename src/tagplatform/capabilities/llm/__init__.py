"""LLM 客户端包出口：`make_llm_client` 是唯一构造入口，契约类型供注解。

能力层定位（四层重构 Task 2）：本包是纯传输能力（传输/重试/response_format
降级/限速/usage 记录），业务协议解析不在此层（模式 A 输出解析住生成域
services/generation/closed/parse.py，Task 4 迁入；client 只管传输不做解析
的分工由此兑现）；防滑坡规则：流程编排、算法、
领域规则禁止入层，业务知识上限 = schema 常量。
"""

from tagplatform.capabilities.llm.client import (
    LLMResponse,
    OpenAIClient,
    Transport,
    make_llm_client,
)
from tagplatform.capabilities.llm.mock import MockLLMClient
from tagplatform.capabilities.llm.usage import accumulate_usage, combined_usage

__all__ = ["LLMResponse", "MockLLMClient", "OpenAIClient", "Transport",
           "accumulate_usage", "combined_usage", "make_llm_client"]
