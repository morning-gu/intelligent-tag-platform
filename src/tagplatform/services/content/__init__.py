"""内容域包出口：内容导入/模板形态/存储读写/入库/影片目录导入/上传/资产构建。

四层重构 Task 9 归位。分层红线：本包不得 import fastapi（设计文档 §9.4）。

- `content_import`：统一内容 JSON → media_asset 层级落库（影片行整块覆盖
  + 场景行 upsert）——服务操作，只能由编排层/API 调用；
- `content_template`：资产内容 JSON 的模板形态（唯一存储/对外口径 =
  new_json.txt）转换；
- `content_io`：存储形态与读取（`load_content` 判别两种形态返回
  ContentJSON）——**跨域共享的领域工具**，generation/eval 任何服务可自由
  import（services 包 docstring 的领域库例证之一）；
- `ingest`：入库动作（审核通过的新标签/新别名写 `tag_define` + 变更日志）
  ——写侧服务操作；别名解析复用 `services.tags.library.parse_aliases`
  （Task 9 统一双解析器，JSON 优先 + 坏 JSON 回落逗号切分）；
- `movie_import`：影片资产目录导入（content.json 已备好，只登记不生成）；
- `media`：上传占位核心层（视频落盘 + media_asset 登记，不做识别）；
- `asset_build`：「上传即生成资产」编排（数据集匹配 → 后台任务 →
  AssetBuilder 逐场景生成 → 内容覆盖回上传的那条资产）；
- `assets/`：资产构建包（builder 逐场景生成 + dataset 文件名匹配 +
  emotions 词表 + subtitles 解析）。

公共名字都在子模块（`tagplatform.services.content.content_import` 等），
本包不做重导出——消费方统一从子模块 import 保持 import 图可读。
"""
