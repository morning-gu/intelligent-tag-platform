"""嵌入器契约测试：Mock 确定性 / role 合法性 / Local 前缀拼装 / Remote 远程通道。

红线：mock 路径绝不允许 `sentence_transformers` 进入 sys.modules——
Lazy import 只能发生在 LocalEmbedder._load 内部（fake 模块注入测试经
monkeypatch 还原，不破坏该红线）。
"""

import sys
import threading
import types

import numpy as np
import pytest

from tagplatform.capabilities.embed import (
    LocalEmbedder,
    MockEmbedder,
    RemoteEmbedder,
    build_embedder,
    make_embedder,
)
from tagplatform.settings import Settings


# ---------- role 前缀契约（e5 非对称检索：query 检索方 / passage 语料方） ----------


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
        def __init__(self, name, device="cpu", *, local_files_only: bool):
            assert local_files_only is True
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
    encoded: list[list[str]] = []

    class _FakeModel:
        def __init__(self, name, device="cpu", *, local_files_only: bool):
            assert local_files_only is True
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


def test_local_lazy_load_is_double_checked_locked(monkeypatch):
    """并发首调 embed 只允许一次模型构造（双检锁）。

    无锁懒加载在评测线程池下多线程同时构造 SentenceTransformer，原生层
    （tokenizers/safetensors Rust 分配器）直接崩溃且 Python 层拦不住
    （实测 0xC0000409 进程即亡）。锁只护加载：构造完后并发 encode 不再
    串行（已验证线程安全）。
    """
    loads = []
    load_started = threading.Event()
    release = threading.Event()

    class _BlockingModel:
        def __init__(self, name, device="cpu", *, local_files_only: bool):
            loads.append(threading.get_ident())
            load_started.set()
            release.wait(timeout=5)  # 模拟真模型数十秒加载窗口
            self.dummy = True

        def encode(self, texts, convert_to_numpy=True,
                   normalize_embeddings=True, show_progress_bar=False):
            mat = np.array([[float(len(t))] for t in texts], dtype=np.float32)
            return mat / np.clip(
                np.linalg.norm(mat, axis=1, keepdims=True), 1e-12, None)

    fake_st = types.ModuleType("sentence_transformers")
    fake_st.SentenceTransformer = _BlockingModel
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)

    e = LocalEmbedder("BAAI/bge-m3")
    shapes = {}

    def worker(i: int) -> None:
        v = e.embed([f"文本{i}"], role="query")
        shapes[i] = v.shape

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    # 第一个线程进入构造后，其余三个都在等锁：此时仍只有一次加载记录
    assert load_started.wait(timeout=5)
    assert len(loads) == 1
    release.set()
    for t in threads:
        t.join(timeout=5)
    assert len(loads) == 1          # 全程仅构造一次模型
    assert sorted(shapes) == [0, 1, 2, 3]
    assert all(s == (1, 1) for s in shapes.values())


# ---------- RemoteEmbedder（OpenAI 兼容 /v1/embeddings，transport 可注入） ----------


def _fake_transport(log: list, dim: int = 4, scale: float = 1.0):
    """默认假 transport：记录 (url, model, texts)；data 乱序返回考验 index 排序。"""

    def transport(url, model, texts):
        log.append((url, model, list(texts)))
        items = [
            {"index": i, "embedding": [scale * float(i + j + 1) for j in range(dim)]}
            for i in range(len(texts))
        ]
        items.reverse()  # 乱序：解析层必须按 index 还原行序
        return {"model": model, "data": items}

    return transport


def test_remote_basic_shape_norm_and_dim():
    log: list = []
    e = RemoteEmbedder("http://9.15.87.193:8193/v1", model="bge-m3",
                       batch_size=8, transport=_fake_transport(log))
    v = e.embed(["雨夜", "追凶"], role="query")
    assert v.shape == (2, 4)
    assert v.dtype == np.float32
    assert e.dim == 4
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0)
    assert log == [("http://9.15.87.193:8193/v1/embeddings", "bge-m3",
                    ["雨夜", "追凶"])]


