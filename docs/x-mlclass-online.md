# 在线 X-MLClass 实验

实现对应 [在线发现设计](online-label-discovery-design.md) 的 B 组，版本 `x-mlclass-online-v1`。本分支的「算法基准」页面固定运行在线 X-MLClass。正式生产的 `open` 模式使用 `mtg-v3`，实验结果保存在基准报告里。

## 运行

1. 安装算法依赖：`pip install -e ".[xmlclass]"`。现有嵌入器和 LLM 配置继续复用。
2. 准备 NLI 模型。默认 `MoritzLaurer/mDeBERTa-v3-base-mnli-xnli`，支持多语言 NLI；首次运行加载，按 Transformers 的模型缓存机制读取或下载。无外网部署应预先下载完整模型与 tokenizer，并设置 `TAGPLT_XMLCLASS_NLI_MODEL=/path/to/model`。默认 CPU，可设置 `TAGPLT_XMLCLASS_NLI_DEVICE=cuda:0`。普通生产和 A 组不加载该模型。
3. 在 X-MLClass 实例的「算法基准」选择影片并设置标签上限、评估提示词，点击「生成并自动评估」。页面固定调用在线 X-MLClass。
4. 对照时用同一版基准代码部署两个实例，选择相同影片、片序、初始词表、标签上限和评估提示词。两边使用相同的影片素材、关键帧配置、标签和目录提示配置、生成模型及嵌入模型。各实例独立运行并保存报告。报告中的 `generation_protocol: shared-materials-dedup-v1` 标识采用共享材料和最终去重的批次。
5. 展开影片查看标签、场景证据、评估理由和词表增长；完整快照可导出。

通过 API 创建独立批次。

```text
POST /api/v1/benchmarks
{"algorithm":"x-mlclass-online-v1","asset_ids":["film1","film2"],"max_tags":15}
```

## 算法与论文的对应

- 用初始标签库里的有效标签直接初始化实验词表，保留原 ID、释义和别名；跳过论文的 GMM 初始化。
- NLI 继续使用场景描述、视觉特征、台词、事件、实体、氛围等已有识别文本。片名、场景 ID 和时间戳用于定位，空场景跳过。
- 用现有嵌入器召回每场景前 8 个标签，NLI 对每个“场景、标签假设”独立打分。支持分为该对的 entailment logit 在关系类别上的 softmax 概率；包含 neutral，不在候选标签之间归一化。增加候选不会改变原有文本对的支持分，但召回集合仍可能变化。
- 对最高支持分小于 0.5 的可用场景，从低到高最多选择 5 个，每场景提取最多 3 个关键词。提词模型看到与 MTG-v3 相同的全片文本、按场景标记的关键帧、初始库相关标签和可扩展目录。每次通过场景 ID 指定提词范围，关键词引文须来自目标场景中发给模型的描述或台词。
- 原词做 Unicode NFKC、大小写和标点归一化；先查工作词表，再与候选池代表词比较。向量只产生同义判断的短名单，必须经过 LLM 确认同义才合并；相关或上下位不合并。候选池仅做代表词级归并，不做全局聚类。
- 支持数按不同 `asset_id` 计，同片多个场景、重复处理同片都只贡献一部。只要在本片有经过校验的发现记录，候选词就能参与本片评分。候选词默认累积到至少 2 部影片、通过工作词表查重后才加入共享工作词表。入表记录和来源保存在影片审计中。
- 本片有待累计的新词或发生共享入表时，重跑一次场景召回和 NLI。本片候选词和本轮入表的新词即使未进入向量召回前 K 项，也会参与 NLI。按场景排名逐列投票，票数并列按最高支持分、稳定标签键排序；最高票为一票时仅取该列最高分项；跳过已输出标签。投票前排除支持分小于 0.7 的标签。基准结果保留支持场景和时间区间，显示共享 LLM 请求中的场景描述字段。现行请求将该字段截到 240 个字符，页面按请求原样显示。原始内容随快照保存。
- 两边都返回 `ProducedTag`。基准在算法排序后、统一截取前 K 条前执行最终去重。先按归一化名称去重，再用 `mtg_merge_threshold`（默认 0.75）筛选相似标签对，由同一生成模型批量确认是否重复传达同一信息。组内各对都确认重复后保留排序靠前的标签及其证据。导出审计记录复核结果和被合并的标签。X-MLClass 的 `confidence` 记录最大场景支持分。

这是设计文档定义的在线变体：只对低覆盖场景即时提词，最终投票只使用场景文本；不执行论文的全语料预提词、关键词第二路投票、频率大于 15 次/低于 1% 筛选或高频标签暂存。论文复现细节见 [论文分析](x-mlclass-paper-analysis.md)。默认阈值是首轮实验参数，应根据中文影视数据验证。

