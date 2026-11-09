"""标签库（JSON 模拟）与入库预览：目录语义、范围过滤、挂载粒度、幂等。

覆盖契约：
- 目录树：父子关系、path_of 全路径、applicable_scope 沿父链继承、
  dir_dynamic 才允许扩充；
- 标签：tags_in_dirs 含子目录、空 dir_ids = 全库；
- assign_dir：只在可扩展目录里挑，按描述相似度排序，返回多条候选；
- resolve_mount：content → asset_id；scene → 首个真实 scene_id；
  character → 恰好一个 character_id 才挂（0/2 个都返回 None）；
- ingest：新词按 (dir_id, tag_name) 幂等、别名按 (tag_id, alias) 幂等，
  行形状对齐 tag_define（tag_id/dir_id/tag_name/tag_type/description/
  aliases/status/source）；
- API：/library/stats、/library/tree、/library/tags、/ingest/preview。
"""

import json

import numpy as np
import pytest
from conftest import MOCK_EMBEDDER

from tagplatform.api import get_session  # noqa: F401  (确保 API 包可导入)
from tagplatform.ingest import record_alias, record_tag_definition
from tagplatform.library import (
    DIR_DYNAMIC,
    DIR_STATIC,
    SCOPE_CHARACTER,
    SCOPE_CONTENT,
    SCOPE_SCENE,
    TagLibrary,
    assign_dir,
    load_library,
    rank_dir_assignments,
    try_load_library,
)
from tagplatform.mounting import Mount, resolve_mount
from tagplatform.schemas import parse_content
from tagplatform.settings import get_settings
from tests.library_fixture import add_dirs, add_tags, write_library


# ---------------------------------------------------------------- 目录 / 标签


def test_load_library_reads_dirs_and_tags():
    library = try_load_library()
    assert library is not None
    assert len(library.dirs()) == 5 and len(library.tags()) == 7
    assert library.directory("dir_theme").dir_name == "细分题材"
    assert library.tag("tag_revenge").tag_name == "复仇"


def test_path_of_walks_up_to_root():
    library = try_load_library()
    assert library.path_of("dir_genre") == ["内容特征标签", "主类型"]
    assert library.path_of("dir_content") == ["内容特征标签"]
    assert library.path_of("dir_unknown") == []


def test_scope_inherits_from_parent_and_defaults_to_content():
    add_dirs([{"dir_id": "dir_genre_child", "parent_id": "dir_genre",
               "level": 3, "dir_name": "子类型", "dir_type": DIR_STATIC,
               "applicable_scope": SCOPE_CONTENT, "description": "", "aliases": []}])
    library = try_load_library()
    assert library.scope_of("dir_scene") == SCOPE_SCENE
    assert library.scope_of("dir_char") == SCOPE_CHARACTER
    assert library.scope_of("dir_genre_child") == SCOPE_CONTENT
    assert library.scope_of("dir_missing") == SCOPE_CONTENT


def test_only_dynamic_dirs_allow_new_tags():
    library = try_load_library()
    assert library.allows_new_tags("dir_theme")
    assert not library.allows_new_tags("dir_genre")  # dir_static
    assert not library.allows_new_tags(None)
    assert {d.dir_id for d in library.new_tag_dirs(None)} == {
        "dir_theme", "dir_scene", "dir_char"}


def test_tags_in_dirs_includes_descendants_and_empty_means_all():
    library = try_load_library()
    in_content = {t.tag_id for t in library.tags_in_dirs(["dir_content"])}
    # dir_content → dir_genre + dir_theme
    assert in_content == {"tag_action", "tag_drama", "tag_epic",
                          "tag_revenge", "tag_nonlinear"}
    assert len(library.tags_in_dirs(None)) == len(library.tags())
    assert library.tags_in_dirs(["dir_unknown"]) == []


def test_descendants_ignores_cycles():
    from tagplatform.library import Directory

    cyclic = TagLibrary(
        [Directory(dir_id="a", dir_name="A", parent_id="b"),
         Directory(dir_id="b", dir_name="B", parent_id="a")], [])
    assert cyclic.descendants(["a"]) == {"a", "b"}


# ---------------------------------------------------------------- 目录归属


def test_assign_dir_only_considers_dynamic_dirs():
    library = try_load_library()
    vec = np.asarray(MOCK_EMBEDDER.embed(["复仇"], role="query")[0],
                     dtype=np.float32)
    assignment = assign_dir(library, MOCK_EMBEDDER, vec)
    assert assignment.dir_id in {"dir_theme", "dir_scene", "dir_char"}
    assert assignment.dir_name and assignment.reason


