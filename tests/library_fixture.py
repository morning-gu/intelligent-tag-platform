"""测试用标签库（JSON 模拟）：目录树 + 标签的构造助手。

标签库运行时直接读 `tag_catalog` / `tag_define`；单测需要一套小而确定的库，
这里的 JSON 只是**夹具的书写格式**——`write_library` 写完就把整套库镜像进
已绑定的测试引擎（`seed_database`），所以测试断言走的仍是真实的 DB 路径。

    write_library(root, dirs=..., tags=...)   # 写 dirs.json / tags.json + 镜像入库
    DEFAULT_DIRS / DEFAULT_TAGS               # 五个目录 + 七个标签的默认库
    library_root()                            # 当前测试库目录（模块内全局）

目录覆盖三种可扩展性/粒度组合，保证 scope 与 dir_type 的行为都能测到。
"""

from __future__ import annotations

import itertools
import json
import tempfile
from pathlib import Path

_VERSION = itertools.count(1)

# 当前生效的测试库目录：autouse fixture 首次 write_library 时确定，之后
# add_tags/add_dirs 继续往里写（不再挂在 Settings 上——生产不再有 JSON 库）。
_ROOT: Path | None = None


def _clear_cache() -> None:
    """库改写后清掉进程内缓存（库 mtime 缓存 + 向量索引缓存）。"""
    from tagplatform import library as library_mod

    library_mod._CACHE.clear()
    library_mod._INDEX_CACHE.clear()
    library_mod._DIR_VECTOR_CACHE.clear()


# 已绑定到测试库的 DB 引擎：测试中途改写 JSON 库时同步镜像进 tag_catalog /
# tag_define，否则新标签 id 没有父行、pre_asset_tag 外键会失败。
_BOUND_ENGINES: list = []

DEFAULT_DIRS = [
    {"dir_id": "dir_content", "parent_id": None, "level": 1,
     "dir_name": "内容特征标签", "dir_type": "dir_static",
     "applicable_scope": "content", "description": "内容本身的类型与风格",
     "aliases": []},
    {"dir_id": "dir_genre", "parent_id": "dir_content", "level": 2,
     "dir_name": "主类型", "dir_type": "dir_static",
     "applicable_scope": "content", "description": "故事的主要类型，如动作、剧情",
     "aliases": []},
    {"dir_id": "dir_theme", "parent_id": "dir_content", "level": 2,
     "dir_name": "细分题材", "dir_type": "dir_dynamic",
     "applicable_scope": "content", "description": "细分题材与剧情母题，如复仇、非线性叙事",
     "aliases": []},
    {"dir_id": "dir_scene", "parent_id": None, "level": 1,
     "dir_name": "微观语义标签", "dir_type": "dir_dynamic",
     "applicable_scope": "scene", "description": "具体情节事件与名场面",
     "aliases": []},
    {"dir_id": "dir_char", "parent_id": None, "level": 1,
     "dir_name": "角色标签", "dir_type": "dir_dynamic",
     "applicable_scope": "character", "description": "角色原型与人设",
     "aliases": []},
]

DEFAULT_TAGS = [
    {"tag_id": "tag_action", "dir_id": "dir_genre", "tag_name": "动作",
     "tag_type": "val_fixed", "description": "以打斗追逐为主要看点", "status": "active"},
    {"tag_id": "tag_drama", "dir_id": "dir_genre", "tag_name": "剧情",
     "tag_type": "val_fixed", "description": "以人物关系与情感为主线", "status": "active"},
    {"tag_id": "tag_epic", "dir_id": "dir_genre", "tag_name": "史诗",
     "tag_type": "val_generalized", "description": "宏大叙事与史诗气质", "status": "active"},
    {"tag_id": "tag_revenge", "dir_id": "dir_theme", "tag_name": "复仇",
     "tag_type": "val_fixed", "description": "以复仇为驱动", "status": "active"},
    {"tag_id": "tag_nonlinear", "dir_id": "dir_theme", "tag_name": "非线性叙事",
     "tag_type": "val_fixed", "description": "时间线打乱的叙事结构", "status": "active"},
    {"tag_id": "tag_twist", "dir_id": "dir_scene", "tag_name": "剧情反转",
     "tag_type": "val_fixed", "description": "关键时刻的情节反转", "status": "active"},
    {"tag_id": "tag_hero", "dir_id": "dir_char", "tag_name": "英雄",
     "tag_type": "val_fixed", "description": "承担主线使命的正派角色", "status": "active"},
]


