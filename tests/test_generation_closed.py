"""closed 生成器测试：按目录分组召回、输出解析与 ClosedTagProducer（v2 Task 6）。

覆盖契约：
- embed_content_texts：[剧情摘要, 各场景描述] 查询文本列表（函数不变）；
- recall_by_dir：deprecated/无向量行排除、按 dir_id 分组各取 top_k、组内
  跨 query 取最高分、跨组按 tag_id 去重、空库返回 []；
- _render_candidates_text：候选行 4 字段格式逐字保留（closed 提示词模板
  冻结、MockLLM 解析契约依赖），第三字段 = 候选标签的父目录名；
- parse_closed_output：键形与分值域校验、坏结构抛 LLMParseError（消息含
  首个坏元素位置）；
- ClosedTagProducer：空候选 ValueError、produce 流程（召回 → 空检查 →
  LLM → 解析 → 过滤截断 → origin="closed" 映射），纯内存输出不写库。

Mock 技巧（与 mock.py 咬合）：
- MockEmbedder 哈希即向量、同形文本同向量——注意 _canonical 丢弃标点
  **不补空格**：播种文本 f"{name}。{desc}" 的真实 canonical 形为
  "{name}{desc}" 直连（如 "悬疑推理"），query 取该形可精确控制余弦=1.0；
- MockLLM 选中（sha256(run_id+tag_id) < 0.4）与打分哈希独立——run_id="r2"
  对 tag_001 必选中且 confidence≈0.856 ≥ 0.7，防过滤后空转（false-green）。
"""

import json

import pytest
from conftest import MOCK_EMBEDDER, MOCK_LLM
from sqlalchemy.orm import Session

from tagplatform.db.models import TagCatalog, TagDefine
from tagplatform.generation import GenerationPolicy, GenerationSpec
from tagplatform.generation.closed import ClosedTagProducer
from tagplatform.generation.recall import embed_content_texts, recall_by_dir
from tagplatform.llm.parsing import (
    LLMParseError,
    parse_closed_output,
)
from tagplatform.prompts import get_active_prompt, render_prompt
from tagplatform.tasks.runner import _render_candidates_text
from tagplatform.vectors import encode_vector

# 2 个目录 × 各 3 条：tag_001 文本 "悬疑。推理" 的真实 canonical 形为
# "悬疑推理"（_canonical 丢标点不补空格），与召回 query 同形（余弦=1.0），
# 命中为构造保证而非哈希运气
_TAGS = [
    ("tag_001", "悬疑", "dir_g", "推理"),
    ("tag_002", "剧情", "dir_g", "人物关系驱动"),
    ("tag_003", "动作", "dir_g", "追逐打斗"),
    ("tag_004", "紧张", "dir_m", "情绪紧绷"),
    ("tag_005", "温馨", "dir_m", "柔和治愈"),
    ("tag_006", "压抑", "dir_m", "低沉沉重"),
]
_DIR_NAMES = {"dir_g": "类型目录", "dir_m": "情绪目录"}


def _seed_catalog(session: Session, n: int = 6) -> None:
    """播种 2 目录 + n 枚标签：embedding 由 MockEmbedder 对 "名称。描述" 现算。"""
    session.add(TagCatalog(dir_id="dir_g", dir_name=_DIR_NAMES["dir_g"],
                           dir_type="dir_static", description="题材类目"))
    session.add(TagCatalog(dir_id="dir_m", dir_name=_DIR_NAMES["dir_m"],
                           dir_type="dir_static", description="情绪类目"))
    session.flush()  # 目录（外键父行）先落库
    for tag_id, name, dir_id, desc in _TAGS[:n]:
        vec = MOCK_EMBEDDER.embed([f"{name}。{desc}"], role="passage")[0]
        session.add(TagDefine(
            tag_id=tag_id, tag_name=name, tag_type="val_fixed",
            description=desc, dir_id=dir_id, status="active",
            embedding=encode_vector(vec),
        ))
    session.commit()


# ---- embed_content_texts ----


def test_embed_content_texts(content_fixture):
    queries = embed_content_texts(content_fixture)
    assert queries[0] == content_fixture.content.summary
    assert queries[1:] == [s.description for s in content_fixture.content.scenes]


# ---- recall_by_dir：按目录分组 / 去重 / 排除 ----


def test_closed_recall_groups_and_dedup(session):
    _seed_catalog(session)
    # query 取播种文本的真实 canonical 形（"悬疑。推理" → "悬疑推理"）：
    # tag_001 余弦恰为 1.0，构造上必为 dir_g 组最高分命中
    cats = recall_by_dir(session, ["悬疑推理"], MOCK_EMBEDDER, top_k=2)
    # 每目录 top-2 × 2 目录 = 4，跨组按 tag_id 去重
    assert len({c.tag_id for c in cats}) == len(cats) == 4
    assert all(c.dir_id in {"dir_g", "dir_m"} for c in cats)
    assert any(c.tag_id == "tag_001" for c in cats)


def test_closed_recall_top_k_per_dir(session):
    """top_k 作用于每个目录组（v1 按 tag_type 分组的 v2 映射）。"""
    _seed_catalog(session)
    cats = recall_by_dir(session, ["悬疑推理"], MOCK_EMBEDDER, top_k=1)
    assert len(cats) == 2  # 每目录各取 1
    assert {c.dir_id for c in cats} == {"dir_g", "dir_m"}


def test_closed_recall_empty_catalog(session):
    assert recall_by_dir(session, ["任何"], MOCK_EMBEDDER, top_k=5) == []


