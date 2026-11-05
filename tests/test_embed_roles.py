"""嵌入 role 的调用点契约（e5 前缀契约，v2 Task 6）：
目录标签向量=passage（被检索语料，入库/更新时算好持久化），一切检索方=query。

调用点逐一钉死（v2）：
- governance 入库向量 passage（存进 tag_define 成为语料）；
- recall 内容查询文本 query（库内标签向量是 passage 持久化 BLOB，doc §5）；
- open 候选归一 query（Task 7 域，用例保持红直到其流水线 v2 化）；
- v2 删除 runner 上下文摘要向量回填（内容向量生成任务启动时实时计算、
  不落库，doc §3.1）——closed 任务仅 query 一种 role。
"""

from conftest import MOCK_EMBEDDER, MOCK_LLM

from tagplatform.db.models import TagCatalog, TagDefine
from tagplatform.generation.open_gen import OpenTagProducer
from tagplatform.generation.recall import recall_by_dir
from tagplatform.governance import TagRow, run_governance
from tagplatform.tasks import run_generation_task
from tagplatform.vectors import encode_vector

# 跨模块播种助手延迟导入：test_task_runner / test_generation_open 属
# Task 6/7 域，生成侧用例在对应域任务落地时恢复（open_gen 相关用例
# 保持红属 Task 7 范围）。


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
    session.add(TagCatalog(dir_id="dir_role", dir_name="叙事目录",
                           dir_type="dir_static", description="叙事类目"))
    session.flush()
    session.add(TagDefine(tag_id="tag_role_1", tag_name="雨夜追击",
                          tag_type="val_fixed", description="雨夜中的追击",
                          dir_id="dir_role", status="active",
                          embedding=encode_vector(vec)))
    session.commit()
    rec = RecordingEmbedder()
    recall_by_dir(session, ["雨夜中警方展开追击"], rec, top_k=3)
    assert [role for role, _texts in rec.calls] == ["query"]


def test_open_producer_embeds_candidates_as_query(session, content_fixture):
    from test_generation_open import _build_spec

    spec = _build_spec(session, content_fixture, run_id="r1")
    rec = RecordingEmbedder()
    OpenTagProducer().produce(spec, session, MOCK_LLM, rec)
    # 候选一次批量嵌入，role 全 query
    assert [role for role, _texts in rec.calls] == ["query"]


def test_runner_closed_task_embeds_content_as_query(session):
    """closed 任务嵌入调用：仅召回查询 query——v2 删除上下文摘要向量回填
    （内容向量实时计算不落库），passage 角色只出现在入库/重嵌路径。"""
    from test_task_runner import _make_task, _seed_catalog

    _seed_catalog(session)
    t = _make_task(session, policy_json='{"min_confidence": 0.5}')
    rec = RecordingEmbedder()
    run_generation_task(t.task_id, session, MOCK_LLM, rec)
    assert t.status == "succeeded"
    assert {role for role, _texts in rec.calls} == {"query"}


def test_runner_open_task_only_query(session):
    from test_task_runner import _make_task

    t = _make_task(session, mode="open", task_id="task_role_open")
    rec = RecordingEmbedder()
    run_generation_task(t.task_id, session, MOCK_LLM, rec)
    assert t.status == "succeeded"
    # open 契约：候选归一 query，且不回填上下文向量（无 passage 调用）
    assert {role for role, _texts in rec.calls} == {"query"}
