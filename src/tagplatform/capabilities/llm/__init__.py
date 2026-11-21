"""LLM 客户端包出口：`make_llm_client` 是唯一构造入口，契约类型供注解。

能力层定位（四层重构 Task 2）：本包是纯传输能力（传输/重试/response_format
降级/限速/usage 记录），业务协议解析不在此层（parsing.py 暂留旧路径
tagplatform.llm.parsing，后续任务迁生成域）；防滑坡规则：流程编排、算法、
领域规则禁止入层，业务知识上限 = schema 常量。
"""

from tagplatform.capabilities.llm.client import (
    LLMResponse,
    OpenAIClient,
    Transport,
    make_llm_client,
)
from tagplatform.capabilities.llm.mock import MockLLMClient

__all__ = ["LLMResponse", "MockLLMClient", "OpenAIClient", "Transport",
           "make_llm_client"]