def write_library(root: str | Path | None = None, dirs=None, tags=None, *,
                  version: str | None = None) -> Path:
    """把目录/标签写成 JSON 库（缺省用默认库），并同步已绑定的测试库。"""
    global _ROOT
    base = Path(root) if root is not None else library_root()
    base.mkdir(parents=True, exist_ok=True)
    _ROOT = base
    version = version or f"test-{next(_VERSION)}"
    (base / "dirs.json").write_text(json.dumps({
        "version": version, "source": "tests",
        "dirs": list(DEFAULT_DIRS if dirs is None else dirs),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (base / "tags.json").write_text(json.dumps({
        "version": version, "source": "tests",
        "tags": list(DEFAULT_TAGS if tags is None else tags),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    _clear_cache()
    for engine in list(_BOUND_ENGINES):
        seed_database(engine)
    return base


def bind_engine(engine) -> None:
    """把测试库引擎登记进来：之后每次 `write_library` 改写库时同步镜像入库。

    同时把引擎绑成 `tagplatform.library` 的"当前数据源"——标签库已改为
    直接读 tag_catalog / tag_define，测试里的 try_load_library() 因此读到
    的就是这套临时库。

    注意：这里**不**主动灌数据。默认库的灌入由需要它的测试显式调用
    `write_library(...)` 触发；不灌的话库里是空的，main 侧「空库」用例
    （治理/重嵌入/迁移）才不会被测试夹具的行数污染。
    """
    from tagplatform import library as library_mod

    if engine not in _BOUND_ENGINES:
        _BOUND_ENGINES.append(engine)
    library_mod.bind_engine(engine)


def unbind_engine(engine) -> None:
    """测试结束解绑（引擎即将 dispose，避免后续写库打到已关闭连接）。"""
    from tagplatform import library as library_mod

    try:
        _BOUND_ENGINES.remove(engine)
    except ValueError:
        pass
    library_mod.unbind_engine(engine)


def _current() -> tuple[list, list]:
    root = library_root()
    dirs = json.loads((root / "dirs.json").read_text(encoding="utf-8"))["dirs"]
    tags = json.loads((root / "tags.json").read_text(encoding="utf-8"))["tags"]
    return dirs, tags


def add_tags(tags: list[dict]) -> None:
    """向当前测试库追加标签（同名已存在则跳过），版本号自增以失效缓存。"""
    dirs, existing = _current()
    names = {t["tag_name"] for t in existing}
    merged = existing + [t for t in tags if t["tag_name"] not in names]
    write_library(library_root(), dirs=dirs, tags=merged)


def add_dirs(dirs: list[dict]) -> None:
    """向当前测试库追加目录（同 id 已存在则跳过）。"""
    existing, tags = _current()
    ids = {d["dir_id"] for d in existing}
    merged = existing + [d for d in dirs if d["dir_id"] not in ids]
    write_library(library_root(), dirs=merged, tags=tags)


def library_root() -> Path:
    """当前生效的标签库目录（首次访问时落到系统临时目录）。"""
    global _ROOT
    if _ROOT is None:
        _ROOT = Path(tempfile.mkdtemp(prefix="taglibrary-"))
    return _ROOT


def seed_database(engine) -> None:
    """把当前 JSON 标签库**整体镜像**进 `tag_catalog` / `tag_define`。

    标签库切到 v2 后这两张是正式表，`pre_asset_tag.tag_id` 与 `candidate_dir_id`
    的外键指向它们；测试库由 `Base.metadata.create_all` 建表、不会跑迁移，
    所以 fixture 建表后要按当前库补父行，否则生成/审核落库会撞外键。

    用"先清空再灌"的权威镜像：测试常在同一个库里换一整套标签（同名不同
    id），增量插入会撞 `ux_tag_define_dir_name`。清空时临时关掉外键检查，
    避免历史 pre 行挡住同步——测试里 DB 标签库本就应当与 JSON 库一致。
    """
    dirs, tags = _current()
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("DELETE FROM tag_define")
        cursor.execute("DELETE FROM tag_catalog")
        raw.commit()
        for d in dirs:
            cursor.execute(
                "INSERT INTO tag_catalog (dir_id, dir_name, dir_type, description,"
                " parent_id, aliases, applicable_scope) VALUES (?,?,?,?,?,?,?)",
                (
                    d["dir_id"],
                    d["dir_name"],
                    d.get("dir_type") or "dir_static",
                    d.get("description") or "",
                    d.get("parent_id"),
                    json.dumps(d["aliases"], ensure_ascii=False)
                    if d.get("aliases")
                    else None,
                    d.get("applicable_scope") or "content",
                ),
            )
        for t in tags:
            cursor.execute(
                "INSERT INTO tag_define (tag_id, tag_name, tag_type, description,"
                " dir_id, aliases, status) VALUES (?,?,?,?,?,?,?)",
                (
                    t["tag_id"],
                    t["tag_name"],
                    t.get("tag_type") or "val_fixed",
                    t.get("description") or "",
                    t["dir_id"],
                    json.dumps(t["aliases"], ensure_ascii=False)
                    if t.get("aliases")
                    else None,
                    _db_status(t.get("status")),
                ),
            )
        raw.commit()
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        raw.close()


def _db_status(status) -> str:
    """JSON 夹具沿用 v1 词汇 `deprecate`，`tag_define.status` 的 CHECK 只允许
    `deprecated`（main 的取值），入库时统一映射。"""
    value = str(status or "active").strip()
    return "deprecated" if value == "deprecate" else value
