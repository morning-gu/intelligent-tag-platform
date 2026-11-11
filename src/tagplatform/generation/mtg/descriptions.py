"""标签描述（通用释义）的校验与补齐。

标签库里的 `tag_define.description` 是**词本身的字典式释义**——
"以愤世嫉俗、道德模糊、阴暗氛围为特征的犯罪题材类型"、"以追忆过往和怀旧
情感为主要观看需求"。它脱离任何影片单独成立。

而生成侧原本把 `reason`（这一轮在本片里找到的内容依据）直接当描述落库，
于是库里出现"总统蜷身藏于货舱，空间封闭压迫"这种只对某一部片成立的文字。
本模块给描述加上"通用性"这一关：不合规的描述一律不落库，缺的用一次
批量补写请求补齐（仍不合规就留空，让审核员在修改里填）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence

from tagplatform.generation.mtg.parse import MTGParseError, extract_json_array

logger = logging.getLogger(__name__)

# 描述长度：库内现有 9-30 字，均值 17。上限留一点余量给"一句完整释义"。
DESC_MIN = 9
DESC_MAX = 32

# 指代本片/某场戏的措辞——通用释义里不该出现。
# 注意不含"主角/结尾/开头"：这些在库里是**通用**用法（"主角因遭受不公…
# 展开复仇行动的剧情母题"就是一条合格的词条），把它们当泄漏会误杀成片定义。
_EVIDENCE_MARKERS = (
    "本片", "该片", "这部片", "这部影", "片中", "影片中", "剧情中", "故事中",
    "这一幕", "本场", "本剧",
)

_SCENE_ID = re.compile(r"scene_\d+", re.I)
_WRAP = "`'\"“”‘’《》〈〉"


class DescriptionError(RuntimeError):
    """补写描述调用失败（上层按"没有描述"处理）。"""


def clean_description(text: str | None) -> str:
    """去空白、引号、首尾标点；不做截断（超长由校验拒绝，交补写重写）。"""
    raw = str(text or "").strip().strip(_WRAP).strip()
    return re.sub(r"\s+", "", raw)


def is_general(text: str | None, names: Iterable[str] = ()) -> bool:
    """是否为"针对词本身"的通用释义。

    names 传入内容里的专名（片名、角色名/别名）：通用释义不该出现具体人名。
    宁可误杀（拒绝后由补写重写，仍不行就留空）也不要放行剧情描述。
    """
    desc = clean_description(text)
    if len(desc) < DESC_MIN or len(desc) > DESC_MAX:
        return False
    if _SCENE_ID.search(desc):
        return False
    if any(marker in desc for marker in _EVIDENCE_MARKERS):
        return False
    for name in names:
        cleaned = str(name or "").strip()
        if len(cleaned) >= 2 and cleaned in desc:
            return False
    return True


def content_names(content) -> tuple[str, ...]:
    """内容里的专名清单：片名 + 角色名 + 角色别名（用于通用性校验）。"""
    names: list[str] = []
    title = str(getattr(getattr(content, "asset", None), "title", "") or "").strip()
    if title:
        names.append(title)
    body = getattr(content, "content", None)
    for character in getattr(body, "characters", None) or []:
        name = str(getattr(character, "name", "") or "").strip()
        if name:
            names.append(name)
        for alias in getattr(character, "aliases", None) or []:
            alias_text = str(alias or "").strip()
            if alias_text:
                names.append(alias_text)
    return tuple(dict.fromkeys(names))


def build_description_messages(items: Sequence[dict]) -> list[dict]:
    """构造补写请求：items 每项 {tag, dir}（dir 为目录全路径，仅作归属提示）。"""
    lines = "\n".join(
        f"- {item['tag']}（归属目录：{item.get('dir') or '未指定'}）"
        for item in items
    )
    return [{
        "role": "user",
        "content": (
            "## 通用释义（标签描述补写）\n"
            "你在维护一个影视标签库。下面是待新增的标签（名称 + 归属目录），"
            "请为每个标签写一句**通用释义**。\n"
            "这句释义以后会脱离任何影片单独使用（标签库检索、审核界面展示），"
            "必须只解释**这个词本身**指什么、具备什么特征。\n"
            "硬性要求：\n"
            "1. 长度 10-32 字，一句话；\n"
            "2. 像词典条目，不叙述任何影片的剧情；\n"
            "3. 禁止出现片名、人名、地名、具体情节、具体镜头或时间点，"
            "禁止用“本片/这部片/影片中/主角/结尾”等指代；\n"
            "4. 句式可参考现有标签库：\n"
            "   - 宏大史诗：整体氛围宏大壮阔，具有史诗般的叙事格局\n"
            "   - 黑色电影：以愤世嫉俗、道德模糊、阴暗氛围为特征的犯罪题材类型\n"
            "   - 怀旧：以追忆过往和怀旧情感为主要观看需求\n"
            "5. 只输出 JSON：{\"items\":[{\"tag\":\"标签名\",\"desc\":\"通用释义\"}]}，"
            "不要解释、不要 Markdown 代码块。\n\n"
            "标签清单：\n" + lines
        ),
    }]


def _parse_items(content: str) -> list[dict]:
    try:
        entries = extract_json_array(content)
    except MTGParseError:
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def fill_descriptions(client, items: Sequence[dict], *, names: Iterable[str] = (),
                      run_id: str, usage: dict | None = None,
                      retries: int = 1) -> dict[str, str]:
    """批量补写通用释义 → {归一化标签名: 描述}（不合规的丢弃）。

    失败不抛错：生成任务不该因为"描述写不出来"而失败，缺的描述留空由
    审核员补（修改动作支持改 candidate_description）。
    """
    from tagplatform.generation.mtg.merge import normalize_tag

    if not items:
        return {}
    wanted = {normalize_tag(str(item.get("tag") or "")): item for item in items}
    wanted.pop("", None)
    if not wanted:
        return {}
    messages = build_description_messages(list(items))
    for attempt in range(retries + 1):
        request = messages if attempt == 0 else [{
            "role": "user",
            "content": messages[0]["content"] + (
                "\n\n上一版不合格：出现了具体影片信息、专名或长度越界。"
                "请严格照要求重写，只输出 JSON。"),
        }]
        try:
            response = client.complete(
                request, f"{run_id}:desc{'' if attempt == 0 else attempt}")
        except Exception as exc:  # noqa: BLE001 —— 网络/超时都不该拖垮任务
            logger.warning("补写标签描述失败（第 %d 次）：%s", attempt + 1, exc)
            continue
        if usage is not None:
            usage["calls"] = usage.get("calls", 0) + 1
            payload = getattr(response, "usage", None) or {}
            for key in ("prompt_tokens", "completion_tokens"):
                usage[key] = (usage.get(key, 0)
                              + int(payload.get(key, 0) or 0))
        if not getattr(response, "ok", False):
            logger.warning("补写标签描述返回失败（第 %d 次）", attempt + 1)
            continue
        result: dict[str, str] = {}
        for entry in _parse_items(getattr(response, "content", "") or ""):
            key = normalize_tag(str(entry.get("tag") or ""))
            if key not in wanted or key in result:
                continue
            desc = clean_description(entry.get("desc") or entry.get("description"))
            if is_general(desc, names):
                result[key] = desc
        if result:
            return result
    return {}


__all__ = [
    "DESC_MAX", "DESC_MIN", "DescriptionError", "build_description_messages",
    "clean_description", "content_names", "fill_descriptions", "is_general",
]
