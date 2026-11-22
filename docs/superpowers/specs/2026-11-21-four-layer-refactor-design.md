# 四层架构重构设计（intelligent-tag-platform）

日期：2026-11-21
状态：已批准（2026-11-21，用户）
前置：本地已与 origin/main 同步（4412b14，含 #32–#38）；
2026-11-21 增量影响分析见文末"增量同步影响分析"节

## 意图与动机

用户对平台的判断：代码结构混乱、职责定义不清晰、可读性不高。2026-11-21 全库审查
（3 个审查代理全文核实 18.5k 行后端 + 2.1k 行前端）的结论是：整体纪律强
（分层红线全库零违规、协议模式一贯），混乱集中在局部——读路径 SQL 散布
router/review/governance/results/tasks 五层、三个 400+ 行巨函数、顶层 16 个散文件
粒度不均、死代码簇误导读者。

本次重构把这些局部混乱按"接入层 / 编排层 / 服务层 / 能力层"四层目标架构一次归位，
同时以搬家为载体清偿审查发现的 P0/P1 债务。

## 目标

1. 每个请求的完整步骤（校验→编排→服务→能力）在一处可读、可单测
2. 路由层变薄：只做协议转换；SQL 全部离开 router
3. 拆掉三个巨函数/巨组件（run_open_generation 460 行、run_governance 406 行、AssetList 851 行）
4. 顺修 P0 缺陷与 DRY 债（详见"顺修清单"）
5. 前端按 7 个业务模块归位目录，跨页面手工同步的常量统一为单一来源

## 非目标 / 反目标

- **不改业务语义**：打标算法、评测口径、治理规则零变化
- **不建"统一工具调用"空架子**：现状无此需求（YAGNI），能力层预留概念不预留代码
- **不引入新框架/依赖**：FastAPI/SQLAlchemy/vitest 栈不变
- 反目标：线上服务中断；重构后除白名单外任何端点行为漂移；测试逻辑被弱化（允许搬家与契约更新，不允许删除断言）

## 约束

- 线上 154.212:8100 生产运行（评测进行中）——重构在分支完成，部署为一次性切换
- 约 100 个测试文件（71 后端 + 29 前端，含 PR #32 增量）是安全网：单分支上全部随代码迁移，合入门槛 = 全绿
- 依赖迁移即合并的债务修复不额外膨胀范围
- Windows 开发环境 + Git Bash（remote_exec.py 的 MSYS 路径坑已知）

## 备选方案（已讨论，用户选定 A）

- **A. 四层一次到位（选定）**：全量归位到四层终态。终态最整齐、目录即文档；
  代价是 18.5k 行调用链一次性搬家、测试大改、单分支冲突窗口长。用户知情选择。
- **B. 绞杀者渐进（被否）**：按痛点切三刀（编排层先收 tags/generation → 拆巨函数 →
  清死代码），改动 1/4 但需要纪律防半途而废，终态保留部分现状结构。
- **C. 只做文件重组（被否）**：不建编排层，职责不清的核心问题不动。

## 已拍板的决定（用户）

| 决定 | 选择 |
|---|---|
| 路径 | A：四层一次到位 |
| 第二层命名 | **编排层**（orchestration；否决"装饰层"——与 GoF Decorator 混淆） |
| 落地方式 | 单分支一次合入；重构窗口内 main 冻结（待办 closed 并发化/judge 自评/PG 迁移不落 main） |
| PR #32 | 已合入 origin/main（fa291a7）；本地先同步再切分支 |
| API 契约 | 顺带改端点，但仅限"端点改造白名单"（见下），白名单外契约冻结 |
| 切换时机 | 部署切换前主动 cancel 全部在途评测 run（已完成片结果留盘，在途部分作废可重跑） |
| 部署机制 | **切换时上容器**（2026-11-21 更新，基于 #36 已合入的多阶段 Dockerfile）：
镜像构建 → 容器替换；回退 = 旧镜像 tag 回滚（比 git checkout 更干净）。
线上 154.212:8100 当前仍 systemd 直跑，容器化与四层重构在同一停服窗口切换 |
| 回退代价 | 允许丢切换→回退窗口内的写入：回退 = 还原 DB 备份 + git 切回旧提交；新迁移仍附 downgrade 作为零丢失备用路径 |
| Prompt 存储 | **无 prompt_template 表**：全部 prompt 为本地 Python 常量（git 即版本机制）；配置管理页只读不可改；DB 版本机制/激活切换/编辑端点全删 |

