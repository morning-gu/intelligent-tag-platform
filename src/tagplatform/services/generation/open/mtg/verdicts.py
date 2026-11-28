"""生产 Judge 与开放评测评审共用的判决域/解析/证据核验/批编排引擎。

已落：判决域（VERDICT_FLOORS/verdict_pass/VerdictTarget，Task 1）与融合
解析器（parse_verdicts，Task 2——两套容错家族取并集）。后续任务在同一
文件追加：证据核验终判（finalize_verdict，Task 3）与批编排引擎
（run_verdicts，Task 4）。

分层红线：本模块不得 import fastapi / sqlalchemy；eval 侧 import 本模块属于
既有先例方向（`open_review.py` import `mtg.prompts` 已开此口）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from tagplatform.services.generation.open.mtg.parse import (
    MTGParseError,
    extract_json_array,
)
from tagplatform.services.tags.value_scoring import (
    VALUE_DIRECTIONS,
    VALUE_DIRECTION_ALIASES,
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
