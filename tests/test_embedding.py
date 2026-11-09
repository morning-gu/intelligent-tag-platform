"""嵌入器契约测试：Mock 确定性 / role 合法性 / Local 前缀拼装。

红线：mock 路径绝不允许 `sentence_transformers` 进入 sys.modules——
Lazy import 只能发生在 LocalEmbedder._load 内部（fake 模块注入测试经
monkeypatch 还原，不破坏该红线）。
"""

import sys
import types

import numpy as np
import pytest

from tagplatform.embed import LocalEmbedder, make_embedder


def test_mock_deterministic_and_no_download():
    e = make_embedder("intfloat/multilingual-e5-base", mock=True)
    a, b = e.embed(["悬疑 推理"], role="query"), e.embed(["悬疑  推理 "], role="query")
    assert a.shape == (1, 64) and np.allclose(a, b)
    assert np.isclose(np.linalg.norm(a[0]), 1.0)
    assert "sentence_transformers" not in sys.modules


def test_mock_empty_never_loads():
    e = make_embedder("x", mock=True)
    assert e.embed([], role="passage").shape == (0, 64)


# ---------- role 前缀契约（e5 非对称检索：query 检索方 / passage 语料方） ----------


def test_mock_role_insensitive():
    """MockEmbedder 对 role 不敏感（测试替身契约）：同文本两 role 同向量——
    语义逻辑测试零波及；前缀拼装行为由 LocalEmbedder 专属测试钉住。"""
    e = make_embedder("x", mock=True)
    q = e.embed(["雨夜追凶"], role="query")
    p = e.embed(["雨夜追凶"], role="passage")
    assert np.allclose(q, p)


def test_embed_invalid_role_rejected():
    """未知 role 立即 ValueError（防调用点拼错 role 静默落错空间）。"""
    e = make_embedder("x", mock=True)
    with pytest.raises(ValueError, match="role"):
        e.embed(["文本"], role="doc")


def test_local_invalid_role_never_loads():
    """非法 role 先于模型加载拒绝：不触发 sentence_transformers 导入。"""
    e = LocalEmbedder("m")
    with pytest.raises(ValueError, match="role"):
        e.embed(["文本"], role="doc")
    assert "sentence_transformers" not in sys.modules


def test_local_prefixes_texts_per_role(monkeypatch):
    """LocalEmbedder 按 role 拼 e5 前缀（"query: 文本"/"passage: 文本"），
    输出 L2 归一化并回填 dim。"""
    encoded: list[list[str]] = []

    class _FakeModel:
        def __init__(self, name, device="cpu"):
            self.loaded = (name, device)

        def encode(self, texts, convert_to_numpy=True,
                   normalize_embeddings=True, show_progress_bar=False):
            encoded.append(list(texts))
            mat = np.array([[float(len(t))] for t in texts], dtype=np.float32)
            return mat / np.clip(
                np.linalg.norm(mat, axis=1, keepdims=True), 1e-12, None)

    fake_st = types.ModuleType("sentence_transformers")
    fake_st.SentenceTransformer = _FakeModel
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)

    e = LocalEmbedder("intfloat/multilingual-e5-base", device="cpu")
    v = e.embed(["雨夜", "追凶"], role="query")
    assert encoded == [["query: 雨夜", "query: 追凶"]]
    assert v.shape == (2, 1)
    assert np.isclose(np.linalg.norm(v[0]), 1.0)
    assert e.dim == 1

    e2 = LocalEmbedder("intfloat/multilingual-e5-base")
    e2.embed(["黎明"], role="passage")
    assert encoded[-1] == ["passage: 黎明"]


def test_local_plain_models_get_no_prefix(monkeypatch):
    """非 e5 模型（BGE-M3）吃纯文本：前缀会被当正文，实测拉低分辨力。"""
    import sys
    import types

    import numpy as np

    from tagplatform.embed.embedding import LocalEmbedder

    encoded: list[list[str]] = []

    class _FakeModel:
        def __init__(self, name, device="cpu"):
            self.name = name

        def encode(self, texts, convert_to_numpy=True,
                   normalize_embeddings=True, show_progress_bar=False):
            encoded.append(list(texts))
            mat = np.array([[float(len(t))] for t in texts], dtype=np.float32)
            return mat / np.clip(
                np.linalg.norm(mat, axis=1, keepdims=True), 1e-12, None)

    fake_st = types.ModuleType("sentence_transformers")
    fake_st.SentenceTransformer = _FakeModel
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)

    e = LocalEmbedder("BAAI/bge-m3")
    assert e.prefix_style == "plain"
    e.embed(["雨夜"], role="query")
    assert encoded == [["雨夜"]]