假设（如不符请指出）：`/api/v1` 除 SPA 前端外无其他消费方（服务器 cron、外部脚本、研究工具均直连 DB 不走 HTTP）。

## 目标目录结构

```
src/tagplatform/
├── api/                    # 接入层：薄路由
│   ├── routers/            # 15 个 router 瘦身：解析入参→调编排层→返回
│   ├── schemas.py          # 请求/响应模型（按域拆分亦可，builder 定）
│   └── depends.py          # 鉴权装配（现状达标，保持）
│
├── orchestration/          # 编排层：每请求一个步骤清单，零 SQL 细节
│   ├── tags.py             # ← routers/tags.py 读路径 SQL + 装配逻辑
│   ├── generation.py       # ← _result_out/_tag_info/_dir_info（N+1 根治）
│   ├── review.py           # ← routers/review.py 内嵌查询装配
│   ├── eval.py / content.py / imports.py / config.py / media.py
│   └── tasks.py            # 生成任务生命周期编排（创建/重跑/查询/取消）
│
├── services/               # 服务层：五个业务模块（config 经 2026-11-21
│   │                       # 精简审计砍除：prompt 移能力层后仅剩"读冻结的
│   │                       # Settings 返回视图"——无状态/无事务/无写，不构成
│   │                       # 服务；config 视图由 orchestration/config 直读
│   │                       # settings 拼装）
│   ├── auth/               # ← auth/（用户鉴权与管理）
│   ├── tags/               # ← library.py + governance/（标签导入与查询；
│   │                       #    Excel 双表治理导入归此）
│   ├── content/            # ← content_import/content_io/content_template/
│   │                       #    ingest/movie_import/media.py（内容导入与查询）
│   │                       # + assets/（资产构建）
│   ├── generation/         # 标签生成与评审；按任务类型分子模块，
│   │   ├── base.py         #   动态扩展第三种任务类型 = 新子目录 +
│   │   │                   #   实现 TagProducer + 模块导入时注册
│   │   │                   #   （get_producer(task.mode) 自动分发）
│   │   ├── closed/         # ← closed.py（416 行，#38 多模态 evidence 打分
│   │   │                   #   后接近巨函数门槛：拆 producer 与 evidence
│   │   │                   #   融合评分）+ recall.py（hybrid BM25+RRF 召回）
│   │   │                   #   + parse.py（← llm/parsing.py 整体迁入：
│   │   │                   #   parse_closed_output 是模式 A 输出契约解析
│   │   │                   #   ——业务协议非技术设施，与其 docstring 自述
│   │   │                   #   "client 只管传输不做解析"的分工一致；
│   │   │                   #   文案（0016 播种）/解析器/producer 三者
│   │   │                   #   同域相邻，协议两半不再跨能力模块分家）
│   │   ├── assemble.py     # ← assemble_spec 从 tasks/runner 抽出下沉并两级化：
│   │   │                   #   load_assembly_context（唯一 DB 交互：库/prompt/
│   │   │                   #   policy 一次装配）+ build_spec（纯内存组
│   │   │                   #   GenerationSpec）。零漂移契约载体：生成=执行时
│   │   │                   #   装配（活库），评测=run 启动装配一次全程零
│   │   │                   #   DB（快照语义，可复现性）
│   │   ├── open/           # ← open_gen.py（开放生产器）+ mtg/（管线实现）
│   │   ├── persist.py      # ← results.py（persist_tags 生成域写操作：
│   │   │                   #   执行序列 assemble→produce→persist 第三环，
│   │   │                   #   归服务层非编排层；响应装配留 orchestration）
│   │   └── review/         # ← review/（审核服务，同域）
│   └── eval/               # ← eval/ + closed_eval.py（1229 行拆三分：数据集层
│                           #    发现/加载/选片、干跑运行器 ClosedEvalService、
│                           #    报告组装）。评测两路形态：**闭环=干跑式**
│                           #    （assemble_spec→produce 真重生成，DB 零写入，
│                           #    复用切点在落库前——persist_tags 不复用）；
│                           #    open 侧 tag_quality=读历史重放评分式
│
├── capabilities/           # 能力层：统一能力收口
│   ├── llm/                # ← llm/ 收缩为纯传输（client.py + mock.py——
│   │                       #    兑现 client docstring "只管传输不做解析"；
│   │                       #    parsing.py 业务协议解析迁 services/
│   │                       #    generation/closed/parse.py，见防滑坡规则）
│   ├── embed/              # ← embed/
│   ├── prompts/            # ← 顶层 prompts.py 迁入并简化为纯读设施：本地模板
│   │                       #    常量（templates.py，每 mode 一条，初始值 =
│   │                       #    线上 DB 当前激活文案导出）+ render_prompt 渲染
│   │                       #    + 占位符 schema（白名单=契约端点单一来源）。
│   │                       #    get_prompt(mode) 无 DB 无状态；版本机制删除
│   │                       #    （git 即版本）。无 prompt_template 表
│   ├── storage/            # ← db/ + 磁盘文件读写（content_io 的存储部分）
│   └── adapters/           # ← adapters/（内容格式转换能力）
│
├── tasks/                  # 横切：纯任务管理器（2026-11-21 定稿：执行器
│   └── runner.py           #   只管任务生命周期——状态机/调度/线程池/孤儿
                            #   重置；装配+produce+persist 业务序列收进
                            #   服务层执行函数 run_generation_task
                            #   （services/generation），执行器只调用。
                            #   两阶段装配：请求时编排层组装轻量参数
                            #   （mode/policy_json/asset_id 存任务行）；
                            #   执行时服务层装配活资源（库/模板渲染）——
                            #   库活性语义与 spec 不可序列化性决定重资源
                            #   不能预装配。asset_build 执行侧同理收编）
└── settings.py / mounting.py / metrics.py / vectors.py
    # 横切支撑件归位（2026-11-21 精简审计后定稿）：metrics→services/eval，
    # mounting→services/tags，vectors→capabilities；results.py 已显式落位
    # services/generation/persist.py（persist_tags 写操作）；config 无独立
    # 服务模块（orchestration/config 直读 settings）
```

