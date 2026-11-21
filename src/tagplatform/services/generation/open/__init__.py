"""开放式生成（模式 B）子包：producer + mtg/（MovieTagGen 移植管线）。

平台只认 `producer.OpenTagProducer` 这一个 mode="open" 的注册入口，
管线实现（多轮生成、语义合并、judge 审核、证据校验）全部在 `mtg/` 包；
以后本地方案迭代，替换 `mtg/` 即可，runner / 审核 / API 不用动。
"""
