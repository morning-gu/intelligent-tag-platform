"""MovieTagGen 移植的开放式生成包（2026-11 起 open 模式的实现）。

模块划分：
- `keyframes`：资产场景 → MovieNet 关键帧（2 帧/场景，全片上限 24）；
- `prompts`：内容渲染 + 多轮生成请求 + judge 请求；
- `parse`：模型输出的容错解析与清洗；
- `merge`：跨轮归一去重 + e5 complete-link 语义聚类；
- `judge`：deepseek-v4.1-flash 逐条判内容支持度；
- `pipeline`：编排（生成 → 合并 → 审核 → 证据校验 → 目录匹配）。

插拔点：平台只认 `open_gen.OpenTagProducer` 这一个 mode="open" 的入口，
内部实现全部在这里。以后本地方案迭代，替换本包即可，runner / 审核 / API
不用动。
"""
from tagplatform.generation.mtg.pipeline import MTG_VERSION, run_open_generation

__all__ = ["MTG_VERSION", "run_open_generation"]
