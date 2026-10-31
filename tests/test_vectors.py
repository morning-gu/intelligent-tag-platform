"""向量编解码与余弦检索测试：float32 little-endian 往返、点积降序 top_k。"""

import numpy as np
import pytest

from tagplatform.vectors import decode_vector, encode_vector, search_similar


def test_vector_roundtrip_and_search():
    v = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    assert np.allclose(decode_vector(encode_vector(v)), v)
    hits = search_similar(np.array([1.0, 0.0, 0.0], dtype=np.float32),
                          [("a", np.array([1, 0, 0], np.float32)),
                           ("b", np.array([0, 1, 0], np.float32))], top_k=2)
    assert hits[0][0] == "a" and hits[0][1] == pytest.approx(1.0)