NLI 当前按 tokenizer 限制到 512 token，超长场景截断前提并保留标签假设；审计仍保留完整场景。长场景尾部可能漏标，这是本版限制。NLI 检查点必须提供明确的 `entailment` 标签映射，不猜测 `LABEL_0` 等编号。

## 状态、失败与评估

评测发现状态来自 `mtg_report_dir/benchmarks` 中仍保留的 X-MLClass 报告。新任务从报告中各自实际发现的关键词恢复候选池和共享词表。删除报告会移除该报告贡献的记录；共享词剩余支持数降到门槛以下时回到候选池，失去全部来源时移除。服务重启后仍可接续保留的报告。每个报告保存生成时的状态快照供导出。重叠运行的任务按影片串行读取和提交发现记录，独立评估可以并行。

每片在状态副本上执行。单个关键词的引文太短或不在原文中时，记录并跳过该关键词，其余关键词继续处理。提词调用失败、响应格式错误、同义判断或 NLI 失败时，保留上一片提交的状态，保存已有审计并继续下一片。生成完成后先保存词表及标签，独立评估失败时保留生成结果。任一影片失败则批次标失败。服务重启时将中断批次标记为失败，新的批次会继承已提交的发现状态。

每个批次独立使用 `glm-5.3-flash` 和 `viewer-quality-v3` 评估标签能否独立读懂，以及能否帮助观众理解或选择影片，输出 keep / rewrite / omit。对照指标为逐片去重后的可展示概念数和可展示占比，改写建议仅供查看。创建批次时可以自定义评估提示词。

算法基准页列出采用当前评估口径的独立批次。已完成或失败的批次可以删除。

生成与独立评估分别记录耗时和 LLM token。展开影片行可查看生成分段耗时，包括向量召回、NLI 模型加载、首轮评分、素材准备、LLM 提词、LLM 同义判断、新词重评分及最终去重。NLI 评分耗时包含分词和推理，模型加载单列。LLM 阶段记录完整调用耗时，包含请求等待和重试。报告导出也包含这些计时。生成 token 包含提词、同义判断和最终去重复核。NLI 记录判断的文本对数。服务未返回 usage 时显示未知，失败生成的部分调用可在审计中查看。最终去重复核失败时该片标记为生成失败，候选池沿用上一次已保存的状态。

## 配置

所有名称均加环境变量前缀 `TAGPLT_`，例如 `TAGPLT_XMLCLASS_MIN_SUPPORT_DOCUMENTS=2`。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `XMLCLASS_NLI_MODEL` | `MoritzLaurer/mDeBERTa-v3-base-mnli-xnli` | 模型 ID 或本地目录 |
| `XMLCLASS_NLI_DEVICE` | `cpu` | PyTorch 设备 |
| `XMLCLASS_NLI_BATCH_SIZE` | 16 | NLI 批大小 |
| `XMLCLASS_NLI_MAX_LENGTH` | 512 | 成对输入最大 token 数 |
| `XMLCLASS_NLI_HYPOTHESIS` | `这个场景包含{label}。` | 标签假设模板 |
| `XMLCLASS_RECALL_TOP_K` | 8 | 每场景召回标签数 |
| `XMLCLASS_LOW_COVERAGE_THRESHOLD` | 0.5 | 发现所用最高支持分阈值 |
| `XMLCLASS_MIN_LABEL_SUPPORT` | 0.7 | 排名与证据的最低支持分 |
| `XMLCLASS_MIN_SCENE_CHARS` | 8 | 可用场景最少字符数 |
| `XMLCLASS_MAX_DISCOVERY_SCENES` | 5 | 每片发现预算，0 关闭发现 |
| `XMLCLASS_KEYWORDS_PER_SCENE` | 3 | 每场景关键词数上限 |
| `XMLCLASS_MIN_SUPPORT_DOCUMENTS` | 2 | 加入跨任务共享工作词表所需的不同影片数 |
| `XMLCLASS_SYNONYM_COSINE` | 0.75 | 同义判断短名单阈值 |
| `XMLCLASS_SYNONYM_TOP_K` | 3 | 每个词最多复核的代表词数 |

NLI 模型使用与标签映射依据[作者模型卡](https://huggingface.co/MoritzLaurer/mDeBERTa-v3-base-mnli-xnli)；排名投票参考 [X-MLClass 作者实现](https://github.com/Kaylee0501/X-MLClass/blob/main/OpenWordMLTC/zero-shot/model_eval.py)。
