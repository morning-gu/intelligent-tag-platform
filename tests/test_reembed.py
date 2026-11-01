"""一次性向量迁移 reembed_all 测试：存量无前缀向量 → role 前缀空间重嵌。

- 仅重嵌 embedding 非空的行（NULL 保持 NULL：纯迁移不新增行为）；
- 向量确实被替换（翻转嵌入器保证非原值复写）；
- 目录行文本 "名称。描述" / 上下文行文本取 content_json 摘要，
  均以 passage role 嵌入（目录与内容侧都是语料方，与入库/回填同源）；
- 不 commit（调用方统一事务边界）：会话内查询即见更新。
"""

from datetime import datetime, timezone

import numpy as np
from test_task_runner import _CONTENT_DATA, _make_task

from tagplatform.db.models import MediaContext, TagCatalog
from tagplatform.reembed import reembed_all
from tagplatform.vectors import decode_vector, encode_vector


class _FlipEmbedder:
    """每次调用返回全然不同的单位向量：断言"确实被重嵌替换"而非原值复写。
    记录 (role, texts) 供文本与 role 断言。"""

    name, dim = "flip", 8

    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []
        self._n = 0

    def embed(self, texts, *, role):
        self.calls.append((role, list(texts)))
        self._n += 1
        mat = np.full((len(texts), 8), float(self._n), dtype=np.float32)
        return mat / np.clip(np.linalg.norm(mat, axis=1, keepdims=True), 1e-12, None)


def _legacy_vec() -> bytes:
    """伪造存量无前缀向量（非单位、值恒定）：与重嵌结果必然不同。"""
    return encode_vector(np.full(8, 0.1, dtype=np.float32))


def test_reembed_catalog_replaces_vectors(session):
    session.add(TagCatalog(tag_id="t1", tag_name="雨夜", tag_type="genre",
                           description="雨夜描述", status="active",
                           embedding=_legacy_vec()))
    session.add(TagCatalog(tag_id="t2", tag_name="黎明", tag_type="genre",
                           description="黎明描述", status="active",
                           embedding=None))
    session.commit()

    flip = _FlipEmbedder()
    summary = reembed_all(session, flip)

    assert (summary.catalog_rows, summary.context_rows) == (1, 0)
    # 一次批量，文本与治理入库同源，role=passage
    assert flip.calls == [("passage", ["雨夜。雨夜描述"])]
    t1 = session.get(TagCatalog, "t1")
    assert not np.allclose(decode_vector(t1.embedding),
                           decode_vector(_legacy_vec()))
    # NULL 行保持 NULL
    assert session.get(TagCatalog, "t2").embedding is None


def test_reembed_context_replaces_summary_vector(session):
    t = _make_task(session)  # 播种 asset + media_context（content_json 为夹具）
    ctx = session.get(MediaContext, t.context_id)
    ctx.embedding = _legacy_vec()
    session.commit()

    flip = _FlipEmbedder()
    summary = reembed_all(session, flip)

    assert summary.catalog_rows == 0 and summary.context_rows == 1
    # 文本与 runner 回填同源：content_json 的剧情摘要，role=passage
    assert ("passage", [_CONTENT_DATA["content"]["summary"]]) in flip.calls
    refreshed = session.get(MediaContext, t.context_id)
    assert refreshed.embedding is not None
    assert not np.allclose(decode_vector(refreshed.embedding),
                           decode_vector(_legacy_vec()))
    # catalog 无行可嵌：不产生额外调用
    assert len(flip.calls) == 1


def test_reembed_empty_db_no_calls(session):
    flip = _FlipEmbedder()
    summary = reembed_all(session, flip)
    assert (summary.catalog_rows, summary.context_rows) == (0, 0)
    assert flip.calls == []


def test_reembed_refreshes_updated_at(session):
    stale = datetime(2020, 1, 1, tzinfo=timezone.utc)
    session.add(TagCatalog(tag_id="t1", tag_name="雨夜", tag_type="genre",
                           description="雨夜描述", status="active",
                           embedding=_legacy_vec(), created_at=stale,
                           updated_at=stale))
    session.commit()

    reembed_all(session, _FlipEmbedder())

    assert session.get(TagCatalog, "t1").updated_at > stale
