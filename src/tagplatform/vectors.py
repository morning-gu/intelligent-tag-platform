"""向量编解码与余弦检索：float32 little-endian 字节往返 + 点积 top_k。

向量一律 L2 归一化（Embedder 契约保证）：归一化向量的点积即余弦相似度，
检索无需再除模长。
"""

import numpy as np

# 统一 little-endian float32：BLOB 字节格式不随平台字节序漂移
_VECTOR_DTYPE = "<f4"


def encode_vector(vec: np.ndarray) -> bytes:
    """float32 little-endian 序列化为 bytes（DB BLOB 存储格式）。"""
    return np.asarray(vec, dtype=_VECTOR_DTYPE).tobytes()


def decode_vector(blob: bytes) -> np.ndarray:
    """bytes 反序列化为一维 float32 向量。

    frombuffer 结果只读且与入参共享内存，拷贝一份交出可写独立数组。
    """
    return np.frombuffer(blob, dtype=_VECTOR_DTYPE).copy()


def search_similar(query: np.ndarray,
                   candidates: list[tuple[str, np.ndarray]],
                   top_k: int) -> list[tuple[str, float]]:
    """余弦相似度降序取前 top_k，返回 (候选 id, 相似度) 列表。

    query 与候选须同维且均已归一化（点积即余弦）；top_k 超过候选数时
    返回全部，top_k <= 0 或候选为空返回空列表。
    """
    if top_k <= 0 or not candidates:
        return []
    ids = [cid for cid, _ in candidates]
    mat = np.stack([np.asarray(vec, dtype=np.float32) for _, vec in candidates])
    q = np.asarray(query, dtype=np.float32)
    if q.ndim != 1 or q.shape[0] != mat.shape[1]:
        raise ValueError(
            f"query 维度 {q.shape} 与候选维度 {mat.shape[1]} 不一致")
    sims = mat @ q
    # stable 降序：同分保持候选原顺序，结果跨运行确定
    order = np.argsort(-sims, kind="stable")[:top_k]
    return [(ids[i], float(sims[i])) for i in order]
