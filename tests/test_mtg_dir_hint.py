"""逐目录开放生成的请求、滤除、聚类和挂载契约。"""
import json
from pathlib import Path

import pytest
from conftest import MOCK_EMBEDDER, MOCK_LLM

from tagplatform.capabilities.llm.client import LLMResponse
from tagplatform.services.generation import GenerationPolicy, GenerationSpec
from tagplatform.services.generation.open.mtg import pipeline
from tagplatform.services.generation.open.mtg.catalog_align import Alignment, BAND_NEW
from tagplatform.services.generation.open.producer import OpenTagProducer
from tagplatform.services.tags.library import try_load_library
from tagplatform.settings import Settings
from tests.library_fixture import add_dirs


def _scenes(content, count):
    template = content.content.scenes[0]
    scenes = [template.model_copy(update={
        "scene_id": f"scene_{index:03d}",
        "description": f"第{index}个场景的可核验原文描述",
    }) for index in range(1, count + 1)]
    return content.model_copy(update={"content": content.content.model_copy(update={"scenes": scenes})})


def _spec(content, run_id, policy=None):
    return GenerationSpec(
        task_id=f"t-{run_id}", asset_id=content.asset.asset_id,
        content=content, policy=policy or GenerationPolicy(), prompt_text="",
        run_id=run_id, library=try_load_library(),
    )


def _report(settings, run):
    return json.loads((Path(settings.mtg_report_dir) / f"t-{run}.json").read_text(encoding="utf-8"))


def _text(messages):
    return "\n".join(part["text"] for part in messages[0]["content"] if part["type"] == "text")


def _result(items):
    return LLMResponse(content=json.dumps(items, ensure_ascii=False), usage={}, raw={}, ok=True)


class _GenLLM:
    def __init__(self, items):
        self.items = items

    def complete(self, messages, run_id):
        return _result(self.items)


class _GenerationScript:
    def __init__(self, outputs):
        self.outputs = outputs
        self.generation_calls = []

    def complete(self, messages, run_id):
        if not isinstance(messages[0]["content"], list):
            return MOCK_LLM.complete(messages, run_id)
        key = run_id.rsplit(":", 1)[-1]
        self.generation_calls.append((key, messages))
        return _result(self.outputs(key, messages))


def _items(names, *, scenes=()):
    return [{"tag": name, "scenes": list(scenes), "reason": "生成理由"} for name in names]


@pytest.fixture
def dir_settings(tmp_path, monkeypatch):
    settings = Settings(use_mock_llm=True, mtg_report_dir=str(tmp_path / "audit"),
                        mtg_modalities="script,dialogue")
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    return settings


def test_directory_requests_and_film_context_before_clustering(
        session, content_fixture, dir_settings):
    add_dirs([{"dir_id": "dir_combined", "level": 1, "dir_name": "全片扩展目录",
               "dir_type": "dir_asset", "applicable_scope": "content",
               "description": "全片扩展目录的定义"}])
    content = _scenes(content_fixture, 3)
    ids = [scene.scene_id for scene in content.content.scenes]

    def output(key, messages):
        prompt = _text(messages)
        assert prompt.count("## 目标目录") == 1
        assert "主类型" not in prompt
        if key.startswith("scene-"):
            assert "最多 3 条" in prompt
            assert "适用范围" not in prompt and '"scope"' not in prompt
            assert "场景搜索" not in prompt and "全片级" not in prompt and "挂载粒度" not in prompt
            return _items(("贯穿线索", "AI", "ai", "超过单次上限"))
        assert "最多 5 条" in prompt
        part = messages[0]["content"][0]["text"]
        context = json.loads(part.partition("## 第一轮逐场候选\n")[2])
        assert [row["scene_id"] for row in context] == ids
        for row in context:
            repeated = [tag["dir"] for tag in row["tags"] if tag["tag"] == "贯穿线索"]
            assert set(repeated) == {"dir_combined", "dir_scene", "dir_theme", "dir_char"}
            assert len(row["tags"]) == 12
        assert all(scene.description in part for scene in content.content.scenes)
        group = "甲组" if "全片扩展目录" in prompt else "乙组"
        return _items([f"{group}全片词{i}" for i in range(6)], scenes=ids)

    llm = _GenerationScript(output)
    out = OpenTagProducer().produce(_spec(content, "directory"), session, llm, MOCK_EMBEDDER)
    report = _report(dir_settings, "directory")
    scene_run, film_run = report["runs"]
    assert [(r["calls"], r["items"]) for r in report["runs"]] == [(12, 36), (2, 10)]
    assert scene_run["after_exact_dedup"] == 12
    assert {r["dir_id"] for r in film_run["requests"]} == {"dir_theme", "dir_combined"}
    scene_rows = [row for row in report["merged"] if row["stage"] == 1]
    repeated = [row for row in scene_rows if row["tag"] == "贯穿线索"]
    assert len(repeated) == 4 and all(row["scenes"] == ids for row in repeated)
    assert {row["tag"] for row in scene_rows} == {"贯穿线索", "AI", "ai"}
    assert len({row["candidate_id"] for row in report["merged"]}) == 22
    assert len(report["verdicts"]) == 22 and report["judge_usage"]["calls"] == 2
    clustering = report["clustering"]
    assert clustering["target"] == 4 and clustering["input"] == 12
    # mock 对 AI 和 ai 编出相同向量，同名标签也跨目录进入同一个簇。
    assert sorted(len(row["members"]) for row in clustering["clusters"]) == [4, 8]
    assert sum(tag.level == "film" for tag in out) == 5
    assert sum(tag.level == "scene" for tag in out) == 2


