# 在线 X-MLClass 实验

实现对应 [在线发现设计](online-label-discovery-design.md) 的 B 组，版本 `x-mlclass-online-v1`。入口在「算法基准」；正式生产的 `open` 模式继续使用 `mtg-v3`。实验不写正式标签、候选审核表或正式词表，无数据库迁移。

## 运行

1. 安装算法依赖：`pip install -e ".[xmlclass]"`。现有嵌入器和 LLM 配置继续复用。
2. 准备 NLI 模型。默认 `MoritzLaurer/mDeBERTa-v3-base-mnli-xnli`，支持多语言 NLI；首次运行加载，按 Transformers 的模型缓存机制读取或下载。无外网部署应预先下载完整模型与 tokenizer，并设置 `TAGPLT_XMLCLASS_NLI_MODEL=/path/to/model`。默认 CPU，可设置 `TAGPLT_XMLCLASS_NLI_DEVICE=cuda:0`。普通生产和 A 组不加载该模型。
3. 在「算法基准」运行 5–10 部影片的 `mtg-v3`。全部完成后，点击该批次的「用 X-MLClass 回放对比」。B 直接读取 A 保存的影片内容、片序、初始词表和标签上限，不重新查询现有资产或标签库。生成模型、温度、嵌入模型及独立评估口径必须一致，否则拒绝回放。
4. 查看两组指标、展开影片查看 B 的词表增长及联合盲评的独有概念、证据和理由。完整快照可导出，包含初始词表、每场景打分、原词、支持影片、同义判断、扩词记录、最终实验词表和待积累候选。

也可以选择「在线 X-MLClass」直接创建独立批次。这种方式使用创建时的内容和词表，不自动产生 A/B 对比。

API：

```text
POST /api/v1/benchmarks
{"algorithm":"x-mlclass-online-v1","asset_ids":["film1","film2"],"max_tags":15}

POST /api/v1/benchmarks/{completed_mtg_report_id}/replay-xmlclass
```

旧的创建请求省略 `algorithm` 时仍运行 `mtg-v3`。

## 算法与论文的对应

- 用初始标签库里的有效标签直接初始化实验词表，保留原 ID、释义和别名；跳过论文的 GMM 初始化。
- 将场景描述、视觉特征、台词、事件、实体、氛围等已有识别文本作为前提，不把片名、场景 ID 或时间戳作为支持内容。仅 ID/时间的空场景不参与发现。B 不再次调用视觉模型。
- 用现有嵌入器召回每场景前 8 个标签，NLI 对每个“场景、标签假设”独立打分。支持分为该对的 entailment logit 在关系类别上的 softmax 概率；包含 neutral，不在候选标签之间归一化。增加候选不会改变原有文本对的支持分，但召回集合仍可能变化。
- 对最高支持分小于 0.5 的可用场景，从低到高最多选择 5 个，每场景独立提取最多 3 个关键词。提示词不包含现有词表；要求保留原词和可核对的连续原文。
- 原词做 Unicode NFKC、大小写和标点归一化；先查工作词表，再与候选池代表词比较。向量只产生同义判断的短名单，必须经过 LLM 确认同义才合并；相关或上下位不合并。候选池仅做代表词级归并，不做全局聚类。
- 支持数按不同 `asset_id` 计，同片多个场景、重复处理同片都只贡献一部。默认至少 2 部影片支持且通过工作词表查重才入表。未达标者在同一批次后续影片中累积。入表记录及来源保存在发生入表的影片审计中。
- 本片发生入表才重跑一次场景召回/NLI。按场景排名逐列投票，票数并列按最高支持分、稳定标签键排序；最高票为一票时仅取该列最高分项；跳过已输出标签。投票前排除支持分小于 0.7 的标签，并保留全部支持场景及时间区间。
- B 的输出使用与 A 相同的 `ProducedTag` 结构，由基准统一截取前 K 条。`confidence` 记录最大场景支持分，不解释为影片级校准概率。

这是设计文档定义的在线变体：只对低覆盖场景即时提词，最终投票只使用场景文本；不执行论文的全语料预提词、关键词第二路投票、频率大于 15 次/低于 1% 筛选或高频标签暂存。论文复现细节见 [论文分析](x-mlclass-paper-analysis.md)。默认阈值是首轮实验参数，应根据中文影视数据验证。

NLI 当前按 tokenizer 限制到 512 token，超长场景截断前提并保留标签假设；审计仍保留完整场景。长场景尾部可能漏标，这是本版限制。NLI 检查点必须提供明确的 `entailment` 标签映射，不猜测 `LABEL_0` 等编号。

## 状态、失败与评估

每批拥有独立实验状态，随批次 JSON 原子保存于 `mtg_report_dir/benchmarks`；同批按固定片序执行。后片继承前片成功生成的词表和候选池。新批次从初始词表重新开始，暂不跨批续跑，以免并行实验互相污染。

每片在状态副本上执行：提词、同义判断或 NLI 失败，不提交本片部分更新，保存已有审计并继续下一片。生成完成后先保存词表及标签，因此独立评估失败不会撤回成功生成的状态。任一影片失败则批次标失败。服务重启时沿用原基准的中断标记机制，保留已落盘状态；重新创建批次从头回放，不承诺断点恢复。

单组评估沿用固定 `glm-5.3-flash` 和 `evidence-concept-v2`：主要比较每片有证据独立概念数和证据不支持比例。回放另做一次联合盲评，将两组标签打乱，仅提供标签名和场景证据，模型看不到来源、生成置信度或内部判决。通过联合概念归并列出两侧独有标签，包含不支持标签及理由。独有概念表示模型判断的语义差异，不是真实召回率。

提词与同义判断的 LLM token、生成总耗时、独立评估成本及联合对比成本分开记录。NLI 是本地计算，以判断文本对数和包含在生成中的耗时呈现，不折算成 LLM token；嵌入计算亦不计 token。服务未返回 usage 时显示未知，不伪造为零。失败生成的部分调用可在审计中查看，不作为完整生成成本。

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
| `XMLCLASS_MIN_SUPPORT_DOCUMENTS` | 2 | 入表所需不同影片数 |
| `XMLCLASS_SYNONYM_COSINE` | 0.75 | 同义判断短名单阈值 |
| `XMLCLASS_SYNONYM_TOP_K` | 3 | 每个词最多复核的代表词数 |

NLI 模型使用与标签映射依据[作者模型卡](https://huggingface.co/MoritzLaurer/mDeBERTa-v3-base-mnli-xnli)；排名投票参考 [X-MLClass 作者实现](https://github.com/Kaylee0501/X-MLClass/blob/main/OpenWordMLTC/zero-shot/model_eval.py)。
