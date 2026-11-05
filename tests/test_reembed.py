"""v2 全量向量重建 reembed_all 测试：目录/标签双通道 passage 重嵌。

- 双通道全表重建：行数计入 ReembedSummary(dir_rows, tag_rows)，embedding
  全部落库（存量行被替换、NULL 行被回填——v2 语义为全表重建）；
- 落库值与嵌入器对同文本的 passage 输出逐字一致（MockEmbedder 跨实例
  确定：sha256 即向量）；
- 目录文本 "目录名。描述" / 标签文本 "标签名。描述"，均 passage 角色
  （与治理入库写入路径同源同形）；
- 无 context 通道：v2 media_asset 无 embedding 列，媒体行不产生任何
  嵌入调用；
- 不 commit（调用方统一事务边界）：会话内查询即见更新。
"""

from datetime import datetime, timezone

import numpy as np

from tagplatform.db.models import MediaAsset, TagCatalog, TagDefine
from tagplatform.embed import MockEmbedder
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
    """伪造存量向量（非单位、值恒定）：与重嵌结果必然不同。"""
    return encode_vector(np.full(8, 0.1, dtype=np.float32))


def _seed_both_channels(session):
    """目录/标签双通道各播一行存量向量 + 一行 NULL（全表语义都应重嵌）。"""
    session.add(TagCatalog(dir_id="d1", dir_name="场景", dir_type="dir_static",
                           description="场景目录", embedding=_legacy_vec()))
    session.add(TagCatalog(dir_id="d2", dir_name="风格", dir_type="dir_dynamic",
                           description="风格目录", embedding=None))
    session.add(TagDefine(tag_id="t1", tag_name="雨夜", tag_type="val_fixed",
                          description="雨夜描述", dir_id="d1",
                          embedding=_legacy_vec()))
    session.add(TagDefine(tag_id="t2", tag_name="黎明", tag_type="val_fixed",
                          description="黎明描述", dir_id="d1", embedding=None))
    session.commit()


def test_reembed_both_channels_rows_texts_and_role(session):
    _seed_both_channels(session)
    flip = _FlipEmbedder()

    summary = reembed_all(session, flip)

    assert (summary.dir_rows, summary.tag_rows) == (2, 2)
    # 目录通道在前、标签通道在后；文本与治理入库同源（"名。描述"），均 passage
    assert flip.calls == [
        ("passage", ["场景。场景目录", "风格。风格目录"]),
        ("passage", ["雨夜。雨夜描述", "黎明。黎明描述"]),
    ]
    # 全表重建：存量行被替换、NULL 行被回填
    d1 = session.get(TagCatalog, "d1")
    assert d1.embedding is not None
    assert not np.allclose(decode_vector(d1.embedding),
                           decode_vector(_legacy_vec()))
    assert session.get(TagCatalog, "d2").embedding is not None
    t1 = session.get(TagDefine, "t1")
    assert t1.embedding is not None
    assert not np.allclose(decode_vector(t1.embedding),
                           decode_vector(_legacy_vec()))
    assert session.get(TagDefine, "t2").embedding is not None


def test_reembed_persists_mockembedder_passage_vectors(session):
    """落库字节 = 嵌入器对同文本 passage 输出（MockEmbedder 跨实例确定）。"""
    _seed_both_channels(session)

    summary = reembed_all(session, MockEmbedder())

    assert (summary.dir_rows, summary.tag_rows) == (2, 2)
    mock = MockEmbedder()
    d1 = session.get(TagCatalog, "d1")
    assert np.allclose(decode_vector(d1.embedding),
                       mock.embed(["场景。场景目录"], role="passage")[0])
    t2 = session.get(TagDefine, "t2")
    assert np.allclose(decode_vector(t2.embedding),
                       mock.embed(["黎明。黎明描述"], role="passage")[0])


def test_reembed_media_rows_produce_no_channel(session):
    """无 context 通道：v2 媒体行不参与重嵌（media_asset 无 embedding 列）。"""
    _seed_both_channels(session)
    session.add(MediaAsset(asset_id="a1", asset_type="movie", title="片名"))
    session.commit()

    flip = _FlipEmbedder()
    summary = reembed_all(session, flip)

    # 仅目录/标签两个通道各一次批量调用，媒体行零涉及
    assert len(flip.calls) == 2
    assert (summary.dir_rows, summary.tag_rows) == (2, 2)


def test_reembed_empty_db_no_calls(session):
    flip = _FlipEmbedder()
    summary = reembed_all(session, flip)
    assert (summary.dir_rows, summary.tag_rows) == (0, 0)
    assert flip.calls == []


def test_reembed_refreshes_updated_at(session):
    stale = datetime(2020, 1, 1, tzinfo=timezone.utc)
    session.add(TagCatalog(dir_id="d1", dir_name="场景", dir_type="dir_static",
                           description="场景目录", embedding=_legacy_vec(),
                           created_at=stale, updated_at=stale))
    session.add(TagDefine(tag_id="t1", tag_name="雨夜", tag_type="val_fixed",
                          description="雨夜描述", dir_id="d1",
                          embedding=_legacy_vec(), created_at=stale,
                          updated_at=stale))
    session.commit()

    reembed_all(session, _FlipEmbedder())

    assert session.get(TagCatalog, "d1").updated_at > stale
    assert session.get(TagDefine, "t1").updated_at > stale
