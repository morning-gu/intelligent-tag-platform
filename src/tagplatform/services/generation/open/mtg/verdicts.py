"""生产 Judge 与开放评测评审共用的判决域/解析/证据核验/批编排引擎。

已落：判决域（VERDICT_FLOORS/verdict_pass/VerdictTarget，Task 1）、融合
解析器（parse_verdicts，Task 2——两套容错家族取并集）、证据核验终判
（finalize_verdict，Task 3）与批编排引擎（run_verdicts + VerdictPolicy，
Task 4——两套旧控制循环收敛为一个队列驱动循环）。

分层红线：本模块不得 import fastapi / sqlalchemy；eval 侧 import 本模块属于
既有先例方向（`open_review.py` import `mtg.prompts` 已开此口）。
"""
from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass

from tagplatform.capabilities.llm.usage import accumulate_usage
from tagplatform.services.generation.open.mtg.normalization import normalize_key
from tagplatform.services.generation.open.mtg.parse import (
    MTGParseError,
    extract_json_array,
)
from tagplatform.services.tags.value_scoring import (
    VALUE_DIRECTIONS,
    VALUE_DIRECTION_ALIASES,
    VALUE_DIRECTION_LABELS,
    aggregate_value_score,
    normalize_tag_kind,
    weights_dict_for_kind,
)

# 三轴通过下限（评审归一拍板值，逐字）：relevance/semantics 楼层 3、value 楼层 2。
# 生产 `_filter_candidates` 与评测报告 `_group_stats` 共用同一常量复算——
# 通过率数字同源，不再各自硬编码。
VERDICT_FLOORS: dict[str, int] = {"relevance": 3, "semantics": 3, "value": 2}


def verdict_pass(relevance: int | float | None, semantics: int | float | None,
                 value: int | float | None) -> bool | None:
    """统一通过判定（裁决复算，两侧同源）：三轴全部达到楼层才算过线。

    null 纪律（None≠0）：任一轴 null → 返回 None——评测侧不进 judged 分母，
    生产侧按未过线淘汰（与现状 null 淘汰口径一致）；value 为聚合后的总分
    （保留两位小数的浮点），楼层按 >=2 判定。
    """
    if relevance is None or semantics is None or value is None:
        return None
    return (relevance >= VERDICT_FLOORS["relevance"]
            and semantics >= VERDICT_FLOORS["semantics"]
            and value >= VERDICT_FLOORS["value"])


@dataclass(frozen=True)
class VerdictTarget:
    """一次判决的完整目标描述——judge 候选与评审 tag_rows 的公共投影。

    后续解析/核验/批编排都以本类型为准：
    - key：引擎内部主键（judge=candidate_id，评审=t{index}），判决条目按它回配；
    - tag：标签名，match_by_name 兜底配对用（归一化精确 → 唯一前缀）；
    - level："film"|"scene"——决定证据核验的场景计数下限（film≥2 / scene≥1）；
    - allowed_scenes：允许引用的场景 id 集合（film 级=全部场景；scene 级=
      来源场景 ∩ 全部），核验按它过滤 scenes 并校验 quote 落点；
    - tag_kind：标签种类（content/scene/character），决定 value 三方向权重；
    - has_desc：有无释义——评审侧 semantics 缺释义置 null 规则的开关输入；
    - id_aliases：模型可能回的 id 变体（如 key "t3" 回成 "3"/3），先按 key
      精确匹配、再按别名，最后才走名称兜底。
    """
    key: str
    tag: str
    level: str  # "film" | "scene"
    allowed_scenes: frozenset[str]
    tag_kind: str
    has_desc: bool
    id_aliases: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 融合解析器（评审归一 Task 2）：mtg.parse.parse_judge_items 与
# eval/open_review._parse_reviews 的容错家族取并集，输出统一条目形状。
# ---------------------------------------------------------------------------

# 三维分值清洗的协议错误哨兵：与「显式 null」（模型申明无法判断）严格区分
# （mtg.parse._MISSING / open_review._MISSING 同语义，本地实现不跨模块引私有）。
_MISSING = object()