**精简审计结论**（2026-11-21）：四层骨架无空转层；砍除 services/config
空壳（prompt 移走后仅剩读冻结 Settings 的视图，无状态无事务无写）；
persist_tags 归位服务层（原倾向 orchestration/review 错位）；
_validate_template_text 退役为测试断言；前端 open-eval 只建目录占位
不写代码（YAGNI）。capabilities/prompts 虽薄但保留（3+ 域共享 +
渲染是行为）；装配两级化为评测零 DB 的必要形态，非过度设计。

分层依赖红线（新增，与既有"领域不 import fastapi"并列）：
api → orchestration → services → capabilities；tasks 只被 orchestration 调用；
任何层不得反向 import。

同层依赖规则（2026-11-21 增补，经现状 grep 实测依赖为无环 DAG 后定）：

| 层 | 同层互相依赖 | 规则 |
|---|---|---|
| 服务层 | 分模块性质（见下） | 领域库与服务分治：领域库可被任何服务 import；服务彼此零直接 import |
| 编排层 | 禁止 | 模块间互不 import；共享装配收进 orchestration/_shared.py |
| 能力层 | 禁止 | llm/embed/storage/adapters 平行互不依赖 |
| 接入层 | 禁止 | router 之间互不 import（现状 review/imports 跨 router 私有导入要修） |

服务层模块二分（2026-11-21 增补，经逐符号解剖现状 import 后定）：

- **领域库**（无状态纯函数/数据结构/契约：library、mtg/prompts 工具、
  content_io、generation/base）——跨域共享语言，允许被任何服务 import。
  现状全部"同层依赖"经解剖均属此类（类型/常量/纯函数），无一为服务调用
- **服务**（有状态、管事务/会话/线程：review/service、closed_eval、
  tasks/runner 调度）——彼此零直接 import；跨服务协作只经注册表
  （get_producer 式间接，现状已达成）或编排层装配
- **跨域数据契约**（≥3 域共享的类型/常量）上移契约包（schemas/ 既有实践的
  推广）；仅 2 域共享的留在被依赖方底座（library 模式）
