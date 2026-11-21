"""情绪词表：中文名 ↔ 大写码。

上游契约（new_json.txt）里 `emotion[]` 是 `{name, code}`，code 是大写下划线枚举。
上游没有给出正式码表，这里定一套固定词表（双方按同一份表校验）：

- `FEELINGS`：人物情绪，用于 `events[].participant_ids[].emotion`
- `MOODS`：场景氛围，用于 `scenes[].emotion`

模型输出中文名，落库前用 `to_emotion()` 换成 `{name, code}`；词表外的词直接丢弃，
保证 code 永远合法（宁可少一个氛围，也不产出对不上的码）。
"""
from collections.abc import Sequence

# 人物情绪（18）
FEELINGS: tuple[tuple[str, str], ...] = (
    ("愤怒", "ANGER"),
    ("恐惧", "FEAR"),
    ("挫败", "FRUSTRATION"),
    ("惊讶", "SURPRISE"),
    ("悲伤", "SADNESS"),
    ("喜悦", "JOY"),
    ("厌恶", "DISGUST"),
    ("焦虑", "ANXIETY"),
    ("内疚", "GUILT"),
    ("坚定", "DETERMINATION"),
    ("痛苦", "PAIN"),
    ("爱意", "AFFECTION"),
    ("轻蔑", "CONTEMPT"),
    ("困惑", "CONFUSION"),
    ("紧张", "NERVOUS"),
    ("期待", "ANTICIPATION"),
    ("绝望", "DESPAIR"),
    ("释然", "RELIEF"),
)

# 场景氛围（12）
MOODS: tuple[tuple[str, str], ...] = (
    ("紧张压迫", "TENSE_OPPRESSIVE"),
    ("悬疑神秘", "MYSTERIOUS_SUSPENSEFUL"),
    ("温馨轻松", "WARM_RELAXED"),
    ("激烈冲突", "INTENSE_CONFLICT"),
    ("压抑沉重", "OPPRESSIVE_HEAVY"),
    ("冷峻克制", "COLD_RESTRAINED"),
    ("恐怖惊悚", "HORROR_THRILLER"),
    ("悲凉凄美", "DESOLATE_MELANCHOLY"),
    ("欢快明快", "CHEERFUL_BRIGHT"),
    ("庄严肃穆", "SOLEMN_GRAND"),
    ("浪漫柔情", "ROMANTIC_TENDER"),
    ("荒诞幽默", "ABSURD_HUMOR"),
)

_BY_NAME = {name: code for name, code in FEELINGS + MOODS}
_BY_CODE = {code: name for name, code in FEELINGS + MOODS}


def prompt_catalog(pool: Sequence[tuple[str, str]]) -> str:
    """给提示词用的词表串：`愤怒(ANGER)、恐惧(FEAR)…`。"""
    return "、".join(f"{name}({code})" for name, code in pool)


def to_emotion(value, pool: Sequence[tuple[str, str]] | None = None) -> dict | None:
    """中文名或 code → `{"name","code"}`；词表外返回 None（调用方丢弃）。

    `pool` 给定时只接受该子表里的词（人物情绪不会混进场景氛围）。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    allowed = set(_BY_NAME) if pool is None else {n for n, _ in pool}
    if text in allowed:
        return {"name": text, "code": _BY_NAME[text]}
    upper = text.upper()
    for code, name in _BY_CODE.items():
        if code == upper and name in allowed:
            return {"name": name, "code": code}
    return None


def to_emotions(values, pool: Sequence[tuple[str, str]], limit: int = 3) -> list[dict]:
    """多个词 → 去重后的 `{name, code}` 列表（保持顺序，最多 limit 个）。"""
    out: list[dict] = []
    if not isinstance(values, list):
        return out
    for value in values:
        item = to_emotion(value, pool)
        if item and item not in out:
            out.append(item)
        if len(out) >= limit:
            break
    return out
