"""嵌入层：确定性 mock 与本地 sentence-transformers 两实现。

统一契约（`.name` / `.dim` / `.embed(texts, *, role) -> np.ndarray (n, d)`，
输出 L2 归一化 float32）：
- role 表达 e5 非对称检索的两侧（2026-11-02 前缀修复）：`"query"`（检索方，
  内容查询文本 / 开放候选）与 `"passage"`（被检索语料方：目录标签向量与
  内容侧文档向量）。非法取值立即 ValueError；
- MockEmbedder：sha256 伪向量（零网络、零重依赖），测试与冒烟路径。规范化
  （casefold + 仅留字母数字与空格 + 折叠空白）后同形文本同向量，不同文本
  近似正交——下游指标在 mock 路径下可区分不同标签。**对 role 不敏感**：
  前缀行为由 LocalEmbedder 承担，语义逻辑测试零波及；
- LocalEmbedder：sentence-transformers 惰性导入（mock/测试路径不付首次
  import 开销，也不触发模型下载），编码前拼 e5 官方前缀 `"{role}: {text}"`，
  encode 后 L2 归一化输出 float32。

实现源自 MovieTagGen `align/embedding.py` 的 Mock/Local 两件（role 契约为
本平台新增——MovieTagGen 同样缺失 e5 前缀）；阶段 1 不做逐文本磁盘缓存，
也无 API 通道，故去掉 embed_texts 缓存层与 ApiEmbedder。
"""
from __future__ import annotations

import hashlib
from typing import Protocol

import numpy as np

_MOCK_DIM = 64

# e5 非对称检索的两侧 role（查询方 / 语料方）
_ROLES = ("query", "passage")


def _validate_role(role: str) -> None:
    """role 只允许 query|passage：拼错立即拒绝，防调用点静默落错空间。"""
    if role not in _ROLES:
        raise ValueError(f"未知 role: {role!r}（合法取值 {'|'.join(_ROLES)}）")


class Embedder(Protocol):
    """嵌入器契约：批量文本 + role → (n, d) float32 L2 归一化矩阵。

    dim 在真实模型首次编码前未知（None），MockEmbedder 恒为 64。
    """

    name: str
    dim: int | None

    def embed(self, texts: list[str], *, role: str) -> np.ndarray: ...


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    """逐行 L2 归一化（零向量行按原样返回，不做除零）。"""
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    return mat / np.clip(norms, 1e-12, None)


# ---------- MockEmbedder ----------

def _canonical(text: str) -> str:
    """规范化：casefold → 仅保留字母数字与空格（其余丢弃）→ 折叠连续空白 → 去首尾。

    isalnum 是 Unicode 感知的（中文/重音字符保留），多语标签天然适用。
    """
    kept = "".join(ch if ch.isalnum() or ch.isspace() else "" for ch in text.casefold())
    return " ".join(kept.split())


def _mock_unit_vec(canonical: str) -> np.ndarray:
    """sha256 摘要按位展开成 64 维 ±1 符号向量，再归一化。

    无 random 模块、跨进程确定：同形必同向量；不同形相互独立，64 维符号
    向量的余弦近似 N(0, 1/8)，|cos| ≥ 0.5 概率 ~6e-5，满足近似正交。
    """
    bits = int.from_bytes(hashlib.sha256(canonical.encode("utf-8")).digest(), "big")
    vec = np.array([1.0 if (bits >> i) & 1 else -1.0 for i in range(_MOCK_DIM)],
                   dtype=np.float32)
    return vec / np.linalg.norm(vec)


class MockEmbedder:
    """确定性伪嵌入：哈希即向量，测试与 mock 配置专用。

    对 role 不敏感（测试替身契约：同文本两 role 同向量），仅做合法性校验。
    """

    name = "mock"
    dim = _MOCK_DIM

    def embed(self, texts: list[str], *, role: str) -> np.ndarray:
        _validate_role(role)
        if not texts:
            return np.zeros((0, _MOCK_DIM), dtype=np.float32)
        return np.stack([_mock_unit_vec(_canonical(t)) for t in texts])


# ---------- LocalEmbedder ----------

class LocalEmbedder:
    """sentence-transformers 本地模型；模型加载惰性到首次 embed 非空输入。"""

    def __init__(self, model_name: str, device: str = "cpu"):
        self.model_name = model_name
        self.device = device
        self.name = model_name
        self.dim = None          # 首次真实编码后回填（模型维度随模型而定）
        self._model = None

    def _load(self):
        if self._model is None:
            # 懒导入：mock/测试路径不需要安装后首次 import 的开销，更不触发下载
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name, device=self.device)
        return self._model

    def embed(self, texts: list[str], *, role: str) -> np.ndarray:
        # 校验先于模型加载：非法 role 绝不触发 sentence_transformers 导入
        _validate_role(role)
        if not texts:
            # dim 未知时返回 (0, 0)：绝不为了空输入加载/下载模型
            return np.zeros((0, self.dim or 0), dtype=np.float32)
        model = self._load()
        # e5 官方前缀格式（冒号后带空格）：query 检索方 / passage 语料方
        prefixed = [f"{role}: {t}" for t in texts]
        vecs = model.encode(prefixed, convert_to_numpy=True,
                            normalize_embeddings=True, show_progress_bar=False)
        mat = np.asarray(vecs, dtype=np.float32)
        # 双保险：normalize_embeddings 已归一化，再归一一次防后端差异
        mat = _l2_normalize(mat)
        self.dim = mat.shape[1]
        return mat


# ---------- 工厂 ----------

def make_embedder(model_name: str, mock: bool) -> Embedder:
    """mock → 确定性伪嵌入（不触碰模型）；否则本地 sentence-transformers 模型。"""
    if mock:
        return MockEmbedder()
    return LocalEmbedder(model_name)
