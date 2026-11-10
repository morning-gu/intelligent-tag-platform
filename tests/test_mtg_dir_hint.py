"""生成阶段就把目录交给模型：提示注入 + 归属校验 + 回退。

口径（2026-11-09 确认）：把可扩展目录清单给 LLM，让它在生成时就决定
"在某目录下新增某标签"；校验不过（目录不存在 / 静态目录 / 挂载点解析不出）
才回退到"按目录描述向量归类"，并且**不静默改写**模型的原始选择——审计里保留
`dir_model_raw` 与 `dir_source`，审核页能看出这条是模型选的还是兜底算的。
"""
import json

import pytest
from conftest import MOCK_EMBEDDER

from tagplatform.generation import GenerationPolicy, GenerationSpec
from tagplatform.generation.mtg import pipeline as mtg_pipeline
from tagplatform.generation.mtg.merge import merge_candidates
from tagplatform.generation.mtg.parse import parse_generation_items
from tagplatform.generation.mtg.prompts import build_generation_messages
from tagplatform.generation.open_gen import OpenTagProducer
from tagplatform.llm.client import LLMResponse
from tagplatform.library import try_load_library
from tagplatform.schemas import parse_content
from tagplatform.settings import Settings


def _two_scene_content(content_fixture):
    """复制成两个场景的资产（全片级标签要求 ≥2 场景支撑）。"""
    data = json.loads(content_fixture.model_dump_json())
    scene = json.loads(json.dumps(data["content"]["scenes"][0]))
    scene["scene_id"] = "scene_002"
    data["content"]["scenes"].append(scene)
    return parse_content(data)


class _GenLLM:
    """固定返回一批候选（可带 dir 字段）的生成桩。"""

    def __init__(self, items):
        self.items = items

    def complete(self, messages, run_id):
        return LLMResponse(content=json.dumps({"tags": self.items}, ensure_ascii=False),
                           usage={"prompt_tokens": 100, "completion_tokens": 20},
                           raw={}, ok=True)


@pytest.fixture
def dir_settings(tmp_path, monkeypatch):
    """judge 关掉（本文件只测目录归属），报告落临时目录。"""
    settings = Settings(
        use_mock_llm=True,
        mtg_judge_enabled=False,
        dataset_root=str(tmp_path / "movienet"),
        mtg_report_dir=str(tmp_path / "audit"),
    )
    monkeypatch.setattr(mtg_pipeline, "get_settings", lambda: settings)
    return settings


def _spec(content, run_id: str, policy=None) -> GenerationSpec:
    return GenerationSpec(
        task_id=f"t-{run_id}", asset_id=content.asset.asset_id,
        content=content, policy=policy or GenerationPolicy(),
        prompt_text="", run_id=run_id, library=try_load_library(),
    )


def _prompt(content, entries) -> str:
    messages = build_generation_messages(content, {}, dir_hint=entries)
    return "".join(part["text"] for part in messages[0]["content"]
                   if part["type"] == "text")


# ---- 提示词内容 ----


def test_dir_hint_lists_only_expandable_dirs_with_full_path(content_fixture):
    """只列 dir_dynamic；路径给全层级，而不是末级名字。"""
    library = try_load_library()
    entries = mtg_pipeline._dir_hint_entries(
        library, _spec(content_fixture, "h1"), 60)

    ids = {entry["dir_id"] for entry in entries}
    assert ids == {"dir_theme", "dir_scene", "dir_char"}
    assert "dir_genre" not in ids  # 静态目录不允许新增，不该出现在提示词里

    theme = next(entry for entry in entries if entry["dir_id"] == "dir_theme")
    assert theme["path"] == "内容特征标签/细分题材"
    assert theme["scope"] == "content"


def test_dir_hint_follows_policy_range(content_fixture):
    """策略限定目录范围时，清单只留范围内的可扩展目录。"""
    library = try_load_library()
    spec = _spec(content_fixture, "h2",
                 GenerationPolicy(dir_ids=["dir_scene"]))

    entries = mtg_pipeline._dir_hint_entries(library, spec, 60)

    assert [entry["dir_id"] for entry in entries] == ["dir_scene"]


def test_prompt_carries_dir_block_and_dir_field(content_fixture):
    """有目录时：出现目录块 + 输出契约要求 dir 字段。"""
    library = try_load_library()
    entries = mtg_pipeline._dir_hint_entries(
        library, _spec(content_fixture, "h3"), 60)

    text = _prompt(content_fixture, entries)

    assert "## 可以放新标签的目录" in text
    assert "- dir_theme｜内容特征标签/细分题材｜整片" in text
    assert '"dir":"目录 id"' in text
    assert "找不到合适目录的标签直接不要输出" in text


def test_prompt_without_dirs_keeps_old_contract(content_fixture):
    """没有可扩展目录时（或开关关掉）：不出现目录块，契约保持 v2 形状。"""
    text = _prompt(content_fixture, [])

    assert "## 可以放新标签的目录" not in text
    assert '"dir"' not in text


# ---- 解析 / 合并 ----


