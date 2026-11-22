"""标签域包出口：领域库（library）+ 治理导入（governance）+ 挂载解析（mounting）。

四层重构 Task 8 归位。分层红线：本包不得 import fastapi（设计文档 §9.4）。

- `library`：标签库只读视图（tag_catalog / tag_define 两表）+ 向量索引——
  **读侧领域库**，generation/review/eval 任何服务可自由 import；
- `governance`：标签治理导入流水线（excel/xml 解析 + 双表落位）——**写侧
  服务操作**，只能由编排层调用，生成域零接触（library 读写分侧规则）；
- `mounting`：目录 applicable_scope → 挂载点解析（生成域消费的纯函数）。

公共名字都在子模块（`tagplatform.services.tags.library` 等），本包不做
重导出——library 的消费方横跨 generation/eval/review/脚本，统一从子模块
import 保持 import 图可读。
"""