def test_filter_rejects_out_of_directory_even_when_label_exists(
        session, content_fixture, dir_settings):
    class Judge:
        def complete(self, messages, run_id):
            rows = json.loads(MOCK_LLM.complete(messages, run_id).content)
            for row in rows:
                if row["tag"] == "复仇":
                    row["dir_fits"]["dir_theme"] = False
            return _result(rows)

    spec = _spec(content_fixture, "mismatch", GenerationPolicy(dir_ids=["dir_theme"]))
    llm = _GenerationScript(lambda key, messages: _items(("复仇", "名字合格")))
    out = OpenTagProducer().produce(spec, session, llm, MOCK_EMBEDDER, judge_client=Judge())
    assert [(tag.tag_name, tag.dir_id) for tag in out] == [("名字合格", "dir_theme")]
    report = _report(dir_settings, "mismatch")
    assert report["clustering"]["input"] == 1
    assert [row["tag"] for row in report["library"]["align"]] == ["名字合格"]
    assert [(row["tag"], row["why"]) for row in report["dropped"]] == [
        ("复仇", "标签含义不属于目标目录")]
    assert report["accepted"][0]["dir_source"] == "request"


def test_film_priority_and_collision_backfill(session, content_fixture, dir_settings):
    content = _scenes(content_fixture, 3)
    ids = [scene.scene_id for scene in content.content.scenes]

    def output(key, messages):
        if key.startswith("film-"):
            return _items(("共同标签", "全片主题"), scenes=ids)
        if "细分题材" in _text(messages):
            return _items(("共同标签", "逐场备选"))
        return []

    spec = _spec(content, "collision", GenerationPolicy(max_tags=3))
    out = OpenTagProducer().produce(spec, session, _GenerationScript(output), MOCK_EMBEDDER)
    assert {(tag.tag_name, tag.level) for tag in out} == {
        ("共同标签", "film"), ("全片主题", "film"), ("逐场备选", "scene"),
    }
    report = _report(dir_settings, "collision")
    assert len(report["clustering"]["clusters"]) == 2
    drop = next(row for row in report["dropped"] if row["tag"] == "共同标签")
    assert drop["stage"] == 1 and "同一挂载点" in drop["why"]


def test_second_stage_uses_value_then_minimum_similarity(
        session, content_fixture, dir_settings, monkeypatch):
    content = _scenes(content_fixture, 2)
    names = ("高余弦高价值", "低余弦高价值", "最低余弦低价值")
    ids = [scene.scene_id for scene in content.content.scenes]
    llm = _GenerationScript(lambda key, messages: _items(names, scenes=ids)
                            if key.startswith("film-") else [])
    cosines = dict(zip(names, (0.72, 0.45, 0.21)))

    class Judge:
        def complete(self, messages, run_id):
            rows = json.loads(MOCK_LLM.complete(messages, run_id).content)
            for row in rows:
                row["value"] = 2 if row["tag"] == names[2] else 4
            return _result(rows)

    monkeypatch.setattr(pipeline, "align_candidates", lambda library, embedder, names, **kwargs: {
        name: Alignment(BAND_NEW, "测试相似度", cosine=cosines[name]) for name in names})
    spec = _spec(content, "sort", GenerationPolicy(dir_ids=["dir_theme"], max_tags=2))
    out = OpenTagProducer().produce(spec, session, llm, MOCK_EMBEDDER, judge_client=Judge())
    assert [tag.tag_name for tag in out] == [names[1], names[0]]
    assert _report(dir_settings, "sort")["library"]["truncated"][0]["tag"] == names[2]