def test_parse_reads_dir_field():
    items = parse_generation_items(json.dumps({"tags": [
        {"tag": "命运羁绊", "dir": "dir_theme", "scenes": ["scene_001"]},
        {"tag": "光影运用", "dir_id": "dir_scene"},
        {"tag": "无声留白"},
    ]}, ensure_ascii=False))

    assert [item["dir"] for item in items] == ["dir_theme", "dir_scene", None]


def test_merge_takes_majority_dir():
    """同名候选跨轮出现时按多数派取目录，并列时取字典序最小（稳定）。"""
    merged = merge_candidates([
        {"tag": "命运羁绊", "dir": "dir_theme", "run": 0},
        {"tag": "命运羁绊", "dir": "dir_theme", "run": 1},
        {"tag": "命运羁绊", "dir": "dir_scene", "run": 2},
    ], MOCK_EMBEDDER, 0.75)

    assert merged[0].dir_raw == "dir_theme"


# ---- 目录解析 ----


@pytest.mark.parametrize("raw,expected", [
    ("dir_theme", "dir_theme"),                      # id
    ("内容特征标签/细分题材", "dir_theme"),              # 全路径
    ("细分题材", "dir_theme"),                         # 唯一目录名
    ("dir_ghost", None),                             # 不存在
    ("", None),
])
def test_resolve_dir_value(content_fixture, raw, expected):
    library = try_load_library()
    spec = _spec(content_fixture, "rsv")

    assert mtg_pipeline._resolve_dir_value(library, raw) == expected
    assert spec is not None


# ---- 流水线：模型给的目录被采用 / 被校验打回 ----


def _run(session, content, run_id, items, **settings_overrides):
    spec = _spec(content, run_id)
    out = OpenTagProducer().produce(spec, session, _GenLLM(items), MOCK_EMBEDDER)
    return out


def _report(tmp_path, run_id):
    return json.loads((tmp_path / "audit" / f"t-{run_id}.json").read_text(encoding="utf-8"))


_ITEM = {"tag": "命运羁绊", "dimension": "主题", "scope": "film",
         "scenes": ["scene_001", "scene_002"], "reason": "两条场景都指向同一困境"}


def test_model_chosen_dir_is_used(session, content_fixture, dir_settings, tmp_path):
    """模型给了合法目录 → 直接用它，审计记 dir_source=model。"""
    content = _two_scene_content(content_fixture)

    out = _run(session, content, "d1", [{**_ITEM, "dir": "dir_theme"}])

    assert [tag.dir_id for tag in out] == ["dir_theme"]
    accepted = _report(tmp_path, "d1")["accepted"][0]
    assert accepted["dir_source"] == "model"
    assert accepted["dir_id"] == "dir_theme"


def test_static_dir_is_rejected_and_falls_back(session, content_fixture,
                                               dir_settings, tmp_path):
    """静态目录不允许新增 → 打回，回退向量归类；原始选择留在审计里。"""
    content = _two_scene_content(content_fixture)

    out = _run(session, content, "d2", [{**_ITEM, "dir": "dir_genre"}])

    assert len(out) == 1
    assert out[0].dir_id != "dir_genre"
    accepted = _report(tmp_path, "d2")["accepted"][0]
    assert accepted["dir_source"] == "fallback"
    assert accepted["dir_model_raw"] == "dir_genre"


def test_unknown_dir_falls_back(session, content_fixture, dir_settings, tmp_path):
    """模型编了一个不存在的目录 id → 回退，不写进 candidate_dir_id。"""
    content = _two_scene_content(content_fixture)

    out = _run(session, content, "d3", [{**_ITEM, "dir": "dir_ghost"}])

    assert len(out) == 1
    accepted = _report(tmp_path, "d3")["accepted"][0]
    assert accepted["dir_source"] == "fallback"
    assert accepted["dir_model_raw"] == "dir_ghost"
    assert accepted["dir_id"] in {"dir_theme", "dir_scene", "dir_char"}


def test_character_dir_without_unique_character_falls_back(
        session, content_fixture, dir_settings, tmp_path):
    """人物级目录挂不上（资产里没有唯一角色）→ 回退到能挂的目录。"""
    content = _two_scene_content(content_fixture)  # characters 为空

    out = _run(session, content, "d4", [{**_ITEM, "dir": "dir_char"}])

    assert len(out) == 1
    assert out[0].target_type != "character"
    assert _report(tmp_path, "d4")["accepted"][0]["dir_source"] == "fallback"


def test_dir_injection_can_be_disabled(session, content_fixture, tmp_path,
                                       monkeypatch):
    """open_dir_hint_max=0 → 不注入目录（回到"生成后再归类"），并仍能产出。"""
    settings = Settings(
        use_mock_llm=True, mtg_judge_enabled=False, open_dir_hint_max=0,
        dataset_root=str(tmp_path / "movienet"),
        mtg_report_dir=str(tmp_path / "audit"),
    )
    monkeypatch.setattr(mtg_pipeline, "get_settings", lambda: settings)
    content = _two_scene_content(content_fixture)

    out = _run(session, content, "d5", [{**_ITEM, "dir": "dir_theme"}])

    assert len(out) == 1
    # 提示词里没有目录 → 模型自报的 dir 无人校验来源，仍按"模型给的"处理
    assert _report(tmp_path, "d5")["params"]["dir_hint_count"] == 0
