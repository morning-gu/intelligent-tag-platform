"""确定性 Mock LLM 客户端：测试与冒烟用，零网络、零随机源。

与阶段 1 的 MockEmbedder 对应：无 random 模块、跨进程确定，全部行为由
sha256 哈希驱动。按 prompt 内容识别两种模式（与任务 8 runner 的组装咬合）：
- 模式 A（封闭提取）：prompt 含 `## 候选标签` 节（ClosedTagProducer
  逐组再分批插入的候选标签块）→ 解析块内标签行（tag_id 从
  `- tag_id：... 标签名称：...` 行提取），按 sha256(run_id + tag_id) 以 ~40% 概率选中，输出 LLM 契约的
  三键（tag_id/applicability/evidence，v8 起 evidence 为 content/scene/
  frame 三模态嵌套节）；
- 模式 B（开放生成）：固定返回 3 个候选（多线叙事/情感张力/视听风格），
  不含 tag_id（模式 B 的 tag_id 由匹配层赋，LLM 不产出）。

场景证据（Task 6）：两模式均解析 prompt 中的场景行（装配层
assemble._render_content_text 组装的 `- {scene_id} [{start_ms}-{end_ms}] {description}`），
每个输出标签按 sha256(evidence|{run_id}|{tag_id 或 tag_name}) 确定性选一条
场景行并将其描述作为 evidence 摘要——时间区间由 producer 从内容中补齐；
演示/冒烟模式下证据链完整、evidence_coverage 非零；prompt 无场景行
（如 title-only 空内容测试）时兜底为空列表。

哈希风格与 MockEmbedder 一致（前 8 hex / 0x100000000）；选中、打分与证据
选取用不同哈希键。
"""
from __future__ import annotations

import hashlib
import json
import re

from tagplatform.capabilities.llm.client import LLMResponse

# 场景行格式（装配层 assemble._render_content_text 组装，逐字对齐）：
# `- {scene_id} [{start_ms}-{end_ms}] {description}`；scene_id 取 \S+（候选
# 行首词后跟 " |" 而非 " ["，两类行格式天然互斥，互不误配）
_SCENE_LINE_RE = re.compile(r"^- (\S+) \[(\d+)-(\d+)\] (.+)$", re.MULTILINE)
# 模式 A 标志节（ClosedTagProducer.produce 逐组分批插入候选标签块的标题）
_CLOSED_MARKER = "## 候选标签"
# 模式 A 候选标签行：`- tag_id：{id} 标签名称：{name}`（tag_block 渲染，
# 全角冒号，与场景行/审核行格式互斥）
_CLOSED_TAG_RE = re.compile(r"^- tag_id：(\S+) 标签名称：(.+)$", re.MULTILINE)
# 开放式生成（mtg 流水线）的标志串：生成 / 审核两种请求靠它分流
_MTG_GEN_MARKER = "## 标签生成任务"
_MTG_JUDGE_MARKER = "## 审核任务"
# 相似度复核请求（services/generation/open/mtg/verify.py）：行格式同为全角竖线三段
_MTG_VERIFY_MARKER = "## 相似度复核"
# 标签描述补写请求（services/generation/open/mtg/descriptions.py）：行格式
# `- {tag}（归属目录：...）`，与生成/审核请求互斥
_MTG_DESC_MARKER = "## 通用释义"
# 证据核验请求（services/generation/closed/confidence.py）：CLOSED_TEMPLATE
# 正文 + 五级核验表；必须在 _CLOSED_MARKER 之前分流（核验 prompt 含场景行
# 无组 JSON，掉进模式 A 会破坏确定性）
_CLOSED_VERIFY_MARKER = "## 证据核验"
# 审核候选行格式：`- {tag}｜{dimension}｜{reason}`（全角竖线，与场景行互斥）
_JUDGE_LINE_RE = re.compile(r"^- (.+?)｜(.+?)｜(.*)$", re.MULTILINE)
# 选中概率阈值（~40%）
_SELECT_PROB = 0.4

# 开放生成固定三候选：名称 / 维度（中文五维名）/ 通用释义（desc 字段，
# 与真实模型一样"只解释这个词本身"，不掺本片剧情）
_OPEN_SEEDS = [
    ("多线叙事", "叙事", "以多条时间线并行推进的叙事结构"),
    ("情感张力", "情绪", "以人物关系中的紧张与拉扯营造的情绪强度"),
    ("视听风格", "风格", "以构图、色彩与剪辑形成的整体视听特征"),
]