- 参数注入适用于"运行时选择"耦合（哪个 llm/embedder/库实例——决策权上移，
  现状已做）；不用于消除契约/纯函数共享（类型注解即知识，注入是伪规避且
  造成签名爆炸与编排层全知）
- **library 读写分侧规则**（2026-11-21 定稿）：计算层已参数注入
  （spec.library——produce 及管线对标签管理零调用，现状即如此）。
  library 读侧（TagLibrary 数据结构/匹配纯函数/try_load_library 加载，
  无状态无事务）= 共享领域库，generation/review/eval 可自由 import；
  标签管理写侧（目录树变更/治理导入/标签增删）= 服务操作，只能由
  编排层调用，生成域零接触。防滑坡：library.py 若长出有状态写方法
  即违反本规则（现状 generation 的 4 处 import 全为读侧符号，
  写侧全在 governance，天然合规）。生成路径库装配在执行时
  （活库语义，后台执行器无请求上下文——"库必须经编排层注入"在
  后台流程上构成反向依赖，不可行）；评测路径库装配在 run 启动时
  由编排层注入（快照语义，已采纳）——两路分叉即装配两级化定稿
- **装配统一 vs 各类型自带的分界**（2026-11-21 定稿）：装配不拆散到
  closed/open/评测各自实现——评测与 closed 装配需求完全相同（零漂移的
  定义）；共享段（库加载/policy/spec 构造）复制即漂移；**动态扩展承诺
  （新类型+注册即自动兼容）的机制核心 = 装配是统一入口、producer 只接
  spec**，拆散装配 = 调度侧被迫知道各类型差异（if-else 或 getattr 探测，
  OCP 与协议缝双双回归）。类型差异的正确归宿是**协议声明**（样板 =
  builds_prompt：producer 声明需求，装配统一响应）；未来新资源需求
  （帧索引/向量库等）按同一模式演进：协议加声明 → 统一装配响应 →
  调度侧永远零改动
- **装配归属判据**（编排层 vs 服务层领域库）：产出"协议响应对象"
  （HTTP 响应 DTO、请求级用例组装）→ 编排层；产出"领域契约对象"
  （GenerationSpec 等领域输入的构造）→ 服务层领域库。硬约束：装配的
  消费方若含后台流程（runner/守护线程，无请求上下文），装配必须低于
  编排层，否则 services/tasks 反向依赖 + 调度循环。**编排层决定装配
  何时发生、以什么参数发生（触发者）；装配怎么发生（领域规则）住
  服务层（实现者）**——同 produce 被编排层触发而实现在生成域。
  assemble.py 独立成文件的理由：两个以上消费方（执行函数/评测干跑/
  未来任务类型）共享的领域段需单一住所（并回 runner=红线违规、并
  编排层=反向依赖、散进 producer=破坏注册表扩展性、并 base.py=污染
  契约模块零依赖性）

- 现状实测依赖图（设计后保持）：library 与 governance 零出依赖（底座）；
  generation/review → library；eval → generation + library；closed_eval →
  eval + generation + library；tasks/runner → 各域（横切，方向向下，允许）；
  closed_eval/runner/generation 读模板 → capabilities/prompts（层间向下，
  取代原 → services/config 的横向 import）
- **闭环评测×闭环生成的复用协议**（2026-11-21 分析定稿）：共享装配
  （services/generation/assemble.py，零漂移契约载体）+ 共享计算
  （get_producer 注册表间接 → ClosedTagProducer.produce）；**切开持久化**
  （persist_tags 只在正式执行器，评测 DB 零写入）。重构修复一处现状
  红线违规：assemble_spec 原住在 tasks/runner（服务借用横切执行器私有段），
  下沉为生成域领域库后，runner 与 closed_eval 双双向下依赖，
  "tasks 只被 orchestration 调用"红线恢复成立
