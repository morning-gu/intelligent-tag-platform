"""嵌入 role 的调用点契约（e5 前缀修复，2026-11-02）：
目录标签向量=passage（被检索语料），一切检索方=query。

四调用点逐一钉死：
- governance 入库向量 passage（存进 tag_catalog 成为语料）；
- recall 内容查询文本 query；
- open 候选归一 query（query↔passage 是 e5 训练方向）；
- runner 上下文摘要回填 passage（内容侧文档数据）；closed 任务
  query+passage 两者都出现（召回+回填），open 任务仅 query
  （契约：open 不回填上下文向量）。
"""

from conftest import MOCK_EMBEDDER, MOCK_LLM
from test_generation_open import _build_spec
from test_task_runner import _make_task, _seed_catalog

from tagplatform.db.models import TagCatalog
from tagplatform.generation.open_gen import OpenTagProducer
from tagplatform.generation.recall import recall_by_type
from tagplatform.governance import TagRow, run_governance
from tagplatform.tasks import run_generation_task
from tagplatform.vectors import encode_vector


class RecordingEmbedder:
    """透传 MOCK_EMBEDDER 并记录每次 embed 调用的 (role, texts)。"""

    name, dim = MOCK_EMBEDDER.name, MOCK_EMBEDDER.dim

    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []

    def embed(self, texts, *, role):
        self.calls.append((role, list(texts)))
        return MOCK_EMBEDDER.embed(texts, role=role)


def test_governance_embeds_catalog_as_passage(session):
    rec = RecordingEmbedder()
    run_governance(
        [TagRow(None, "雨夜追击", "genre", "雨夜中的追击", None, [], None, None)],
        session, rec)
    assert rec.calls != []
    assert {role for role, _texts in rec.calls} == {"passage"}


def test_recall_embeds_content_as_query(session):
    vec = MOCK_EMBEDDER.embed(["雨夜追击。雨夜中的追击"], role="passage")[0]
    session.add(TagCatalog(tag_id="tag_role_1", tag_name="雨夜追击",
                           tag_type="genre", description="雨夜中的追击",
                           status="active", embedding=encode_vector(vec)))
    session.commit()
    rec = RecordingEmbedder()
    recall_by_type(session, ["雨夜中警方展开追击"], rec, top_k=3)
    assert [role for role, _texts in rec.calls] == ["query"]


def test_open_producer_embeds_candidates_as_query(session, content_fixture):
    spec = _build_spec(session, content_fixture, run_id="r1")
    rec = RecordingEmbedder()
    OpenTagProducer().produce(spec, session, MOCK_LLM, rec)
    # 候选一次批量嵌入，role 全 query
    assert [role for role, _texts in rec.calls] == ["query"]


def test_runner_closed_task_uses_query_only(session):
    """closed 只做召回 query：v2 不再回填 media_context 的摘要向量。"""
    _seed_catalog(session)
    t = _make_task(session, policy_json='{"min_confidence": 0.5}')
    rec = RecordingEmbedder()
    run_generation_task(t.task_id, session, MOCK_LLM, rec)
    assert t.status == "succeeded"
    assert {role for role, _texts in rec.calls} == {"query"}


def test_runner_open_task_only_query(session):
    t = _make_task(session, mode="open", task_id="task_role_open")
    rec = RecordingEmbedder()
    run_generation_task(t.task_id, session, MOCK_LLM, rec)
    assert t.status == "succeeded"
    # open 契约：候选归一 query，且不回填上下文向量（无 passage 调用）
    assert {role for role, _texts in rec.calls} == {"query"}
