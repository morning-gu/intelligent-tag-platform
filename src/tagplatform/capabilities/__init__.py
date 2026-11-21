"""能力层（capabilities）：跨域技术能力的统一收口。

层内四包（平行互不依赖，设计文档 §同层依赖规则）：
- llm/      LLM 传输（client/mock：传输、重试、response_format 降级、限速、usage）
- embed/    文本嵌入（mock / 本地 sentence-transformers / OpenAI 兼容远程）
- adapters/ 内容格式转换（平台标准格式与上游视频理解格式的适配）
- storage/  存储基础（SQLite 引擎/会话工厂 + ORM 列契约 models）

分层依赖红线：api → orchestration → services → capabilities，本层不得
反向 import（禁止 import tagplatform.services / orchestration / api）；
本层不得 import fastapi（设计文档 §9.4）。

防滑坡规则（能力层允许的业务知识上限）：schema 常量（如占位符白名单、
mode 取值——定位为存储约束，类比 DB CHECK）可以住在本层；流程编排、
算法、领域规则进能力层 = 违规。消息构造（如生成域 mtg/prompts.py 的
render_content / build_judge_messages）是生成域私有知识，永不进能力层。
"""
