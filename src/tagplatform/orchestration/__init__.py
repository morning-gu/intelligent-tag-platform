"""编排层（orchestration）：协议响应对象与请求级用例组装。

四层架构（spec 2026-11-21）：`api → orchestration → services → capabilities`。
本层产出"协议响应对象"——HTTP 响应 DTO（pydantic 响应模型、Response/文件流
对象）与请求级用例组装（每请求一个步骤清单：查库、调服务、装配响应），供
接入层薄路由直接调用（router 只做 入参解析 → 调编排 → 返回）。

同层规则（红线，Task 23 机械验证）：
- 本包模块互不 import——跨域共享的装配（pre_asset_tag 行输出三件套、共享
  DTO）一律收进 `_shared.py`；
- 允许向下 import services / capabilities / tasks（tasks 只被编排层调用）；
- 禁止向上 import api（响应时间戳类型 `UtcDatetime` 因此定义在 _shared，
  api/schemas.py 反向 re-export 供接入层按原路径引用）。

域模块清单（每模块 docstring 声明该域用例清单）：

- `_shared.py`    跨域公共装配：pre_asset_tag 行输出三件套、导入结果摘要、
                  标签库导出归档
- `tasks.py`      生成任务生命周期（创建/列表/详情/质量评估重跑/任务重跑）
- `generation.py` 资产正式挂载视图（GET /assets/{asset_id}/tags）
- `tags.py`       标签库查询/导入/生命周期/导出 + 标签库统计视图（library/ingest）
- `review.py`     审核操作（单条/批量）与审核列表/筛选
- `imports.py`    标签导入任务（接收/列表/详情/导出/撤销）
- `content.py`    内容导入与资产查询 + 影片目录导入
- `media.py`      媒体上传/文件名预检/构建任务/进度页/视频流
- `eval.py`       评测 admin 端点（闭环 + 开放 open-eval Task 6：数据集根/数据集/run 生命周期/报告）
- `config.py`     只读运行配置视图（直读 Settings 拼装，无 services/config）
"""