def _extract_text(messages: list[dict]) -> str:
    """拼接消息中的全部文本（多模态 content parts；图像 part 无 text 键自然跳过）。"""
    parts = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts.extend(p.get("text", "") for p in c if isinstance(p, dict))
    return "\n".join(parts)


def _hash_unit(key: str) -> float:
    """哈希 → 确定性 [0,1) 均匀值（sha256 前 8 hex，与 MockEmbedder 同风格）。"""
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(h[:8], 16) / 0x1_0000_0000


def _score_five_point(key: str) -> int:
    """哈希 → 4 或 5 分，确保 mock 闭环结果通过适用性门槛。"""
    return 4 + int(_hash_unit(key) * 2)


def _parse_scenes(text: str) -> list[dict]:
    """解析 prompt 中的全部场景行（无场景行 → []，title-only 兜底的前置）。

    行格式由装配层 assemble._render_content_text 组装（逐字对齐），时间戳还原为 int。"""
    return [{"scene_id": sid, "start_ms": int(start), "end_ms": int(end),
             "description": desc}
            for sid, start, end, desc in _SCENE_LINE_RE.findall(text)]


def _pick_evidence(scenes: list[dict], hash_key: str) -> list[dict]:
    """按 sha256(哈希键) 确定性选一条场景行作一元素证据卡（无场景行 → []）。

    哈希键 `evidence|{run_id}|{tag_id 或 tag_name}`（与选中/打分哈希键区分，
    互不耦合）；前 8 hex → [0,1) 均匀值按场景数分桶定位（[0,1) 严格小于 1，
    桶下标恒落在合法区间）。description 定为场景描述（Task 6 拍板）。
    """
    if not scenes:
        return []
    scene = scenes[int(_hash_unit(hash_key) * len(scenes))]
    return [{"scene_id": scene["scene_id"], "start_ms": scene["start_ms"],
             "end_ms": scene["end_ms"], "description": scene["description"]}]


def _closed_verify_output(text: str, messages: list[dict]) -> dict:
    """closed Cver 核验（v2 模态两维）：确定性三模态判定输出。

    分流前提：_CLOSED_VERIFY_MARKER 在 _CLOSED_MARKER 之前判（"## 证据
    核验" 节只出现在核验消息中）。输出新契约 modalities JSON：
    - content 恒 relevant（整体资产文本必在 prompt 中）；
    - scene 按场景行有无（_SCENE_LINE_RE 命中即相关）；
    - frame 按 messages 是否带图片 part（核验消息多模态时才有）；
    分值哈希采样 4/5（与旧口径等值，_score_five_point 确定性）。
    """
    content_part = messages[0]["content"]
    has_frame_parts = (isinstance(content_part, list)
                       and any(part.get("type") == "image_url"
                               for part in content_part
                               if isinstance(part, dict)))
    modalities: dict[str, dict] = {
        "content": {"r": 1, "sufficiency": "充分",
                    "t": _score_five_point("verify|content|" + text[:64]),
                    "v": _score_five_point("verify|v|content|" + text[:64])},
    }
    if _SCENE_LINE_RE.search(text):
        modalities["scene"] = {
            "r": 1, "sufficiency": "支持",
            "t": _score_five_point("verify|scene|" + text[:64]),
            "v": _score_five_point("verify|v|scene|" + text[:64])}
    if has_frame_parts:
        modalities["frame"] = {
            "r": 1, "sufficiency": "辅助",
            "t": _score_five_point("verify|frame|" + text[:64]),
            "v": _score_five_point("verify|v|frame|" + text[:64])}
    return {"modalities": modalities}