def test_closed_recall_excludes_deprecated_and_empty_embedding(session):
    _seed_catalog(session)
    # tag_001 废弃（v2 字面量 deprecated）→ 不召回；tag_002 无向量 → 不召回
    session.query(TagDefine).filter(TagDefine.tag_id == "tag_001").one().status = "deprecated"
    session.query(TagDefine).filter(TagDefine.tag_id == "tag_002").one().embedding = None
    session.commit()
    cats = recall_by_dir(session, ["悬疑推理"], MOCK_EMBEDDER, top_k=50)
    ids = {c.tag_id for c in cats}
    assert "tag_001" not in ids and "tag_002" not in ids
    assert "tag_003" in ids  # 其余有效行正常召回


# ---- _render_candidates_text：4 字段格式 + 第三字段父目录名 ----


def test_render_candidates_text_third_field_is_dir_name(session):
    """候选行 4 字段格式逐字保留（MockLLM 契约依赖），第三字段 =
    tag_id → TagDefine.dir_id → TagCatalog.dir_name。"""
    _seed_catalog(session)
    defines = session.query(TagDefine).order_by(TagDefine.tag_id).all()
    text = _render_candidates_text(session, defines)
    assert text.startswith("## 候选标签\n")
    lines = text.split("\n")[1:]
    assert lines[0] == "- tag_001 | 悬疑 | 类型目录 | 推理"
    assert lines[3] == "- tag_004 | 紧张 | 情绪目录 | 情绪紧绷"
    assert all(len(line.split(" | ")) == 4 for line in lines)


# ---- ClosedTagProducer：produce 流程 ----


def _build_spec(session: Session, content_fixture, run_id: str):
    """按执行器的职责模拟组装：召回候选 → 渲染 closed prompt。"""
    cats = recall_by_dir(
        session, embed_content_texts(content_fixture), MOCK_EMBEDDER, 50)
    dir_names = {d.dir_id: d.dir_name
                 for d in session.query(TagCatalog).all()}
    # 候选清单文本：closed 标志节 + 候选行（契约见 llm/mock.py，第三段=目录名）
    candidates_text = "## 候选标签\n" + "\n".join(
        f"- {c.tag_id} | {c.tag_name} | {dir_names[c.dir_id]} | {c.description}"
        for c in cats
    )
    prompt_text = render_prompt(
        get_active_prompt(session, "closed"),
        content=content_fixture.model_dump_json(),
        candidates=candidates_text,
    )
    return GenerationSpec(
        task_id="t1", asset_id=content_fixture.asset.asset_id,
        content=content_fixture, policy=GenerationPolicy(),
        prompt_text=prompt_text, run_id=run_id,
    )


def test_closed_produce_end_to_end(session, content_fixture):
    _seed_catalog(session)
    spec = _build_spec(session, content_fixture, run_id="r2")
    out = ClosedTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)
    assert out and all(t.origin == "closed" and t.tag_id for t in out)
    assert all(t.confidence >= 0.7 for t in out) and len(out) <= 30
    assert any(t.tag_id == "tag_001" for t in out)  # 必选中 run_id（r2）
    # closed 来源：candidate_* 恒为 None；evidence 列表（本测试 prompt 以
    # JSON 文本作 content——无场景行 → Mock 场景证据兜底空列表；
    # runner 真实渲染文本路径的场景证据由 runner/API 层测试覆盖）
    assert all(t.candidate_type is None and t.candidate_description is None
               for t in out)
    assert all(t.evidence == [] for t in out)
    # 不编造目录外标签
    assert {t.tag_id for t in out} <= {tid for tid, *_ in _TAGS}


def test_closed_no_candidates_raises(session, content_fixture):
    # 空库：召回零候选 → 显式 ValueError（先导库再生成）；不会走到 LLM 调用
    spec = _build_spec(session, content_fixture, run_id="r1")
    with pytest.raises(ValueError, match="候选标签为空"):
        ClosedTagProducer().produce(spec, session, MOCK_LLM, MOCK_EMBEDDER)


# ---- parse_closed_output：closed 契约校验 ----


def test_parse_closed_bad_structure():
    with pytest.raises(LLMParseError):
        parse_closed_output('{"not": "a list"}')
    with pytest.raises(LLMParseError):
        parse_closed_output('[{"tag_id": "x"}]')  # 缺键
    with pytest.raises(LLMParseError):
        parse_closed_output('[{"tag_id":"x","tag_name":"n","relevance":2.0,'
                            '"confidence":0.5,"evidence":[]}]')  # relevance 越界
    with pytest.raises(LLMParseError):
        parse_closed_output("not json at all")  # 非 JSON 文本


def test_parse_closed_happy_path():
    items = parse_closed_output(json.dumps([
        {"tag_id": "tag_001", "tag_name": "悬疑", "relevance": 0.9,
         "confidence": 0.8,
         "evidence": [{"scene_id": "s1", "start_ms": 0, "end_ms": 100,
                       "description": "开场悬念"}]},
        # 边界值 [0,1] 双侧含端点
        {"tag_id": "tag_002", "tag_name": "剧情", "relevance": 1.0,
         "confidence": 0, "evidence": []},
    ], ensure_ascii=False))
    assert items[0]["tag_id"] == "tag_001"
    assert items[0]["evidence"][0]["scene_id"] == "s1"
    assert items[1]["relevance"] == 1.0 and items[1]["confidence"] == 0


def test_parse_closed_error_message_has_position():
    bad = json.dumps([
        {"tag_id": "t", "tag_name": "n", "relevance": 0.5, "confidence": 0.5,
         "evidence": []},
        {"tag_id": "x"},  # 第 1 个（0 起）元素缺键
    ], ensure_ascii=False)
    with pytest.raises(LLMParseError) as exc_info:
        parse_closed_output(bad)
    assert "第 1 个" in str(exc_info.value)