def test_remote_row_order_follows_text_order():
    """data 乱序返回时，行序必须与输入 texts 一致（index 排序后拼接）。"""
    log: list = []
    e = RemoteEmbedder("http://x/v1", transport=_fake_transport(log))
    v = e.embed(["a", "b", "c"], role="passage")
    # 假向量第 i 行元素是 (i+j+1)，逐行 L2 归一化不改变行内比值——
    # 行序正确时 v[0] ∝ [1,2,3,4]（行内比 2/1）、v[2] ∝ [3,4,5,6]（行内比 4/3）；
    # 乱序（反序拼接）时 v[0] 变 [3,4,5,6]（比 4/3），两断言立刻交换暴露
    assert np.isclose(v[0, 1] / v[0, 0], 2.0)
    assert np.isclose(v[2, 1] / v[2, 0], 4.0 / 3.0)


def test_remote_role_validated_before_transport():
    log: list = []
    e = RemoteEmbedder("http://x/v1", transport=_fake_transport(log))
    with pytest.raises(ValueError, match="role"):
        e.embed(["文本"], role="doc")
    assert log == []  # 非法 role 先于任何网络调用拒绝


def test_remote_empty_never_calls():
    log: list = []
    e = RemoteEmbedder("http://x/v1", transport=_fake_transport(log))
    assert e.embed([], role="passage").shape == (0, 0)
    assert log == []


def test_remote_batches_chunks():
    log: list = []
    e = RemoteEmbedder("http://x/v1", batch_size=3, transport=_fake_transport(log))
    v = e.embed([f"t{i}" for i in range(7)], role="passage")
    assert v.shape == (7, 4)
    assert [len(call[2]) for call in log] == [3, 3, 1]


def test_remote_dim_drift_rejected():
    state = {"dim": 4}

    def transport(url, model, texts):
        return {"data": [{"index": i, "embedding": [1.0] * state["dim"]}
                         for i in range(len(texts))]}

    e = RemoteEmbedder("http://x/v1", transport=transport)
    e.embed(["a"], role="query")
    state["dim"] = 8
    with pytest.raises(ValueError, match="维度"):
        e.embed(["b"], role="query")


def test_remote_count_mismatch_rejected():
    def transport(url, model, texts):
        return {"data": [{"index": 0, "embedding": [1.0, 2.0]}]}  # 缺一条

    e = RemoteEmbedder("http://x/v1", transport=transport)
    with pytest.raises(ValueError, match="数量"):
        e.embed(["a", "b"], role="query")


def test_remote_missing_data_rejected():
    e = RemoteEmbedder("http://x/v1", transport=lambda u, m, t: {"detail": "Not Found"})
    with pytest.raises(ValueError, match="data"):
        e.embed(["a"], role="query")


# ---------- 工厂：backend 开关（默认 local，行为与历史版本一致） ----------


def test_make_embedder_default_local_unchanged():
    e = make_embedder("BAAI/bge-m3", mock=False)
    assert isinstance(e, LocalEmbedder) and e.model_name == "BAAI/bge-m3"


def test_make_embedder_remote():
    e = make_embedder("BAAI/bge-m3", mock=False, backend="remote",
                      api_base="http://h:8193/v1", api_model="bge-m3", api_batch=16)
    assert isinstance(e, RemoteEmbedder)
    assert e.base_url == "http://h:8193/v1"
    assert e.model == "bge-m3" and e.batch_size == 16


def test_make_embedder_remote_requires_api_base():
    with pytest.raises(ValueError, match="api_base"):
        make_embedder("m", mock=False, backend="remote", api_base="")


def test_make_embedder_unknown_backend_rejected():
    with pytest.raises(ValueError, match="backend"):
        make_embedder("m", mock=False, backend="cuda")


def test_make_embedder_mock_wins_over_backend():
    e = make_embedder("m", mock=True, backend="remote", api_base="http://x/v1")
    assert isinstance(e, MockEmbedder)


def test_build_embedder_remote_from_settings():
    s = Settings(embedding_backend="remote",
                 embedding_api_base="http://9.15.87.193:8193/v1",
                 embedding_api_model="bge-m3", embedding_api_batch=32)
    e = build_embedder(s)
    assert isinstance(e, RemoteEmbedder)
    assert e.base_url == "http://9.15.87.193:8193/v1" and e.model == "bge-m3"
