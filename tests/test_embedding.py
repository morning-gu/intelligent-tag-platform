"""MockEmbedder 契约测试：确定性、L2 归一化、空输入不加载。

红线：mock 路径绝不允许 `sentence_transformers` 进入 sys.modules——
Lazy import 只能发生在 LocalEmbedder._load 内部。
"""

import sys

import numpy as np

from tagplatform.embed import make_embedder


def test_mock_deterministic_and_no_download():
    e = make_embedder("intfloat/multilingual-e5-base", mock=True)
    a, b = e.embed(["悬疑 推理"]), e.embed(["悬疑  推理 "])
    assert a.shape == (1, 64) and np.allclose(a, b)
    assert np.isclose(np.linalg.norm(a[0]), 1.0)
    assert "sentence_transformers" not in sys.modules


def test_mock_empty_never_loads():
    e = make_embedder("x", mock=True)
    assert e.embed([]).shape == (0, 64)