- **装配两级化 + 评测预装配**（2026-11-21 定稿）：assemble.py 拆
  `load_assembly_context(session, mode, policy_json)`（唯一 DB 交互点，
  产出 library/prompt_text/policy 的装配上下文）+
  `build_spec(context, content, ids)`（纯内存组 GenerationSpec）。
  两路语义分叉为设计意图：**生成**=执行器内装配（活库语义，任务用
  执行时刻的库）；**评测**=run 启动时（编排层请求上下文）装配一次、
  守护线程全程内存引用共享——评测过程 DB 零交互（content 文件系统读、
  产物文件写）。零漂移定义校准为"装配逻辑同段"（同一对函数），
  装配时刻与库语义（活/快照）的分叉是有意设计。顺修价值：根治现状
  "run 中途库/激活模板变更导致前后片口径不一"的可复现性隐患
  （现状每片 get_active_prompt 查 DB，无 run 级一致性保证）
- **能力层防滑坡规则**：能力层允许的业务知识上限 = schema 常量（如占位符
  白名单、mode 取值——定位为存储约束，类比 DB CHECK）；流程编排、算法、
  领域规则进能力层 = 违规。消息构造（mtg/prompts.py 的 render_content/
  build_judge_messages 等）是生成域私有知识，永不进能力层
- 成环处置：新依赖成环 = 职责切错，三选一——下沉共享逻辑到共同被依赖方 /
  抽公共子模块 / 抬到编排层；禁止靠挪 import 位置绕
- 协作风格延续现状参数注入（run_open_generation 签名式）：服务函数接收
  协作者为参数，模块 import 仅为类型与构造

## 端点改造白名单（顺带改端点的全部范围）

| 端点 | 问题 | 改法 | 契约变化 |
|---|---|---|---|
| `GET /api/v1/tags`（list） | 全表加载 + Python 切片分页、无 total | SQL LIMIT/OFFSET + total 字段 | 响应加 total（前端 TagCatalog 同步） |
| `GET /tags/export` | 两表全量进内存 + Python BFS | 流式/分批组装 | 无 |
| `GET /generation/tasks/{id}`、`GET /assets/{id}/tags` | `_dir_info` 逐行全库重载（O(行×库)） | 编排层单次库加载传参 | 无 |
| imports 批次反查 | 全表扫描 + JSON 文本 Python 侧过滤 | 迁移把批键提为列（Alembic 新版本） | 无 |
| `GET /api/v1/media/build-tasks/{task_id}/view` | 43 行 HTML/JS 内嵌 router + task_id 直插 JS 无转义 | 后端静态 HTML 文件 + 转义替换（URL 保留，前端零改动；不做 SPA 新页面） | 无 |
| 登录/建号 | cookie 无 samesite；用户不存在时跳过 PBKDF2（时序枚举） | samesite=lax + dummy verify | 无 |
| `GET/POST /api/v1/prompts`、`POST /prompts/{id}/activate` | prompt 走 DB 版本机制（与本地化决定冲突） | GET 保留为**只读视图**（返回各 mode 模板文本 + allowed/required 占位符，兼作契约单一来源）；POST 与 activate **删除**（配置页只读） | GET 响应结构变（去版本字段）；POST/activate 端点消失 |
| review/imports 跨 router 私有导入 | `_result_out`/`_tag_info` 等私有符号被寄生共用 | 收进 orchestration 公共装配模块 | 无 |

白名单外所有端点 URL、请求、响应结构**冻结**。

## 前端改造范围

1. 目录按 7 模块归位：tag-management / content-management / generation / review /
   closed-eval /（open-eval 只建目录占位不写代码）/ config
2. 跨页面常量（ORIGIN/TARGET/三态 review labels/colors，3 处手工同步）→ `shared/` 单一来源
3. AssetList.tsx 851 行拆 4 子组件（列表 / 上传向导 / 进度轮询 / 目录导入 Modal）
4. **ModelConfig.tsx 编辑表单改为只读视图**（prompt 模板查看 + 占位符说明从
   GET /prompts 契约拉取——根治 P0 #4 文案矛盾）；api client 层同步 tags list
   响应结构变化；删 library.ts 死模块（113 行零引用）
5. 413 子串判断改用 ApiError.status

## 顺修清单（搬家即合并，不另开修复）