# 标量评分轴：接受裸 0-4 整数 / {"score","reason"} 对象 / 显式 null。
_VERDICT_AXES = ("relevance", "semantics")

# 包装键族（open_review._parse_reviews 同族）：verdicts/reviews 支持列表与
# 按 id 索引对象两种形状，tags 仅列表（rubric 原输出形状）。
_WRAPPER_LIST_KEYS = ("verdicts", "reviews", "tags")


def _clean_tag(value) -> str:
    """判决里的标签名（mtg.parse._clean_tag 同款截断）：按分隔符截断、≤40 字。"""
    text = str(value or "").strip()
    for separator in ("｜", "|"):
        if separator in text:
            text = text.split(separator, 1)[0]
    return text.strip()[:40]


def _clamp_confidence(value) -> float:
    """置信度夹到 [0,1]；非数值按 0 处理（mtg.parse._clamp_confidence 同款）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, number))


def _clean_dimension(value) -> tuple:
    """维度值 → (score, reason)；形状非法返回 (_MISSING, "")。

    容忍 {"score","reason"} 对象与裸分值两种形状（open_review._clean_dimension
    同款）；score 只认 0-4 整数（含整数值浮点）或显式 null，布尔/越界/缺
    score 键/非数值都是协议错误。null 是否合法由调用方按轴区分
    （relevance/semantics 允许 = 无法判断；value 三方向不允许 = 协议错误）。
    """
    if isinstance(value, dict):
        if "score" not in value:
            return _MISSING, ""
        score = value["score"]
        reason = value.get("reason")
        reason = reason.strip() if isinstance(reason, str) else ""
    else:
        score, reason = value, ""
    if score is None:
        return None, reason
    if isinstance(score, bool):
        return _MISSING, ""
    if isinstance(score, float) and score.is_integer():
        score = int(score)
    if isinstance(score, int) and 0 <= score <= 4:
        return score, reason
    return _MISSING, ""


def _clean_value_block(entry: dict) -> tuple[dict | None, list[str]]:
    """解析 value 三方向分（presentation/search/recommendation，均 0-4 整数）。

    新协议 `value` 优先，旧 judge `value_scores` 兜底（与 parse_judge_items
    的 value_scores 优先相反；仅畸形双键并存的响应才有差异）；中文方向键经
    VALUE_DIRECTION_ALIASES 归一。方向分接受裸整数或 {score, reason} 对象；
    null / 漏方向 / 布尔 / 越界一律协议错误（v13 纪律：value 不接受
    「无法判断」，绝不与 relevance 的 null 混同）。
    """
    raw = entry.get("value")
    if not isinstance(raw, dict):
        raw = entry.get("value_scores")
    if not isinstance(raw, dict):
        if "value" not in entry and "value_scores" not in entry:
            return None, ["value 漏字段"]
        return None, [f"value 必须是三方向分值对象（收到 {raw!r}）"]
    normalized: dict[str, object] = {}
    for key, val in raw.items():
        canonical = (VALUE_DIRECTION_ALIASES.get(str(key).strip().lower())
                     or VALUE_DIRECTION_ALIASES.get(str(key).strip()))
        if canonical and canonical not in normalized:
            normalized[canonical] = val
    dims: dict[str, dict] = {}
    errors: list[str] = []
    for direction in VALUE_DIRECTIONS:
        if direction not in normalized:
            errors.append(f"value.{direction} 漏方向")
            continue
        score, reason = _clean_dimension(normalized[direction])
        if score is _MISSING:
            errors.append(f"value.{direction} 非法值 {normalized[direction]!r}")
        elif score is None:
            errors.append(f"value.{direction} 不接受 null")
        else:
            dims[direction] = {"score": score, "reason": reason}
    if errors:
        return None, errors
    return dims, []


def _flatten_wrapper(data) -> list | None:
    """包装 dict / 裸列表 → 条目列表；不是已知包装形状返回 None（交兜底）。

    verdicts/reviews 为按 id 索引对象时展平（键即 id，open_review 同款）；
    列表形状原样返回，非对象条目由主循环记协议错误（judge 严格侧）。
    """
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return None
    for key in _WRAPPER_LIST_KEYS:
        inner = data.get(key)
        if isinstance(inner, list):
            return inner
        if isinstance(inner, dict) and key != "tags":
            return [{"id": item_id, **value} for item_id, value in inner.items()
                    if isinstance(value, dict)]
    return None


def _loads_verdict_payload(text: str) -> list:
    """取判决条目数组：先直读 JSON（包装 dict 在本地展平）；失败按首尾大
    括号抠对象再展平（open_review._loads_with_prose_fallback 同款——prose
    包裹的 {"reviews": {"t0": {...}}} 不失守，extract_json_array 只会抠
    [...] 抠不了 {...}），仍失败走 extract_json_array 兜底（```json 围栏 /
    前后 prose / 嵌套列表）。"""
    try:
        data = json.loads((text or "").strip())
    except json.JSONDecodeError:
        raw = (text or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                pass
            else:
                flattened = _flatten_wrapper(data)
                if flattened is not None:
                    return flattened
        return extract_json_array(text)
    flattened = _flatten_wrapper(data)
    if flattened is not None:
        return flattened
    if isinstance(data, dict):
        # 无已知包装键的 dict（单判决对象 / 标签名为键映射）交给
        # extract_json_array 的容错二/三处理。
        return extract_json_array(text)
    raise MTGParseError(f"输出不是数组（{type(data).__name__}）")


def _entry_id(entry: dict) -> str | None:
    """条目 id 字符串化：新协议 "id" 优先，旧 judge "candidate_id" 兜底；
    缺失 / 空白 → None（回配交引擎按别名与名称兜底）。"""
    raw = entry.get("id")
    if raw is None:
        raw = entry.get("candidate_id")
    if raw is None:
        return None
    return str(raw).strip() or None


def _parse_entry(entry: dict) -> tuple[dict, list[str]]:
    """单条判决 → (条目, 协议错误列表)；错误非空时条目整体不可用（salvage）。

    条目键：id（字符串化）/ tag（同款截断，可空）/ relevance、semantics
    （{"score","reason"}，null 合法）/ value（三方向 {"score","reason"}）/
    quote（≤60 字，缺省 ""）/ scenes（缺省 []，非字符串数组=协议错误）/
    confidence（夹 [0,1]，缺省 None）。漏 relevance/semantics 键与分值非法
    是协议错误（judge 严格侧），漏可选键给缺省值（评审宽松侧）。
    """
    item = {
        "id": _entry_id(entry),
        "tag": _clean_tag(entry.get("tag") or entry.get("tag_name")),
    }
    broken: list[str] = []
    axes: dict[str, dict] = {}
    for axis in _VERDICT_AXES:
        if axis not in entry:
            broken.append(f"{axis} 漏字段")
            continue
        score, reason = _clean_dimension(entry[axis])
        if score is _MISSING:
            broken.append(f"{axis} 非法值 {entry[axis]!r}")
        else:
            axes[axis] = {"score": score, "reason": reason}
    value, value_errors = _clean_value_block(entry)
    broken.extend(value_errors)
    raw_scenes = entry.get("scenes")
    if raw_scenes is not None and (not isinstance(raw_scenes, list)
                                   or any(not isinstance(s, str) for s in raw_scenes)):
        broken.append("scenes 必须是场景 ID 数组")
    if broken:
        return item, broken
    raw_confidence = entry.get("confidence")
    item.update({
        **axes,
        "value": value,
        "quote": str(entry.get("quote") or "").strip()[:60],
        "scenes": [s.strip() for s in raw_scenes or []
                   if isinstance(s, str) and s.strip()],
        "confidence": None if raw_confidence is None else _clamp_confidence(raw_confidence),
    })
    return item, []


def parse_verdicts(text: str) -> list[dict]:
    """融合解析器：生产 judge 与开放评测评审共用一把解析尺（Task 2）。

    输入容错取两套家族并集：```json 围栏与前后 prose、{"verdicts"/"reviews":
    [...]}、按 id 索引对象（{"reviews": {"t0": {...}}} 展平，键即 id）、
    {"tags": [...]}、裸列表、单判决对象与标签名为键映射（extract_json_array
    兜底）。重复 id 的条目整对剔除——镜像评审规则「重复即不可靠」，不是
    首条生效。

    任一条目违协议（漏轴键 / 分值非法 / value 方向 null 或漏方向 / scenes
    非字符串数组）→ MTGParseError(message, items=同批合格条目)（salvage
    协议：一条坏判决不让整片产出归零，上层决定重试或只保住可用判决）；
    全部剔除 / 空数组 → 空数组错误。解析层不猜分数、不把「没按协议给分」
    伪装成「无法判断」——显式 null 仅 relevance/semantics 合法。
    """
    items: list[dict] = []
    problems: list[str] = []
    seen_ids: set[str] = set()
    for entry in _loads_verdict_payload(text):
        if not isinstance(entry, dict):
            problems.append("判决条目必须是对象")
            continue
        item_id = _entry_id(entry)
        if item_id is not None:
            if item_id in seen_ids:
                # 重复 id 不可靠：已收条目一并剔除，两条都不进结果。
                items = [it for it in items if it["id"] != item_id]
                continue
            seen_ids.add(item_id)
        item, broken = _parse_entry(entry)
        if broken:
            label = item["tag"] or item_id or "条目"
            problems.append(f"{label}：" + "、".join(broken))
            continue
        items.append(item)
    if problems:
        raise MTGParseError("判决不合协议（" + "；".join(problems[:5]) + "）",
                            items=items)
    if not items:
        raise MTGParseError("审核输出为空数组")
    return items


# ---------------------------------------------------------------------------
# 证据核验与终判（评审归一 Task 3）：原生产 `_require_quotes`（Task 6 已删其
# pipeline 副本，judge 经引擎走本实现）与评审 open_review.py 200–241 行后处理
# （Task 7 收敛）的统一实现——quote 逐字核验 + 场景计数 + 缺释义置 null +
# value 三方向聚合，产出两侧共用的终判 dict。
# ---------------------------------------------------------------------------

# quote 核验三件套从 pipeline.py 原样搬移（评审归一 Task 3）；Task 6 已删
# pipeline 侧副本，judge 侧归一化文本构建改 import 本模块，两侧收敛完成。
_QUOTE_MIN_CHARS = 8
_QUOTE_STRIP_RE = re.compile(
    r"[\s「」『』“”‘’\"'…—·,，。、；;：:！!？?（）()\[\]【】]+")


def _normalize_quote(text: str) -> str:
    return _QUOTE_STRIP_RE.sub("", text or "")


def _quote_ok(quote: str | None, haystack: str) -> bool:
    """quote 与内容有 ≥8 字连续重合即视为有效引用。"""
    normalized = _normalize_quote(str(quote or ""))
    if len(normalized) < _QUOTE_MIN_CHARS:
        return False
    for start in range(len(normalized) - _QUOTE_MIN_CHARS + 1):
        if normalized[start:start + _QUOTE_MIN_CHARS] in haystack:
            return True
    return False


def _summarize_value_reasons(dims: dict[str, dict]) -> str:
    """将三个价值方向的得分与理由汇总为单句可读文案。

    从 open_review.py 原样搬移（评审归一 Task 3）——与 open_review 副本暂时
    并存，Task 7 删 open_review 侧副本，届时收敛。
    """
    parts = []
    for direction in VALUE_DIRECTIONS:
        entry = dims[direction]
        label = VALUE_DIRECTION_LABELS[direction]
        score = "null" if entry["score"] is None else str(entry["score"])
        reason = entry.get("reason") or ""
        parts.append(f"{label}({score})：{reason}" if reason else f"{label}({score})")
    return "；".join(parts)


def _axis_reason(label: str, entry: dict) -> str:
    """单轴简要文案（与 _summarize_value_reasons 同款格式：标签(分)：理由）。"""
    score = "null" if entry["score"] is None else str(entry["score"])
    reason = entry.get("reason") or ""
    return f"{label}({score})：{reason}" if reason else f"{label}({score})"


# 核验失败文案（拍板口径）：quote 失败含「未引用可核对原文」、场景计数失败
# 含「引用场景不足」；写入 relevance.reason 并同步为顶层派生 reason。
_QUOTE_FAIL_REASON = "未引用可核对原文，相关性高分缺少逐字依据"
_SCENES_FAIL_REASON = "有效引用场景不足，无法核验所给相关性高分"

# 有效场景计数下限（open_review 同款映射；level 恒为 film/scene，缺省 1 兜底）。
_SCENE_MIN_COUNTS = {"film": 2, "scene": 1}


def finalize_verdict(item: dict, target: VerdictTarget, *, haystack: str,
                     scene_texts: Mapping[str, str],
                     value_weights: Mapping | None = None,
                     semantics_null_without_desc: bool = False) -> dict:
    """单条判决的证据核验与终判（评审归一 Task 3）：生产 judge 与评审共用。

    规则序：① scenes 求有效集（item.scenes ∩ scene_texts 键集，scene 级再
    ∩ allowed_scenes，排序输出）；② relevance 为 int 且 ≥3 时核验证据——
    quote 须在 allowed 场景文本（空则退回全文 haystack）中 ≥8 字连续重合，
    且有效场景计数达下限（film≥2 / scene≥1；scene 级 allowed_scenes 为空集
    时计数免查、quote 核验退回全文 haystack），任一失败 relevance 压 1 并写
    核验失败说明 + verify_fail（quote 先查、失败即压，优先于 scenes）；
    ③ semantics 缺释义规则（仅 semantics_null_without_desc 开且 has_desc=
    False 且分为 int ≥2 时置 {"score": None, "reason": "缺少释义"}；null=
    无法判断不在此列）；④ value 三方向按标签种类权重聚合（aggregate_value_
    score，保留两位）；⑤ 派生顶层 reason（压分时 = 核验失败说明；否则各轴
    「标签(分)：理由」+ value 汇总拼接）。

    返回键：relevance/semantics {"score","reason"}、value {"score","reason",
    "tag_kind","weights","dimensions"}、quote、scenes（核验后有效集，排序）、
    confidence、reason（派生顶层理由）、verify_fail（None|"quote"|"scenes"）。
    入参 haystack/scene_texts 须为调用方归一化后的核验文本（Task 6/7 现口径
    _normalize_quote(render(...))），本函数不重复归一。
    """
    relevance = item["relevance"]
    semantics = item["semantics"]
    cited = {s for s in item.get("scenes") or [] if s in scene_texts}
    if target.level == "scene":
        cited &= target.allowed_scenes

    verify_fail = None
    verify_reason = ""
    score = relevance["score"]
    if isinstance(score, int) and score >= VERDICT_FLOORS["relevance"]:
        sources = [scene_texts[s] for s in sorted(target.allowed_scenes)
                   if s in scene_texts] or [haystack]
        if not any(_quote_ok(item.get("quote"), source) for source in sources):
            verify_fail, verify_reason = "quote", _QUOTE_FAIL_REASON
        elif not (target.level == "scene" and not target.allowed_scenes):
            if len(cited) < _SCENE_MIN_COUNTS.get(target.level, 1):
                verify_fail, verify_reason = "scenes", _SCENES_FAIL_REASON
    if verify_fail:
        relevance = {"score": 1, "reason": verify_reason}

    sem_score = semantics["score"]
    if (semantics_null_without_desc and not target.has_desc
            and isinstance(sem_score, int) and sem_score >= 2):
        semantics = {"score": None, "reason": "缺少释义"}

    tag_kind = normalize_tag_kind(target.tag_kind)
    dims = item["value"]
    value_reason = dims.get("reason") or _summarize_value_reasons(dims)
    value_block = {
        "score": aggregate_value_score(
            {d: dims[d]["score"] for d in VALUE_DIRECTIONS},
            tag_kind, weights=value_weights),
        "reason": value_reason,
        "tag_kind": tag_kind,
        "weights": weights_dict_for_kind(tag_kind, value_weights),
        "dimensions": {d: dict(dims[d]) for d in VALUE_DIRECTIONS},
    }

    if verify_fail:
        reason = verify_reason
    else:
        reason = "；".join([
            _axis_reason("相关性", relevance),
            _axis_reason("语义", semantics),
            value_reason,
        ])

    return {
        "relevance": {"score": relevance["score"], "reason": relevance["reason"]},
        "semantics": {"score": semantics["score"], "reason": semantics["reason"]},
        "value": value_block,
        "quote": item.get("quote") or "",
        "scenes": sorted(cited),
        "confidence": item.get("confidence"),
        "reason": reason,
        "verify_fail": verify_fail,
    }


# ---------------------------------------------------------------------------
# 批编排引擎（评审归一 Task 4）：judge._judge_batch（固定 20 一批/整批重试/
# salvage）与 open_review.review_movie_tags（整片单批/漏判拆半/单条复试/
# 预算 2n+2）两套控制循环收敛为一个队列驱动循环，两侧差异全部收敛在
# VerdictPolicy 三个旋钮里。每次调用的原始响应与漏判都进 trace（两测统一
# 键族），审计能还原异常形态。
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VerdictPolicy:
    """批编排策略参数——judge 与评审两条旧控制循环的差异全在这三个旋钮。

    - max_batch_size：初始分批大小（None = 整批一队，评审现状；judge = 20）；
    - split_on_missing：漏判是否拆批恢复（评审开——漏判子集另起一批、零命中
      对半拆；judge 关——漏判重试一轮后直接记 errors 由上层淘汰）；
    - defer_single_retries：单条复试是否追加到所有子批之后排队补试（评审开，
      一次机会；judge 关）。

    预算不在这里：恒为 2 * len(targets) + 2（两测同款，见 run_verdicts）。
    """
    max_batch_size: int | None = None
    split_on_missing: bool = False
    defer_single_retries: bool = False


# 错误文案拍板口径：预算文案与现行评审逐字一致（运行器测试按全文断言）；
# 调用失败/截断/漏判三族文案沿用两侧现状的公共形状。
_BUDGET_ERROR = "评审调用预算耗尽，未发出恢复请求"
_TRUNCATED_ERROR = "评审输出超出长度限制"
_CALL_FAIL_PREFIX = "评审调用失败"


def _unwrap(response) -> tuple[str, bool, str]:
    """引擎响应归一（open_review._unwrap 同语义本地实现，不跨模块引私有）：
    纯文本替身按成功处理；LLMResponse 失败时给 raw.error 错误串。

    返回 (text, ok, error)。
    """
    if isinstance(response, str):
        return response, True, ""
    ok = bool(getattr(response, "ok", True))
    if not ok:
        raw = getattr(response, "raw", None) or {}
        return "", False, str(raw.get("error") or "模型未返回成功响应")
    return getattr(response, "content", None) or "", True, ""


def _truncated(response) -> bool:
    """finish_reason == "length" 判定（open_review._truncated 同语义本地实现）。

    ok=False 信封不判截断（失败请求的 raw 里即便带着 length 也走「调用失败」
    口径）；纯文本替身没有 raw 信封，恒 False。
    """
    if not getattr(response, "ok", True):
        return False
    raw = getattr(response, "raw", None) or {}
    choices = raw.get("choices") or [{}]
    return choices[0].get("finish_reason") == "length"


def _match_items(items: list[dict], batch: list[VerdictTarget],
                 match_by_name: bool) -> dict[str, dict]:
    """判决条目 → {target.key: 条目}：id/别名精确优先，其次名称两级兜底。

    id 匹配认 target.key 与 id_aliases（模型回 "3"/3/"t3" 都落回 key "t3"）；
    带 id 但引擎不认识的条目不进名称兜底（镜像 judge/_parse_reviews 两侧：
    回错 id 比名错更不可信）。名称兜底仅 match_by_name=True（judge 策略）
    时启用：normalize_key 精确 → 唯一前缀（judge._map_items 同款）。重复
    回配同一 key 以首条为准（setdefault）。
    """
    by_id: dict[str, VerdictTarget] = {}
    for target in batch:
        by_id.setdefault(target.key, target)
        for alias in target.id_aliases:
            by_id.setdefault(alias, target)
    by_name = ({normalize_key(target.tag): target for target in batch
                if normalize_key(target.tag)} if match_by_name else {})
    matched: dict[str, dict] = {}
    leftovers: list[tuple[str, dict]] = []
    for item in items:
        item_id = item.get("id")
        if item_id is not None:
            if item_id in by_id:
                matched.setdefault(by_id[item_id].key, item)
            continue
        if not match_by_name:
            continue
        key = normalize_key(item["tag"])
        if key and key in by_name:
            matched.setdefault(by_name[key].key, item)
        else:
            leftovers.append((key, item))
    for key, item in leftovers:
        if not key:
            continue
        hits = [candidate_key for candidate_key in by_name
                if key.startswith(candidate_key) or candidate_key.startswith(key)]
        if len(hits) == 1 and by_name[hits[0]].key not in matched:
            matched.setdefault(by_name[hits[0]].key, item)
    return matched


def run_verdicts(client, targets: list[VerdictTarget], *,
                 build_messages: Callable[[list[VerdictTarget], int], list[dict]],
                 run_id: str, policy: VerdictPolicy, haystack: str,
                 scene_texts: Mapping[str, str] | None = None,
                 value_weights: Mapping | None = None,
                 semantics_null_without_desc: bool = False,
                 match_by_name: bool = False, trace: dict | None = None,
                 usage: dict | None = None, request_slot=None,
                 ) -> tuple[dict[str, dict], dict[str, str]]:
    """队列驱动的判决批编排：返回 ({target.key: 终判}, {target.key: 错误})。

    控制流（评审归一拍板口径，覆盖两套旧循环）：初始按 max_batch_size 分批
    （None = 整批一队）；每批调用 → parse_verdicts → 回配（id/别名精确 →
    match_by_name 时名称两级）→ 全配齐即完成。漏判派发：
    attempt 0 零命中且未截断 → 整批重试一次（attempt 1，重试 hint 由
    build_messages 按 attempt 附加）；有命中 → split_on_missing 时漏判子集
    另起一批（已判不重发），否则 judge 口径漏判子集带 hint 重试一轮；
    零命中且 split_on_missing 且 >1 条 → 对半两批入队（front，深度优先）；
    单条 → defer_single_retries 时追加队尾单条复试（一次机会），否则记
    errors。截断（finish_reason=length）不整批重试——重发只会再截断，直接
    走拆批口径；已解析条目照常保留。每次调用前查预算（恒
    2*len(targets)+2，含单条复试），耗尽后剩余目标直接记「预算耗尽」，
    不再发请求。调用抛异常或 LLMResponse.ok=False：该批目标记「评审调用
    失败」，其余批继续。

    usage 传入 dict 时逐次真实响应累计（accumulate_usage，judge 同款）；
    trace 传入 dict 时记录统一键族 {"batches": [{batch, attempt, targets,
    raw, error, missing}], "errors": {key: msg}, "salvaged": int}——salvaged
    为每个未配齐批的已配条目**即时**计数累加（legacy 是末轮一次计 pending，
    语义不同；audit 读侧只做计数消费，不受口径差影响）。命中目标逐条
    finalize_verdict 出终判。
    """
    if not targets:
        return {}, {}
    scene_texts = scene_texts or {}
    targets_by_key = {target.key: target for target in targets}
    errors: dict[str, str] = {}
    verdicts: dict[str, dict] = {}
    budget = 2 * len(targets) + 2
    if trace is not None:
        # 统一键族三键恒在（成功批 errors 空表 / salvaged 0），前端读侧免判键。
        trace.setdefault("batches", [])
        trace.setdefault("errors", {})
        trace.setdefault("salvaged", 0)
    queue: deque = deque()
    size = policy.max_batch_size or len(targets)
    for start in range(0, len(targets), size):
        queue.append((targets[start:start + size], 0, False))

    def note_errors() -> None:
        """errors 就地镜像进 trace（实时进度读得到，终值同 legacy 落法）。"""
        if trace is not None and errors:
            trace["errors"] = dict(errors)

    while queue:
        batch, attempt, derived = queue.popleft()
        if budget <= 0:
            for target in batch:
                errors[target.key] = _BUDGET_ERROR
            note_errors()
            continue
        budget -= 1
        batch_id = f"{run_id}:{batch[0].key}-{batch[-1].key}"
        record = {"batch": batch_id, "attempt": attempt,
                  "targets": [target.key for target in batch], "raw": "",
                  "error": None, "missing": []}
        if trace is not None:
            trace.setdefault("batches", []).append(record)
        try:
            with request_slot if request_slot is not None else nullcontext():
                response = client.complete(build_messages(batch, attempt), batch_id)
        except Exception as exc:  # noqa: BLE001 —— 传输层任何异常统一按调用失败
            error = f"{_CALL_FAIL_PREFIX}：{exc}"
            record.update(error=error,
                          missing=[target.key for target in batch])
            for target in batch:
                errors[target.key] = error
            note_errors()
            continue
        if usage is not None:
            accumulate_usage(usage, getattr(response, "usage", None))
        text, ok, call_error = _unwrap(response)
        if not ok:
            error = f"{_CALL_FAIL_PREFIX}：{call_error}"
            record.update(error=error,
                          missing=[target.key for target in batch])
            for target in batch:
                errors[target.key] = error
            note_errors()
            continue
        record["raw"] = text
        truncated = _truncated(response)
        parse_error = ""
        try:
            items = parse_verdicts(text)
        except MTGParseError as exc:
            items = exc.items
            parse_error = str(exc)
        matched = _match_items(items, batch, match_by_name)
        for key, item in matched.items():
            verdicts.setdefault(key, finalize_verdict(
                item, targets_by_key[key], haystack=haystack,
                scene_texts=scene_texts, value_weights=value_weights,
                semantics_null_without_desc=semantics_null_without_desc))
        missing = [target for target in batch if target.key not in matched]
        if not missing:
            continue
        if truncated:
            error = _TRUNCATED_ERROR
        elif parse_error:
            error = f"评审响应无法解析：{parse_error}"
        elif not matched:
            error = "评审响应未包含有效判决"
        else:
            error = "评审响应缺少目标判决"
        record.update(error=error, missing=[target.key for target in missing])
        if trace is not None:
            trace["salvaged"] = trace.get("salvaged", 0) + len(matched)
        # 漏判派发（brief Step 3 控制流，按序判定）：
        if attempt == 0 and not derived and not truncated and not matched:
            queue.appendleft((batch, 1, True))              # 整批重试一次
        elif matched and policy.split_on_missing:
            queue.appendleft((missing, 0, True))            # 漏判子集另起一批
        elif matched:
            if attempt == 0:
                queue.appendleft((missing, 1, True))        # judge：漏判带 hint 重试一轮
            else:
                for target in missing:
                    errors[target.key] = error
                note_errors()
        elif policy.split_on_missing and len(missing) > 1:
            middle = len(missing) // 2                      # 对半两批，front 入队（深度优先）
            queue.appendleft((missing[middle:], 0, True))
            queue.appendleft((missing[:middle], 0, True))
        elif (len(missing) == 1 and policy.defer_single_retries
              and attempt == 0):
            queue.append((missing, 1, True))                # 单条复试排队尾，一次机会
        else:
            for target in missing:
                errors[target.key] = error
            note_errors()
    return verdicts, errors