def test_scene_quota_caps_at_ten(session, content_fixture, dir_settings):
    content = _scenes(content_fixture, 12)

    def output(key, messages):
        if key.startswith("scene-") and "细分题材" in _text(messages):
            return _items((f"现象{key.split('-')[1]}",))
        return []

    out = OpenTagProducer().produce(_spec(content, "cap"), session,
                                    _GenerationScript(output), MOCK_EMBEDDER)
    assert len(out) == 10 and all(tag.level == "scene" for tag in out)
    report = _report(dir_settings, "cap")
    assert len(report["clustering"]["clusters"]) == 12
    assert len(report["library"]["truncated"]) == 2


def test_scene_only_directory_has_no_film_request(session, content_fixture, dir_settings):
    content = _scenes(content_fixture, 2)
    spec = _spec(content, "scene-only", GenerationPolicy(dir_ids=["dir_scene"]))
    llm = _GenerationScript(lambda key, messages: _items(("开场对峙",)))
    out = OpenTagProducer().produce(spec, session, llm, MOCK_EMBEDDER)
    assert len(out) == 1 and out[0].target_type == "scene"
    assert len(llm.generation_calls) == 2
    assert [run["scope"] for run in _report(dir_settings, "scene-only")["runs"]] == ["scene"]


def test_static_scope_skips_open_generation(
        session, content_fixture, dir_settings):
    """范围内仅有静态目录（无可扩充目录）→ 开放生成直接返回空，不调用 LLM。"""
    content = _scenes(content_fixture, 2)
    spec = _spec(content, "static", GenerationPolicy(dir_ids=["dir_genre"]))
    messages_by_run = {}

    class RecordingLLM(_GenLLM):
        def complete(self, messages, run_id):
            messages_by_run[run_id] = messages
            return super().complete(messages, run_id)

    assert OpenTagProducer().produce(
        spec, session, RecordingLLM([]), MOCK_EMBEDDER) == []
    assert messages_by_run == {}


def test_cluster_representative_is_nearest_to_center():
    import numpy as np

    from tagplatform.services.generation.open.mtg.directory_candidates import cluster_scene_candidates
    from tagplatform.services.generation.open.mtg.merge import MergedCandidate

    class Embedder:
        def embed(self, names, *, role):
            assert names == ["第一个", "最居中", "第三个"]
            assert role == "query"
            angles = np.radians([0, 15, 65])
            return np.column_stack((np.cos(angles), np.sin(angles)))

    names = ["第一个", "最居中", "第三个", "全片词"]
    rows = [
        (MergedCandidate(name, None, (), 1, (name,), "", scope="scene" if i < 3 else "film",
                         candidate_id=str(i)), {"value": 2 if i == 1 else 4}, [])
        for i, name in enumerate(names)
    ]
    kept, audit, _ = cluster_scene_candidates(rows, Embedder(), scene_count=2, directory_count=2)
    assert [row[0].tag for row in kept] == ["最居中", "全片词"]
    assert audit["clusters"] == [{"representative": "1", "members": ["0", "1", "2"]}]


def test_same_name_in_two_mounted_directories_gets_its_own_definition(
        session, content_fixture, dir_settings):
    content = _scenes(content_fixture, 2)
    scene_ids = [scene.scene_id for scene in content.content.scenes]
    descriptions = {
        "dir_theme": "以长久延宕的仇怨推动情节发展的叙事母题",
        "dir_scene": "以冷色光线和低饱和画面形成疏离感的视觉处理",
    }

    class LLM(_GenerationScript):
        def complete(self, messages, run_id):
            if run_id.endswith(":desc"):
                text = messages[0]["content"]
                directory = "dir_scene" if "微观语义标签" in text else "dir_theme"
                return _result({"items": [{"tag": "边界感", "desc": descriptions[directory]}]})
            return super().complete(messages, run_id)

    def output(key, messages):
        if key.startswith("film-"):
            return _items(("边界感",), scenes=scene_ids)
        if key.startswith("scene-001") and "微观语义标签" in _text(messages):
            return _items(("边界感",))
        return []

    spec = _spec(content, "descriptions", GenerationPolicy(dir_ids=["dir_theme", "dir_scene"]))
    out = OpenTagProducer().produce(spec, session, LLM(output), MOCK_EMBEDDER)
    assert {tag.target_type for tag in out} == {"content", "scene"}
    assert {tag.dir_id: tag.candidate_description for tag in out} == descriptions
    assert _report(dir_settings, "descriptions")["descriptions"]["filled"] == 2