- 归一化键×3 → 单一 `normalization.py`（merge/judge/pipeline 共用）
- 噪音词表×2 漂移 → 单一词表常量（dataset 与 asset_build 共用）
- `_PLACEHOLDER_MARKS`×2、aliases 双解析器 → 单一实现
- excel.py 三路径 pprint 调试残留 → 删
- scripts/reembed_catalog.py 坏脚本 + 三处活代码指引 → 删脚本改指引
- TagProducer 协议补 `prompt_version`/`builds_prompt` 声明（消 getattr 默认；
  现场就在 assemble_spec 的 `getattr(producer, "builds_prompt", False)` 与
  runner 的 prompt_version 分支，补齐后改直接属性访问）——
  这是"动态扩展新任务类型"承诺成立的前提：漏实现成员必须在注册时报错，
  而非静默写空串进 generation_task
- `run_open_generation` 按现成 12 个编号小节拆函数；`run_governance` 闭包状态改显式参数
- usage 累加×5 → 单一 helper；`_make_judge_client`/`_make_verify_client` 合并参数化
- `isinstance(MockLLMClient)` 测试关注点泄漏 → 协议方法判定
- scope 组合值契约断裂（`scope_of` vs DB 默认值）：**需先拍板组合值语义**
  （选项：`scope_of` 支持解析组合值 / 收紧 DB 约束禁组合值），默认按"支持解析"实现
- prompt 文案单轨归宿（判据 = 谁改它）：全部 prompt 为开发者资产——
  **运营主模板本地化**（capabilities/prompts/templates.py 常量，**初始值
  直接取 models.py 当前 _PROMPT_V1_CLOSED_TEXT 内容**——#38 的 0016
  迁移播种的就是该常量，权威源已在代码中，无需线上 dump，git 即版本）
  + **管线固定指令**（judge/verify/descriptions 提示词 → 域内模块级
  命名常量，与解析器相邻声明协议）。迁移顺序硬约束：**0012/0013/0016
  三个迁移先改字面量快照**（0016 同为活常量 import，#38 新增）→
  再删 models 常量与 PromptTemplate ORM → 新增 drop prompt_table
  迁移（编号 0017 起，0016 已被占用；否则 fresh 迁移链在活常量处断链）
- AssetBuildTask/TagImportTask 孤儿重置：重启后卡 running 的任务复位为 failed
  （用户已批准随 cancel 决定带入；对齐 generation 的 reset_orphan_running 模式）
- 管线指令字面量提升为模块级命名常量：judge/verify/descriptions 的提示词
  从函数体 f-string 提为 `mtg/prompts.py` 等域内常量，与解析器常量相邻，
  docstring 声明"文案与解析互为协议"——根治审查 nit #11（judge 主/重试
  提示格式互相矛盾靠 parse 容错兜住的现行漂移）
- settings.py:157 注释 0.87 vs 值 0.74 漂移 → 校正
- prompt 种子文案相关顺修随本地化决定并入：版本机制（create/activate/
  单事务保证）删除；models.py 的 _PROMPT_V1_CLOSED_TEXT 常量删除；
  alembic 0012/0013 的活常量 import 改为字面量快照（**从可选升级为
  前置必须**，见迁移顺序硬约束）
- library.py:391 缓存键改用契约字段 `embedder.name`
- `_validate_template_text` 退役为测试断言（模板为代码常量后运行时校验
  失去意义：断言常量含必需占位符、无非法字段——错了测试红而非运行时
  拦截）；capabilities/prompts 最终形态 = get_prompt + render_prompt
  + templates.py 常量

## 执行策略

1. 本地同步 origin/main（#32 已在 fa291a7）→ 切 `refactor/four-layer` 分支（worktree 隔离）
2. main 冻结到合入：closed 并发化、judge 同模自评、PG 迁移等待办不落 main
3. 单分支上按层推进（骨架目录 → capabilities → services → orchestration → routers 瘦身
   → 前端），每步测试随迁、随时全绿；**不合入 main 直到全部完成**