def _closed_output(text: str, run_id: str) -> list[dict]:
    """模式 A：解析候选标签块，哈希采样选中项（确定性伪判定），逐标签附三模态证据。

    ClosedTagProducer 按目录分组、组内再分批，以 `## 候选标签` 节插入
    渲染后正文之前（tag_block 文本块，非 JSON）；一批一次调用，本函数
    单次只解析一个块——块内标签行即该批候选。场景行在
    content 节（候选块之后），对全文解析即可。v8 多模态融合：输出嵌套
    evidence 对象，content/scene/frame 三模态节各含 evidence_strength 与
    evidences（哈希键带模态维度，三模态打分/取证据互不耦合）。
    """
    # 候选块从标志节起到「## 影视内容」节前截止；关键帧 text part 在其后
    # （_extract_text 拼接全部文本 part），不影响块内行解析
    section = text.split(_CLOSED_MARKER, 1)[1].split("\n\n## 影视内容", 1)[0]
    scenes = _parse_scenes(text)
    items = []
    for tag_id, _tag_name in _CLOSED_TAG_RE.findall(section):
        if _hash_unit(run_id + tag_id) >= _SELECT_PROB:
            continue
        evidence: dict[str, dict] = {}
        for modality in ("content", "scene", "frame"):
            picked = _pick_evidence(
                scenes, f"evidence|{modality}|{run_id}|{tag_id}")
            evidence[modality] = {
                "evidence_strength": _score_five_point(
                    f"evidence_strength|{modality}|{run_id}|{tag_id}"),
                "evidences": [
                    {"scene_id": item["scene_id"],
                     "evidence": item["description"]}
                    for item in picked
                ],
            }
        items.append({
            "tag_id": tag_id,
            "applicability": _score_five_point(
                f"applicability|{run_id}|{tag_id}"),
            "evidence": evidence,
        })
    return items


def _open_output(text: str, run_id: str) -> list[dict]:
    """模拟单场或全片候选，使用对应数量的场景证据。"""
    scene_ids = [scene["scene_id"] for scene in _parse_scenes(text)]
    round_id = run_id.rsplit(":", 1)[-1]
    scope = "film" if round_id == "film" or round_id.startswith("film-dir-") else "scene"
    if not scene_ids:
        scene_ids = re.findall(r"^场景 ID：(.+)$", text, re.MULTILINE)
    if scope == "film" and not scene_ids:
        scene_ids = re.findall(r'"scene_id": "([^"]+)"', text)
    evidence = scene_ids[:2] if scope == "film" else scene_ids[:1]
    directories = re.findall(r"^- ([^｜\n]+)｜[^\n]+$", text, re.MULTILINE)
    target = {"dir": directories[0]} if len(directories) == 1 else {}
    return [
        {"tag": name, "dimension": dimension, "scenes": evidence, "scope": scope,
         "desc": desc, "reason": f"模拟依据 #{index + 1}", **target}
        for index, (name, dimension, desc) in enumerate(_OPEN_SEEDS)
    ]


# 补写请求的候选行：`- {tag}（归属目录：{path}）`
_DESC_LINE_RE = re.compile(r"^- (.+?)（归属目录：(.+?)）$", re.MULTILINE)


def _desc_output(text: str) -> dict:
    """通用释义补写：按清单里的标签名回一条"以…为特征"的通用定义。"""
    items = []
    for tag, _dir in _DESC_LINE_RE.findall(text):
        name = tag.strip()
        items.append({"tag": name, "desc": f"以{name}为核心特征的表现方式"})
    return {"items": items}


def _verify_output(text: str) -> dict:
    """相似度复核：候选与库标签字面相同才算同一概念（确定性伪判定）。

    真实模型的判据是语义；mock 只按字面相等给 true，保证测试里
    "只有同名才替换"这条路径可预期。
    """
    results = [
        {"candidate": candidate.strip(),
         "same": candidate.strip() == library.strip()}
        for candidate, library, _cos in _JUDGE_LINE_RE.findall(text)
    ]
    return {"results": results}


