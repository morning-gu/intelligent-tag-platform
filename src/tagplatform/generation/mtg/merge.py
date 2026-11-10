"""跨轮候选合并：归一去重 + e5 语义聚类（complete-link）。

多轮生成的固有问题是"同义冗余"：同一特征换种说法就重复出现（本地
MovieTagGen 实测冗余率 0.821）。这里两层处理：

1. **归一去重**：去掉空白与标点、casefold 后同形的直接合并（"多线叙事" /
   "多线 叙事" 是同一个）；
2. **语义聚类**：对归一后的候选做 e5 嵌入，余弦 ≥ `threshold` 的做
   complete-link 合并——要求两个簇的**任意两成员**都够相似才合，避免
   "链式传递"把一串不相干的词连成一个大簇。

代表词选取：出现轮数最多 → 名字更短 → 更早出现（确定性，跨进程稳定）。
维度取簇内出现最多的（并列取更早出现的）。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import reduce


@dataclass(frozen=True)
class MergedCandidate:
    """合并后的候选：代表词 + 别名 + 出现轮数 + 场景证据并集 + 层级（内部用）。

    `scope`（film|scene）只在流水线内部用于"全片级要两个场景支撑"和
    配额排序，**不落库、不进审核页**——对外只有标签本身。
    """

    tag: str
    dimension: str | None
    scenes: tuple[str, ...]
    runs: int
    aliases: tuple[str, ...]
    reason: str
    scope: str | None = None
    # 模型自报的归属目录（原始串，可能是 id / 路径 / 名字）；合并时取多数派，
    # 由流水线解析成 dir_id 并校验，这里不做合法性判断。
    dir_raw: str | None = None


def normalize_tag(name: str) -> str:
    """归一化键：只留字母/数字/汉字，casefold，丢弃其余字符（含空白标点）。"""
    return "".join(ch for ch in name.casefold() if ch.isalnum())


def _clusters(vectors, threshold: float) -> list[list[int]]:
    """complete-link 聚类：返回下标簇列表（簇内任意两成员相似度 ≥ 阈值）。"""
    n = len(vectors)
    parent = list(range(n))
    members: dict[int, set[int]] = {i: {i} for i in range(n)}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    sims = vectors @ vectors.T
    pairs = [(float(sims[i][j]), i, j)
             for i in range(n) for j in range(i + 1, n)]
    pairs.sort(reverse=True)
    for similarity, i, j in pairs:
        if similarity < threshold:
            break
        root_i, root_j = find(i), find(j)
        if root_i == root_j:
            continue
        # complete-link 检查：两簇任意交叉对都必须过线
        if all(float(sims[a][b]) >= threshold
               for a in members[root_i] for b in members[root_j]):
            parent[root_j] = root_i
            members[root_i] |= members.pop(root_j)
    result: dict[int, list[int]] = {}
    for index in range(n):
        result.setdefault(find(index), []).append(index)
    return [sorted(group) for group in result.values()]


def merge_candidates(entries: list[dict], embedder,
                     threshold: float = 0.90) -> list[MergedCandidate]:
    """多轮候选 → 合并后的候选列表（保持首次出现顺序）。

    entries 每项：{tag, dimension, scenes, run, reason}
    """
    groups: dict[str, dict] = {}
    order: list[str] = []
    for entry in entries:
        key = normalize_tag(entry["tag"])
        if not key:
            continue
        if key not in groups:
            groups[key] = {
                "names": [], "dimensions": Counter(), "scopes": Counter(),
                "dirs": Counter(), "scenes": set(), "runs": set(), "reason": "",
                "first": len(order),
            }
            order.append(key)
        group = groups[key]
        group["names"].append(entry["tag"])
        if entry.get("dimension"):
            group["dimensions"][entry["dimension"]] += 1
        if entry.get("scope"):
            group["scopes"][entry["scope"]] += 1
        if entry.get("dir"):
            group["dirs"][entry["dir"]] += 1
        group["scenes"].update(entry.get("scenes") or [])
        group["runs"].add(entry.get("run", 0))
        if not group["reason"] and entry.get("reason"):
            group["reason"] = entry["reason"]

    items = [groups[key] for key in order]
    if len(items) <= 1:
        return [_to_candidate(items[0])] if items else []

    vectors = embedder.embed([key for key in order], role="query")
    clusters = _clusters(vectors, threshold)

    merged: list[MergedCandidate] = []
    for cluster in clusters:
        members = [items[index] for index in cluster]
        # 代表词：轮数多优先 → 名字短优先 → 出现早优先
        best = reduce(lambda a, b: a if _rank(a) <= _rank(b) else b, members)
        dimension = _primary_dimension(members)
        scenes: set[str] = set()
        runs: set[int] = set()
        aliases: list[str] = []
        for member in members:
            scenes |= member["scenes"]
            runs |= member["runs"]
            for name in member["names"]:
                if name not in aliases:
                    aliases.append(name)
        merged.append(MergedCandidate(
            tag=best["names"][0],
            dimension=dimension,
            scenes=tuple(sorted(scenes)),
            runs=len(runs),
            aliases=tuple(aliases),
            reason=best["reason"],
            scope=_primary_scope(members),
            dir_raw=_primary_dir(members),
        ))
    # 按首次出现位置排序，保证输出稳定
    return merged


def _rank(group: dict) -> tuple[int, int, int]:
    """代表词排序键：轮数降序（取负）→ 名字长度 → 首次出现位置。"""
    return (-len(group["runs"]), len(group["names"][0]), group["first"])


def _primary_dimension(members: list[dict]) -> str | None:
    counter: Counter = Counter()
    for member in members:
        counter.update(member["dimensions"])
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def _primary_scope(members: list[dict]) -> str | None:
    """簇内多数派层级；没声明过（旧数据/模型漏填）返回 None。

    多数并列时 film 优先：三层都把它当全片特征，按更严的约束处理。
    """
    counter: Counter = Counter()
    for member in members:
        counter.update(member.get("scopes") or {})
    if not counter:
        return None
    top = max(counter.values())
    for scope in ("film", "scene"):
        if counter.get(scope) == top:
            return scope
    return counter.most_common(1)[0][0]


def _primary_dir(members: list[dict]) -> str | None:
    """簇内多数派目录（模型自报的原始串）；没有则 None。

    并列时取字典序最小的，保证跨进程稳定（保序不依赖插入顺序）。
    """
    counter: Counter = Counter()
    for member in members:
        counter.update(member.get("dirs") or {})
    if not counter:
        return None
    top = max(counter.values())
    return sorted(name for name, count in counter.items() if count == top)[0]


def _to_candidate(group: dict) -> MergedCandidate:
    aliases = tuple(dict.fromkeys(group["names"]))
    return MergedCandidate(
        tag=aliases[0],
        dimension=_primary_dimension([group]),
        scenes=tuple(sorted(group["scenes"])),
        runs=len(group["runs"]),
        aliases=aliases,
        reason=group["reason"],
        scope=_primary_scope([group]),
        dir_raw=_primary_dir([group]),
    )