def test_assign_dir_respects_dir_scope_and_returns_ranked():
    library = try_load_library()
    vec = np.asarray(MOCK_EMBEDDER.embed(["英雄"], role="query")[0],
                     dtype=np.float32)
    ranked = rank_dir_assignments(library, MOCK_EMBEDDER, vec,
                                  dir_ids=["dir_scene", "dir_char"], top_n=3)
    assert {a.dir_id for a in ranked} <= {"dir_scene", "dir_char"}
    scores = [a.score for a in ranked]
    assert scores == sorted(scores, reverse=True)


def test_assign_dir_without_dynamic_dirs_returns_none():
    library = try_load_library()
    vec = np.asarray(MOCK_EMBEDDER.embed(["复仇"], role="query")[0],
                     dtype=np.float32)
    assignment = assign_dir(library, MOCK_EMBEDDER, vec, dir_ids=["dir_genre"])
    assert assignment.dir_id is None and "没有可扩展目录" in assignment.reason


def test_dir_vectors_include_existing_tag_names():
    """目录向量文本带上已收录标签名：只有目录名时归属判断太弱。"""
    library = try_load_library()
    seen: list[list[str]] = []

    class RecordingEmbedder:
        name = "recording"

        def embed(self, texts, *, role):
            assert role == "passage"
            seen.append(list(texts))
            return np.asarray(MOCK_EMBEDDER.embed(texts, role=role))

    from tagplatform.library import dir_vectors

    ids, matrix = dir_vectors(library, RecordingEmbedder(), ["dir_theme"])
    assert ids == ["dir_theme"] and matrix.shape[0] == 1
    text = seen[-1][0]
    assert text.startswith("细分题材")
    assert "复仇" in text and "非线性叙事" in text
    # 静态目录不参与归属，文本里不会出现它收录的标签
    assert "动作" not in text


def test_dir_text_without_samples_keeps_name_and_description():
    from tagplatform.library import Directory, dir_text

    directory = Directory(dir_id="d1", dir_name="情绪基调", description="整体氛围")
    assert dir_text(directory) == "情绪基调。整体氛围"
    assert dir_text(directory, ["压抑"]) == "情绪基调。整体氛围。已收录：压抑"


# ---------------------------------------------------------------- 挂载点


def _content():
    data = json.loads(
        (__import__("pathlib").Path(__file__).parent / "fixtures"
         / "content_movie.json").read_text(encoding="utf-8"))
    return parse_content(data)


def test_mount_content_uses_asset_id():
    content = _content()
    mount = resolve_mount(SCOPE_CONTENT, content.asset.asset_id, content, [])
    assert mount == Mount("content", content.asset.asset_id)


def test_mount_scene_uses_first_real_scene():
    content = _content()
    scene_id = content.content.scenes[0].scene_id
    from tagplatform.generation.base import Evidence

    mount = resolve_mount(SCOPE_SCENE, content.asset.asset_id, content,
                          [Evidence(scene_id, 0, 1000, "证据")])
    assert mount == Mount("scene", scene_id)


def test_mount_scene_without_evidence_returns_none():
    content = _content()
    assert resolve_mount(SCOPE_SCENE, content.asset.asset_id, content, []) is None


def test_mount_character_needs_exactly_one_character():
    data = json.loads(
        (__import__("pathlib").Path(__file__).parent / "fixtures"
         / "content_movie.json").read_text(encoding="utf-8"))
    data.setdefault("content", {})["characters"] = [
        {"character_id": "char_001", "name": "甲"},
    ]
    data["content"]["scenes"] = [{
        "scene_id": "scene_001",
        "time_range": {"start_ms": 0, "end_ms": 1000},
        "description": "两人对峙",
        "character_ids": ["char_001"],
    }]
    content = parse_content(data)
    from tagplatform.generation.base import Evidence

    evidence = [Evidence("scene_001", 0, 1000, "对峙")]
    assert resolve_mount(SCOPE_CHARACTER, content.asset.asset_id, content,
                         evidence) == Mount("character", "char_001")
    # 场景里没有角色 → 解析不出唯一角色
    content.content.scenes[0].character_ids = []
    assert resolve_mount(SCOPE_CHARACTER, content.asset.asset_id, content,
                         evidence) is None


# ---------------------------------------------------------------- 入库（写 tag_define）


def test_record_tag_definition_writes_row_and_is_idempotent(session):
    """新词入库：插一行 tag_define + 一条 create 日志；同目录同名不重复插。"""
    from tagplatform.db.models import TagChangeLog, TagDefine

    library = try_load_library(session)
    first = record_tag_definition(
        session, library, tag_name="时间循环", dir_id="dir_theme",
        description="同一段时间反复重来",
        source={"asset_id": "a1", "reviewer": "alice"})
    session.commit()
    again = record_tag_definition(session, library, tag_name="时间循环",
                                  dir_id="dir_theme", description="重复登记不应新增")
    assert first["tag_id"] == again["tag_id"] and again["created"] is False
    rows = session.query(TagDefine).filter_by(tag_id=first["tag_id"]).all()
    assert len(rows) == 1
    assert set(first) >= {"tag_id", "dir_id", "dir_name", "tag_name", "tag_type",
                          "description", "aliases", "status", "created", "source"}
    assert first["tag_id"].startswith("tag_") and len(first["tag_id"]) == 12
    assert first["dir_name"] == "细分题材" and first["status"] == "active"
    log = session.query(TagChangeLog).filter_by(
        tag_id=first["tag_id"], action="create").one()
    assert log.operator == "alice"
    # 库里真正多了一条：再载入标签库能看到
    assert try_load_library(session).tag(first["tag_id"]).tag_name == "时间循环"


