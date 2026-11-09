"""嵌入 role 的调用点契约（v2：库在 JSON，向量按角色分工）。

目录标签向量 = passage（被检索语料），一切检索方（内容/候选）= query。
库索引由 `library_index` 现算（tag 名按 passage 批量嵌入），召回与新词归一
才用 query。

四调用点逐一钉死：
- recall：内容查询文本 query；库内标签 passage；
- open：候选归一 query；库索引 passage；
- runner closed：召回 query + 库索引 passage（内容向量实时计算，不落库）；
- runner open：候选 query + 库索引 passage。
"""

from conftest import MOCK_EMBEDDER, MOCK_LLM
from test_generation_open import _build_spec
from test_task_runner import _make_task, _seed_catalog
from tests.library_fixture import add_tags

from tagplatform.generation.open_gen import OpenTagProducer
from tagplatform.generation.recall import recall_candidates
from tagplatform.library import try_load_library
from tagplatform.tasks import run_generation_task


class RecordingEmbedder:
    """透传 MOCK_EMBEDDER 并记录每次 embed 调用的 (role, texts)。"""

    name, dim = MOCK_EMBEDDER.name, MOCK_EMBEDDER.dim
    # 让索引缓存键与 MOCK_EMBEDDER 一致（向量相同，避免重复嵌入）
    model_name = getattr(MOCK_EMBEDDER, "model_name", None)

    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []

    def embed(self, texts, *, role):
        self.calls.append((role, list(texts)))
        return MOCK_EMBEDDER.embed(texts, role=role)

    def roles(self) -> list[str]:
        return [role for role, _texts in self.calls]

    def texts_by_role(self, role: str) -> list[str]:
        return [text for r, texts in self.calls if r == role for text in texts]


def test_recall_embeds_content_as_query():
    """内容查询 = query；库内标签名 = passage。"""
    add_tags([{"tag_id": "tag_role_1", "dir_id": "dir_theme",
               "tag_name": "雨夜追击", "tag_type": "val_fixed",
               "description": "雨夜中的追击", "status": "active"}])
    library = try_load_library()
    rec = RecordingEmbedder()
    recall_candidates(library, rec, ["雨夜中警方展开追击"], top_k=3)
    assert "query" in rec.roles()
    assert rec.texts_by_role("query") == ["雨夜中警方展开追击"]
    assert "雨夜追击" in rec.texts_by_role("passage")


def test_open_producer_embeds_candidates_as_query(session, content_fixture):
    spec = _build_spec(session, content_fixture, run_id="r1")
    rec = RecordingEmbedder()
    OpenTagProducer().produce(spec, session, MOCK_LLM, rec)
    # 候选走 query；库索引走 passage，两边不混
    assert set(rec.roles()) <= {"query", "passage"}
    assert {"多线叙事", "情感张力", "视听风格"} <= set(rec.texts_by_role("query"))


def test_runner_closed_task_uses_query_only(session):
    """closed：内容召回 query、库索引 passage，无其它角色的额外调用。"""
    _seed_catalog(session)
    t = _make_task(session, policy_json='{"min_confidence": 0.5}')
    rec = RecordingEmbedder()
    run_generation_task(t.task_id, session, MOCK_LLM, rec)
    assert t.status == "succeeded"
    assert set(rec.roles()) <= {"query", "passage"}
    assert "query" in rec.roles()


def test_runner_open_task_only_query(session):
    t = _make_task(session, mode="open", task_id="task_role_open")
    rec = RecordingEmbedder()
    run_generation_task(t.task_id, session, MOCK_LLM, rec)
    assert t.status == "succeeded"
    assert set(rec.roles()) <= {"query", "passage"}
    # open 的检索方只有候选归一（query）；库索引是 passage
    assert "query" in rec.roles()