4. 部署 runbook（切换 = 一次停服窗口，容器化 + 重构同窗）：
   a. 主动 cancel 全部在途评测 run（已完成片结果留盘；在途重跑为已知代价）
   b. prompt 初始值 = models.py 当前常量内容（#38 后 0016 播种源即代码，
      权威源已在代码中，**线上 dump 步骤取消**）；templates.py 落定后
      测试断言渲染与常量逐字一致，此后才允许 drop 表
   c. 停服 → 基于重构合入提交构建镜像（#36 多阶段 Dockerfile：node
      构建 dist + python 运行时）→ 停旧 systemd 服务 → 起新容器；
      alembic 迁移随容器入口执行（0017+：批键列 + drop prompt_template；
      0016 多模态 evidence 播种在前按序执行；入口命令细节部署时按
      #36 实际启动方式核对）
   d. smoke：白名单外端点逐个比对响应；基线 = 切换前的线上实际版本快照
      （含 #33–#38 演进；#32 报告页字段差异列入豁免清单）；
      新增：prompt 渲染输出与常量逐字比对
   e. 切换前取 DB 备份
5. 回退 runbook：停容器 → 还原 DB 备份（回到切换点，接受丢窗口内写入）→
   回滚旧镜像 tag 重启 systemd 服务（或旧容器）。备用零丢失路径：
   先 `alembic downgrade`（新迁移附 downgrade）再回滚镜像
6. 验收标准：
   - 约 100 个测试文件迁移后全绿，断言总数不减
   - 白名单外端点 smoke 逐个比对响应一致
   - 新分层红线经 grep 机械验证（api→orch→services→capb 无反向 import）
   - P0 顺修清单逐项勾销

## 留给计划阶段（builder）

- 各文件到目标位置的逐文件映射与拆分边界——**最大的未映射块必须显式落位**：
  closed_eval.py（1258 行，第二大模块，闭环评测守护服务）、tag_import.py、
  filtering/、schemas/（assets/ 已落位 services/content）
- 巨函数拆分的具体函数签名与模块归属
- 测试文件与 fixture 的迁移映射
- Alembic 批键列迁移的 schema 细节（含 downgrade 路径）
- 前端目录结构与 import 路径更新

## 增量同步影响分析（2026-11-21，fa291a7→f454457→4412b14）

origin/main 较 builder check 基线新增 #33（hybrid 召回 BM25+RRF）、
#34/#35（θ 拆分与统一取格）、#36（容器化部署）、#37（输出筛选 Sf 保底
截断）、#38（**多模态 evidence 打分**——closed.py +169 至 416 行、
0016 新迁移播种新 prompt、llm/parsing.py +122 解析 evidence 三模态
契约、models 常量演进）。逐项核实结论：

- **六项设计假设全部成立**：重构核心区（tags/governance/library/
  mtg-open/AssetList/ModelConfig）零变更（顺修清单、白名单、审查基线
  有效）；pyproject 零变化（BM25+RRF 自实现）；runner 执行序列不变
  （+8 行为 persist 留痕日志）；TagProducer 协议形态不变；closed.py
  416 行接近但未过巨函数门槛（列入拆分关注）
- **迁移链影响（#38 实质变更）**：0016 已占号 → 设计新迁移从 0017 起；
  **0012/0013/0016 三个迁移均 import 活常量**，字面量冻结前置从两个
  扩到三个；prompt 权威源已回到代码（0016 播种源 = models 当前常量）
  → **线上 dump 步骤取消，templates.py 初始值直接取常量**（简化红利）
- **能力层防滑坡规则的实例落位**：llm/parsing.py 的
  parse_closed_output（evidence 三模态契约校验）是业务协议非技术
  设施——整体迁 services/generation/closed/parse.py；llm/ 收缩为纯
  传输（client+mock），兑现 client docstring 自述的分工承诺；文案
  （0016）/解析器/producer 三者同域相邻，协议两半不再跨能力模块分家
- **零漂移契约被实践验证**：#37/#38 输出筛选与多模态打分进 closed.py
  的同时评测侧（closed_eval/eval router/前端表格）同步跟改——设计的
  复用协议描述连续两次被真实演进检验
- **照搬型演进**：#33–#38 全为 closed 域业务演进，"不改业务语义"非
  目标条款自动覆盖；eval 端点 theta_p/theta_r 查询参数属已上线演进
- **设计校准**：persist.py 归位照搬 #37 入库留痕日志；closed.py 拆
  producer 与 evidence 融合评分；测试规模再涨 ~1600 行（#37+38）
- **部署机制更新**：原拍板"git + systemd"更新为"切换时上容器"
  （#36 Dockerfile 已合入；线上当前仍 systemd，容器化与重构同窗切换，
  runbook 见执行策略；回退基线为 0016）