def test_record_tag_definition_rejects_unknown_dir(session):
    library = try_load_library(session)
    with pytest.raises(ValueError, match="目录不存在"):
        record_tag_definition(session, library, tag_name="x", dir_id="dir_nope")


def test_record_tag_definition_rejects_blank_name(session):
    library = try_load_library(session)
    with pytest.raises(ValueError, match="标签名不能为空"):
        record_tag_definition(session, library, tag_name="  ", dir_id="dir_theme")


def test_record_alias_appends_and_dedupes(session):
    """别名入库：追加进 tag_define.aliases，重复返回 False，记 update 日志。"""
    from tagplatform.db.models import TagChangeLog, TagDefine

    assert record_alias(session, tag_id="tag_revenge", alias="报仇") is True
    assert record_alias(session, tag_id="tag_revenge", alias="报仇") is False
    session.commit()
    row = session.get(TagDefine, "tag_revenge")
    assert "报仇" in (row.aliases or "")
    log = session.query(TagChangeLog).filter_by(
        tag_id="tag_revenge", action="update").one()
    assert "报仇" in (log.after_json or "")
    with pytest.raises(ValueError, match="标签不存在"):
        record_alias(session, tag_id="tag_nope", alias="x")


# ---------------------------------------------------------------- API


def test_library_stats_api(client):
    body = client.get("/api/v1/library/stats").json()
    assert body["dirs"] == 5 and body["tags"] == 7
    assert body["dynamic_dirs"] == 3
    assert body["scopes"] == {"content": 3, "scene": 1, "character": 1}
    assert body["tag_types"] == {"val_fixed": 6, "val_generalized": 1}


def test_library_tree_api(client):
    body = client.get("/api/v1/library/tree").json()
    assert [node["dir_id"] for node in body["dirs"]] == [
        "dir_content", "dir_scene", "dir_char"]
    content = body["dirs"][0]
    assert [child["dir_id"] for child in content["children"]] == [
        "dir_genre", "dir_theme"]
    assert content["children"][0]["path"] == "内容特征标签/主类型"
    assert content["children"][0]["tag_count"] == 3
    assert content["allows_new_tags"] is False
    assert body["dirs"][1]["allows_new_tags"] is True


def test_library_tags_api_filters(client):
    body = client.get("/api/v1/library/tags", params={"dir_id": "dir_theme"}).json()
    assert body["total"] == 2
    assert {row["tag_name"] for row in body["items"]} == {"复仇", "非线性叙事"}
    assert all(row["path"] == "内容特征标签/细分题材" for row in body["items"])

    body = client.get("/api/v1/library/tags", params={"query": "复仇"}).json()
    assert body["total"] == 1 and body["items"][0]["tag_id"] == "tag_revenge"


def test_ingest_preview_api(client):
    """审核通过新词 + 合并别名 → /ingest/preview 反映最近入库记录。"""
    from tagplatform.db.models import MediaAsset, PreAssetTag

    factory = client.app.state.session_factory
    with factory() as s:
        s.add(MediaAsset(asset_id="a1", asset_type="movie", title="测试影片"))
        s.flush()
        new_word = PreAssetTag(asset_id="a1", target_type="content",
                               target_id="a1", candidate_name="时间循环",
                               candidate_dir_id="dir_theme", origin="open",
                               review_status="pending")
        merger = PreAssetTag(asset_id="a1", target_type="content",
                             target_id="a1", candidate_name="报仇",
                             candidate_dir_id="dir_theme", origin="open",
                             review_status="pending")
        s.add_all([new_word, merger])
        s.commit()
        new_id, merge_id = new_word.id, merger.id

    assert client.post(f"/api/v1/asset-tags/{new_id}/review",
                       json={"action": "approve"}).status_code == 200
    assert client.post(f"/api/v1/asset-tags/{merge_id}/review",
                       json={"action": "merge",
                             "payload": {"merge_to_tag_id": "tag_revenge"}}
                       ).status_code == 200

    body = client.get("/api/v1/ingest/preview").json()
    assert body["tag_define_count"] == 1 and body["alias_count"] == 1
    assert body["tag_define"][0]["tag_name"] == "时间循环"
    assert body["aliases"][0]["alias"] == "报仇"
