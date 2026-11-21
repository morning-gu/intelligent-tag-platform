"""兼容垫片（四层重构 Task 2 迁移期，随 parsing 迁生成域后整体删除）。

llm 的传输层（client/mock）已迁 `tagplatform.capabilities.llm`；本目录
仅暂留 parsing.py（模式 A 输出解析，属业务协议非传输能力），生成域消费
方暂不改路径——旧路径 `tagplatform.llm.parsing` 保持可 import，此处
re-export 公共名兜底包级 import。后续任务将 parsing 迁入生成域并删除
本垫片。
"""

from tagplatform.llm.parsing import LLMParseError, parse_closed_output

__all__ = ["LLMParseError", "parse_closed_output"]