def _judge_output(text: str, run_id: str) -> list[dict]:
    """judge：解析候选行，按融合判决协议回过线三维分（评审归一 Task 6）。

    条目形状与共享 VERDICT_OUTPUT_SPEC 一致：id 键（有候选 ID 回候选 ID，
    否则回标签名）+ relevance/semantics 与 value 三方向各 {"score","reason"}
    + quote/scenes/confidence；tag 字段同时保留（名称兜底回配用）。必须给
    quote：引擎证据核验要求"评分引用内容原文"，模拟客户端从所报依据场景
    的描述里截一段；film 级候选至少报两个场景（与提示词同规）。目录匹配
    已融合进 semantics，不再输出 dir_fits；是否保留由程序复算。
    """
    scenes = _parse_scenes(text)
    scene_id = scenes[0]["scene_id"] if scenes else None
    quote = scenes[0]["description"][:12] if scenes else ""
    items = []
    candidates = text.partition("## 候选标签\n")[2].partition("## 影片内容\n")[0]
    for tag, _dimension, reason in _JUDGE_LINE_RE.findall(candidates):
        marks = reason.partition("〕")[0] if reason.startswith("〔") else ""
        source = re.search(r"｜依据场景 ([^｜]+)", marks)
        evidence = source.group(1).split("、") if source else ([scene_id] if scene_id else [])
        candidate_id = re.search(r"(?:〔|｜)候选 ID ([^｜]+)", marks)
        cited = next((scene for scene in scenes if scene["scene_id"] in evidence), None)
        items.append({
            "id": candidate_id[1] if candidate_id else tag.strip(),
            "tag": tag.strip(),
            "relevance": {"score": 4, "reason": "模拟内容评分"},
            "semantics": {"score": 3, "reason": "模拟语义评分"},
            "value": {
                "presentation": {"score": 3, "reason": "模拟内容呈现评分"},
                "search": {"score": 3, "reason": "模拟内容搜索评分"},
                "recommendation": {"score": 3, "reason": "模拟内容推荐评分"},
            },
            "confidence": 0.9,
            "scenes": evidence,
            "quote": cited["description"][:12] if cited else quote,
        })
    return items


def _quality_output(text: str) -> dict:
    """评审：解析待评估标签块，按融合判决协议回三维分（评审归一 Task 7）。

    条目形状与共享 VERDICT_OUTPUT_SPEC 一致：id 键 + relevance/semantics 与
    value 三方向各 {"score","reason"} + quote/scenes/confidence。有场景行时
    relevance 给 4 并引用全部场景（quote 取首场景描述前 12 字，可被证据核验
    逐字命中）；无场景行时 relevance 置 null（无法判断），value 三方向给
    3 分（协议不收 null）。是否保留由程序按分数复算，mock 不输出 pass。
    """
    material, _, label_json = text.rpartition("\n\n## 待评估标签\n")
    labels = json.loads(label_json)
    scenes = _parse_scenes(material)
    quote = scenes[0]["description"][:12] if scenes else ""
    rows = [{
        "id": label["id"],
        "relevance": {"score": 4 if scenes else None, "reason": "模拟内容评分"},
        "semantics": {"score": 4 if label["desc"] else None, "reason": "模拟语义评分"},
        "value": {
            "presentation": {"score": 4 if scenes else 3, "reason": "模拟内容呈现评分"},
            "search": {"score": 4 if scenes else 3, "reason": "模拟内容搜索评分"},
            "recommendation": {"score": 4 if scenes else 3, "reason": "模拟内容推荐评分"},
        },
        "quote": quote,
        "scenes": [scene["scene_id"] for scene in scenes],
        "confidence": 0.9,
    } for label in labels]
    return {"verdicts": rows}


class MockLLMClient:
    """确定性伪 LLM：哈希即输出，测试与 mock 配置专用。

    能力位 `zero_network = True`：mtg 管线构造 judge/verify 阶段客户端时按
    能力探测（getattr，而非 isinstance 本类）决定"复用传入客户端还是按阶段
    参数另建真实客户端"——管线因此不 import 测试替身的类型，任何声明该
    能力位的零网络客户端都适配。
    """

    zero_network = True

    def __init__(self, model: str = "mock-1"):
        self.model = model

    def complete(self, messages: list[dict], run_id: str) -> LLMResponse:
        text = _extract_text(messages)
        if str(messages[0]["content"]).startswith("## 标签质量评估"):
            out = _quality_output(text)
        elif _MTG_JUDGE_MARKER in text:
            out = _judge_output(text, run_id)
        elif _MTG_VERIFY_MARKER in text:
            out = _verify_output(text)
        elif _MTG_DESC_MARKER in text:
            out = _desc_output(text)
        elif _CLOSED_VERIFY_MARKER in text:
            out = _closed_verify_output(text, messages)
        elif _CLOSED_MARKER in text:
            out = _closed_output(text, run_id)
        else:
            out = _open_output(text, run_id)
        content = json.dumps(out, ensure_ascii=False)
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        # raw 保留 OpenAI 信封形状（choices[0].message.content），与真实
        # 客户端成功响应结构一致，便于上层统一处理与调试对账
        return LLMResponse(content=content, usage=usage,
                           raw={"choices": [{"message": {"content": content},
                                             "finish_reason": "stop"}],
                                "usage": usage, "mock": True},
                           ok=True)
