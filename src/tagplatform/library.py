"""标签库（目录 + 标签）的只读视图：直接读 `tag_catalog` / `tag_define`。

v2 起标签库就是这两张正式表（标签数据由导入/治理链路写入，或由测试夹具
镜像出的小库）：

- 目录：`tag_catalog`（dir_id / parent_id / dir_name / dir_type /
  applicable_scope / description / aliases）
- 标签：`tag_define`（tag_id / dir_id / tag_name / tag_type / description /
  aliases / status）

生成本身只依赖这里的三类能力：
1. **范围**：任务可指定 dir_ids，只有落在这些目录（含子目录）下的标签参与
   召回 / 对齐；
2. **可扩展性**：`dir_type == "dir_dynamic"` 的目录才允许产生新标签，静态
   目录只能"命中"不能"新增"；
3. **归属与挂载粒度**：每个标签/候选都能追到目录，目录的
   `applicable_scope`（content/scene/character）决定 `pre_asset_tag` 的
   `target_type`。

数据源绑定：进程内只认一个"当前引擎"（`bind_engine`）。app 在 `create_app`
里绑定自己的引擎，测试 fixture 依次绑定临时库——`load_library()` 因此不需要
层层传 session，生成线程池里的工作线程也能取到同一个库视图。缓存以库内容的
指纹作版本号（标签改名/增删都会换 key），向量索引不会读到旧库。

分层红线：本模块不得 import fastapi（设计文档 §9.4）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

DIR_STATIC = "dir_static"
DIR_DYNAMIC = "dir_dynamic"
SCOPE_CONTENT = "content"
SCOPE_SCENE = "scene"
SCOPE_CHARACTER = "character"
SCOPES = (SCOPE_CONTENT, SCOPE_SCENE, SCOPE_CHARACTER)

# 失效状态：v1 的 JSON 库写 `deprecate`，`tag_define.status` 的 CHECK 只允许
# `deprecated`（main 的取值）——读的时候两种都认，写的时候统一用后者。
DEPRECATED_STATUSES = ("deprecate", "deprecated")


def is_deprecated(status: str | None) -> bool:
    return (status or "").strip().lower() in DEPRECATED_STATUSES


@dataclass(frozen=True)
class LibraryTag:
    """库内一枚标签的检索视图：名字 + 主键 + 向量 + 目录与挂载粒度。

    放在本模块（而不是 generation.mtg.catalog_align）是为了避免
    "library → mtg → pipeline → library" 的循环导入。
    """

    tag_id: str
    name: str
    vector: np.ndarray
    dir_id: str | None = None
    scope: str = "content"


@dataclass(frozen=True)
class Directory:
    """一枚目录节点（对应未来 tag_catalog 的一行）。"""

    dir_id: str
    dir_name: str
    dir_type: str = DIR_STATIC
    applicable_scope: str = SCOPE_CONTENT
    parent_id: str | None = None
    level: int = 1
    description: str = ""
    aliases: tuple[str, ...] = ()

    @property
    def allows_new_tags(self) -> bool:
        """动态目录允许开放生成审核后扩充，静态目录不允许。"""
        return self.dir_type == DIR_DYNAMIC


@dataclass(frozen=True)
class TagDef:
    """一枚标签（对应未来 tag_define 的一行）。"""

    tag_id: str
    dir_id: str
    tag_name: str
    tag_type: str = "val_fixed"
    description: str = ""
    aliases: tuple[str, ...] = ()
    status: str = "active"


def _as_tuple(values) -> tuple[str, ...]:
    if not values:
        return ()
    return tuple(str(v) for v in values if str(v).strip())


def _alias_list(value) -> list:
    """DB 的 aliases 是 JSON 数组文本；也容忍"a,b"老格式与已解析的 list。"""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    text = str(value).strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [part.strip() for part in text.split(",") if part.strip()]
        return list(parsed) if isinstance(parsed, list) else []
    return [part.strip() for part in text.split(",") if part.strip()]


class TagLibrary:
    """目录树 + 标签集合的只读视图（进程内可复用）。"""

    def __init__(self, dirs: list[Directory], tags: list[TagDef], *,
                 version: str = "", source: str = ""):
        self.version = version
        self.source = source
        self._dirs: dict[str, Directory] = {d.dir_id: d for d in dirs}
        self._tags: dict[str, TagDef] = {t.tag_id: t for t in tags}
        # 父 → 子（保持模板顺序，保证同一份库的遍历顺序稳定）
        self._children: dict[str, list[str]] = {}
        for directory in dirs:
            if directory.parent_id:
                self._children.setdefault(directory.parent_id, []).append(directory.dir_id)
        self._by_name: dict[str, list[str]] = {}
        for directory in dirs:
            self._by_name.setdefault(directory.dir_name, []).append(directory.dir_id)

    # ---------- 基本读取 ----------

    def directory(self, dir_id: str | None) -> Directory | None:
        return self._dirs.get(dir_id or "")

    def tag(self, tag_id: str | None) -> TagDef | None:
        return self._tags.get(tag_id or "")

    def dirs(self) -> list[Directory]:
        return list(self._dirs.values())

    def tags(self) -> list[TagDef]:
        return list(self._tags.values())

    def children(self, dir_id: str) -> list[Directory]:
        return [self._dirs[cid] for cid in self._children.get(dir_id, [])]

    def root_dirs(self) -> list[Directory]:
        return [d for d in self._dirs.values() if d.parent_id is None]

    def descendants(self, dir_ids: list[str] | None) -> set[str]:
        """dir_ids（含自身）∪ 全部后代；None/空 = 全库。"""
        if not dir_ids:
            return set(self._dirs)
        result: set[str] = set()
        stack = list(dir_ids)
        while stack:
            current = stack.pop()
            if current in result or current not in self._dirs:
                continue
            result.add(current)
            stack.extend(self._children.get(current, []))
        return result

    def dirs_by_name(self, name: str) -> list[Directory]:
        return [self._dirs[i] for i in self._by_name.get(name, [])]

    def path_of(self, dir_id: str) -> list[str]:
        """目录全路径（L1 → 自身）；未知目录返回空列表。"""
        names: list[str] = []
        seen: set[str] = set()
        current = self._dirs.get(dir_id)
        while current is not None and current.dir_id not in seen:
            seen.add(current.dir_id)
            names.append(current.dir_name)
            current = self._dirs.get(current.parent_id or "")
        return list(reversed(names))

    # ---------- 语义能力 ----------

    def scope_of(self, dir_id: str | None) -> str:
        """目录的挂载粒度：自身声明优先，缺失时沿父链回退，最后按 content。"""
        seen: set[str] = set()
        current = self._dirs.get(dir_id or "")
        while current is not None and current.dir_id not in seen:
            seen.add(current.dir_id)
            if current.applicable_scope in SCOPES:
                return current.applicable_scope
            current = self._dirs.get(current.parent_id or "")
        return SCOPE_CONTENT

    def allows_new_tags(self, dir_id: str | None) -> bool:
        directory = self._dirs.get(dir_id or "")
        return bool(directory and directory.allows_new_tags)

    def scope_of_tag(self, tag_id: str | None) -> str:
        tag = self._tags.get(tag_id or "")
        return self.scope_of(tag.dir_id) if tag else SCOPE_CONTENT

    def tags_in_dirs(self, dir_ids: list[str] | None) -> list[TagDef]:
        """目录范围（含子目录）内的标签；dir_ids 为空 = 全库。"""
        allowed = self.descendants(dir_ids)
        return [t for t in self._tags.values() if t.dir_id in allowed]

    def new_tag_dirs(self, dir_ids: list[str] | None) -> list[Directory]:
        """可产新标签的目录：范围内且 dir_dynamic。"""
        allowed = self.descendants(dir_ids)
        return [d for d in self._dirs.values()
                if d.dir_id in allowed and d.allows_new_tags]

    # ---------- 构造 ----------

    @classmethod
    def from_rows(cls, dir_rows, tag_rows) -> "TagLibrary":
        """由 `tag_catalog` / `tag_define` 的行对象构造（ORM 行或 Row 均可）。

        目录层级（`level`）按父链现算：表里没有这一列（main 的 schema 归
        v1 的 level 概念到 self-reference），而 `path_of` 的输出要稳定。
        """
        dirs: list[Directory] = []
        parents: dict[str, str | None] = {}
        for row in dir_rows:
            dir_id = str(row.dir_id)
            parents[dir_id] = row.parent_id
            dirs.append(
                Directory(
                    dir_id=dir_id,
                    dir_name=str(row.dir_name),
                    dir_type=str(row.dir_type or DIR_STATIC),
                    applicable_scope=str(row.applicable_scope or SCOPE_CONTENT),
                    parent_id=row.parent_id,
                    description=str(row.description or ""),
                    aliases=_as_tuple(_alias_list(row.aliases)),
                )
            )
        for index, directory in enumerate(dirs):
            depth, seen, cursor = 1, {directory.dir_id}, parents.get(directory.dir_id)
            while cursor and cursor in parents and cursor not in seen:
                seen.add(cursor)
                depth += 1
                cursor = parents.get(cursor)
            if depth != directory.level:
                dirs[index] = Directory(
                    dir_id=directory.dir_id, dir_name=directory.dir_name,
                    dir_type=directory.dir_type,
                    applicable_scope=directory.applicable_scope,
                    parent_id=directory.parent_id, level=depth,
                    description=directory.description, aliases=directory.aliases,
                )
        tags = [
            TagDef(
                tag_id=str(row.tag_id),
                dir_id=str(row.dir_id),
                tag_name=str(row.tag_name),
                tag_type=str(row.tag_type or "val_fixed"),
                description=str(row.description or ""),
                aliases=_as_tuple(_alias_list(row.aliases)),
                status=str(row.status or "active"),
            )
            for row in tag_rows
        ]
        return cls(dirs, tags, version=_fingerprint(dirs, tags), source=SOURCE_DB)


# ---------------------------------------------------------------- 数据源绑定

SOURCE_DB = "db"

# 当前生效的引擎（app 启动时绑定；测试 fixture 按需压栈/出栈）
_ENGINES: list = []
_ENGINE_LOCK = threading.Lock()

_CACHE: dict[str, TagLibrary] = {}
_CACHE_LOCK = threading.Lock()


def _fingerprint(dirs: list[Directory], tags: list[TagDef]) -> str:
    """库内容指纹：任一目录/标签增删改都会换版本号（向量索引据此失效）。"""
    digest = hashlib.sha1()
    for d in dirs:
        digest.update(f"D|{d.dir_id}|{d.dir_name}|{d.dir_type}|{d.parent_id}|"
                      f"{d.applicable_scope}|{d.description}|{','.join(d.aliases)}\n"
                      .encode("utf-8"))
    for t in tags:
        digest.update(f"T|{t.tag_id}|{t.dir_id}|{t.tag_name}|{t.tag_type}|"
                      f"{t.status}|{t.description}|{','.join(t.aliases)}\n"
                      .encode("utf-8"))
    return digest.hexdigest()[:16]


def bind_engine(engine) -> None:
    """绑定当前生效的 DB 引擎（app 启动、测试 fixture）；后绑定者优先。"""
    with _ENGINE_LOCK:
        _ENGINES.append(engine)
    _clear_caches()


def unbind_engine(engine=None) -> None:
    """解除绑定（测试 teardown）：不传则弹出最近一次绑定。"""
    with _ENGINE_LOCK:
        if not _ENGINES:
            return
        if engine is None:
            _ENGINES.pop()
        elif engine in _ENGINES:
            _ENGINES.remove(engine)
    _clear_caches()


def current_engine():
    """当前生效的引擎（未绑定返回 None）。"""
    with _ENGINE_LOCK:
        return _ENGINES[-1] if _ENGINES else None


def _clear_caches() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
    with _INDEX_LOCK:
        _INDEX_CACHE.clear()
        _DIR_VECTOR_CACHE.clear()


def load_library(session=None) -> TagLibrary:
    """从 `tag_catalog` / `tag_define` 读标签库（指纹相同则复用缓存）。

    不传 session 时用当前绑定引擎开一个短会话；两者都没有时抛 OSError
    （等价于旧的"库文件缺失"）。
    """
    if session is not None:
        return library_from_session(session)
    engine = current_engine()
    if engine is None:
        raise OSError("标签库数据库未绑定（app 未启动或未调用 bind_engine）")
    from sqlalchemy.orm import Session

    with Session(engine) as own_session:
        return library_from_session(own_session)


def library_from_session(session) -> TagLibrary:
    """由 ORM 会话读两表；内容指纹命中缓存时直接返回旧视图。

    不加 ORDER BY：SQLite 的自然顺序即 rowid 插入顺序，与迁移/导入时的
    目录树顺序一致（旧的 JSON 库同样按文件顺序遍历，前端树与召回顺序不变）。
    """
    from tagplatform.db.models import TagCatalog, TagDefine

    dir_rows = session.query(TagCatalog).all()
    tag_rows = session.query(TagDefine).all()
    library = TagLibrary.from_rows(dir_rows, tag_rows)
    with _CACHE_LOCK:
        cached = _CACHE.get(library.version)
        if cached is not None:
            return cached
        _CACHE.clear()
        _CACHE[library.version] = library
    return library


def try_load_library(session=None) -> TagLibrary | None:
    """加载失败返回 None（库未就绪时生成退化为"无库"模式，不致命）。"""
    try:
        return load_library(session)
    except Exception as exc:  # noqa: BLE001 - 任何异常都退化为无库
        logger.warning("标签库不可用（%s），本轮按无库运行", exc)
        return None


# ---------------------------------------------------------------- 向量索引

_INDEX_CACHE: dict[tuple, list[LibraryTag]] = {}
_DIR_VECTOR_CACHE: dict[tuple, tuple[list[str], np.ndarray]] = {}
_INDEX_LOCK = threading.Lock()


def _embeddings_key(library: TagLibrary, embedder, dir_ids) -> tuple:
    return (library.version, library.source, tuple(sorted(dir_ids or ())),
            getattr(embedder, "model_name", None) or id(embedder))


def library_index(library: TagLibrary, embedder,
                  dir_ids: list[str] | None = None) -> list[LibraryTag]:
    """库内标签的对齐索引（带向量），只含范围内、有效、非目录、非占位的行。

    向量按库 + 模型 + 范围缓存：同一进程内多次生成不重复嵌入。
    """
    key = _embeddings_key(library, embedder, dir_ids)
    with _INDEX_LOCK:
        cached = _INDEX_CACHE.get(key)
        if cached is not None:
            return cached

    tags = [t for t in library.tags_in_dirs(dir_ids)
            if not is_deprecated(t.status) and not _is_placeholder(t.tag_name)]
    if not tags:
        index: list[LibraryTag] = []
    else:
        vectors = embedder.embed([t.tag_name for t in tags], role="passage")
        index = [
            LibraryTag(tag_id=t.tag_id, name=t.tag_name,
                       vector=np.asarray(vec, dtype=np.float32),
                       dir_id=t.dir_id, scope=library.scope_of(t.dir_id))
            for t, vec in zip(tags, vectors)
        ]
    with _INDEX_LOCK:
        _INDEX_CACHE.clear()
        _INDEX_CACHE[key] = index
    return index


def dir_vectors(library: TagLibrary, embedder,
                dir_ids: list[str] | None = None) -> tuple[list[str], np.ndarray]:
    """可扩展目录的（id 列表, 描述向量矩阵），用于新词的目录归属。

    目录文本 = 目录名 + 描述 + 该目录已收录的标签名（最多 12 个）。只靠
    "情绪基调"这种短名，模型很难判断"权力更迭"该归哪里；带上已收录的
    "压抑/温情/宿命感"，归属判断才稳。
    """
    key = _embeddings_key(library, embedder, dir_ids)
    with _INDEX_LOCK:
        cached = _DIR_VECTOR_CACHE.get(key)
        if cached is not None:
            return cached

    dirs = library.new_tag_dirs(dir_ids)
    if not dirs:
        result = ([], np.zeros((0, 0), dtype=np.float32))
    else:
        samples: dict[str, list[str]] = {}
        for tag in library.tags():
            if is_deprecated(tag.status) or _is_placeholder(tag.tag_name):
                continue
            bucket = samples.setdefault(tag.dir_id, [])
            if len(bucket) < _DIR_SAMPLE_TAGS:
                bucket.append(tag.tag_name)
        texts = [dir_text(d, samples.get(d.dir_id, [])) for d in dirs]
        matrix = np.asarray(embedder.embed(texts, role="passage"), dtype=np.float32)
        result = ([d.dir_id for d in dirs], matrix)
    with _INDEX_LOCK:
        _DIR_VECTOR_CACHE.clear()
        _DIR_VECTOR_CACHE[key] = result
    return result


_DIR_SAMPLE_TAGS = 12


def dir_text(directory, sample_tags: list[str] | None = None) -> str:
    """目录 → 归属判定用的嵌入文本（目录名 + 描述 + 已收录标签名）。"""
    parts = [directory.dir_name]
    if directory.description:
        parts.append(directory.description)
    if sample_tags:
        parts.append("已收录：" + "、".join(sample_tags))
    return "。".join(parts)


_PLACEHOLDER_MARKS = ("YYYY-MM-DD", "yyyy-mm-dd", "填写", "示例", "TODO")


def _is_placeholder(name: str) -> bool:
    """模板占位行（"按 YYYY-MM-DD 填写"这类）不参与对齐。"""
    return any(mark in name for mark in _PLACEHOLDER_MARKS)


@dataclass
class DirAssignment:
    """一条新词候选的目录归属结论。"""

    dir_id: str | None
    dir_name: str | None
    scope: str
    score: float = 0.0
    reason: str = ""


def rank_dir_assignments(library: TagLibrary, embedder, vector: np.ndarray,
                         dir_ids: list[str] | None = None,
                         top_n: int = 5) -> list[DirAssignment]:
    """新词 → 目录候选（按描述相似度降序）。

    返回多条而不是一条：目录的 applicable_scope 决定挂载粒度，最佳目录若
    解析不出挂载点（如 character 目录但证据里没有唯一角色），调用方可以退
    到次优目录，而不是把这个标签丢掉。
    """
    ids, matrix = dir_vectors(library, embedder, dir_ids)
    if not ids or matrix.size == 0 or vector.size != matrix.shape[1]:
        return [DirAssignment(dir_id=None, dir_name=None, scope=SCOPE_CONTENT,
                              reason="范围内没有可扩展目录")]
    scores = matrix @ np.asarray(vector, dtype=np.float32)
    order = list(np.argsort(-scores))[:max(1, top_n)]
    results = []
    for index in order:
        directory = library.directory(ids[index])
        name = directory.dir_name if directory else ids[index]
        results.append(DirAssignment(
            dir_id=ids[index],
            dir_name=directory.dir_name if directory else None,
            scope=library.scope_of(ids[index]),
            score=float(scores[index]),
            reason=f"与目录「{name}」描述相似度 {float(scores[index]):.2f}",
        ))
    return results


def assign_dir(library: TagLibrary, embedder, vector: np.ndarray,
               dir_ids: list[str] | None = None) -> DirAssignment:
    """新词 → 目录：取描述相似度最高的可扩展目录（无候选时 dir_id=None）。"""
    return rank_dir_assignments(library, embedder, vector, dir_ids, top_n=1)[0]
