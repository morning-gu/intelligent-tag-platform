"""生产 Judge 与开放评测评审共用的判决域/解析/证据核验/批编排引擎（本任务先落域段）。

后续任务在同一文件追加：融合解析器（parse_verdicts，Task 2）、证据核验终判
（finalize_verdict，Task 3）与批编排引擎（run_verdicts，Task 4）。

分层红线：本模块不得 import fastapi / sqlalchemy；eval 侧 import 本模块属于
既有先例方向（`open_review.py` import `mtg.prompts` 已开此口）。
"""
from __future__ import annotations

from dataclasses import dataclass

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
