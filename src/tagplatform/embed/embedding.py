"""嵌入层：确定性 mock、本地 sentence-transformers、OpenAI 兼容远程三实现。

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
  import 开销，也不触发模型下载）。**前缀按模型决定**（2026-11-05 换模型）：
  e5 系需要 e5 官方前缀 `"{role}: {text}"`，BGE-M3 及其它模型吃纯文本，
  加前缀反而伤效果；encode 后 L2 归一化输出 float32；
- RemoteEmbedder（2026-11-18 新增）：OpenAI 兼容 /v1/embeddings 远程通道
  （如 bge-m3 @ http://host:8193/v1）。role 只校验不拼前缀（bge-m3 无指令
  口径，与 Local 的 plain 风格同一语义空间，切换后端无需重校准阈值）；
  分批请求、按 index 还原行序、客户端侧统一再归一化。transport 可注入，
  默认 stdlib urllib 懒构建——零新增依赖。

实现源自 MovieTagGen `align/embedding.py` 的 Mock/Local 两件（role 契约为
本平台新增——MovieTagGen 同样缺失 e5 前缀）；阶段 1 无逐文本磁盘缓存，
embed_texts 缓存层与 ApiEmbedder 不随迁——远程通道按 OpenAI 兼容口径重写。
"""
from __future__ import annotations

import hashlib
from typing import Callable, Protocol

import numpy as np

from tagplatform.settings import Settings

_MOCK_DIM = 64

# e5 非对称检索的两侧 role（查询方 / 语料方）
_ROLES = ("query", "passage")


def _prefix_style(model_name: str) -> str:
    """模型 → 前缀风格：e5 加 "role: " 前缀，其它模型用纯文本。

    BGE-M3 官方口径是无指令、直接编码；给它拼 "query: " 会把指令当正文，
    实测会拉低分辨力。未知模型一律按纯文本——多前缀无害但少前缀更安全。
    """
    return "e5" if "e5" in (model_name or "").casefold() else "plain"


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
        self.prefix_style = _prefix_style(model_name)
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
        # e5：官方前缀格式（冒号后带空格），query 检索方 / passage 语料方；
        # 其它模型（BGE-M3 等）：纯文本，前缀会被当成正文干扰语义
        if self.prefix_style == "e5":
            prefixed = [f"{role}: {t}" for t in texts]
        else:
            prefixed = list(texts)
        vecs = model.encode(prefixed, convert_to_numpy=True,
                            normalize_embeddings=True, show_progress_bar=False)
        mat = np.asarray(vecs, dtype=np.float32)
        # 双保险：normalize_embeddings 已归一化，再归一一次防后端差异
        mat = _l2_normalize(mat)
        self.dim = mat.shape[1]
        return mat


# ---------- RemoteEmbedder ----------

# (url, model, texts) → /v1/embeddings 响应 JSON dict；可注入便于测试零网络
RemoteTransport = Callable[[str, str, list[str]], dict]


def _extract_vectors(raw: dict, expected: int) -> list[list[float]]:
    """从 /v1/embeddings 响应提取向量：按 data[].index 还原行序并校验数量。

    服务端不保证 data 数组有序（OpenAI 规范以 index 为准），乱序返回直接
    拼接会把向量挂错文本——必须排序后再取 embedding。
    """
    items = raw.get("data")
    if not isinstance(items, list) or not items:
        raise ValueError(f"远程嵌入响应缺少 data 数组：{str(raw)[:120]}")
    if len(items) != expected:
        raise ValueError(f"远程嵌入返回数量不符：请求 {expected} 实得 {len(items)}")
    return [list(it["embedding"]) for it in sorted(items, key=lambda it: it["index"])]


class RemoteEmbedder:
    """OpenAI 兼容 /v1/embeddings 远程嵌入器（默认按 bge-m3 无指令口径）。

    与 Local 同契约：批量文本 + role → (n, d) float32 L2 归一化矩阵，dim
    首次真实编码后回填。role 只做合法性校验不拼前缀——bge-m3 官方口径
    纯文本，与 LocalEmbedder 的 plain 风格同一语义空间，切换后端无需重
    校准阈值。大目录分批（batch_size）；客户端侧统一再归一化一次防端点
    未归一化的差异；维度漂移立即 ValueError（宁可失败不落错向量）。
    """

    def __init__(self, base_url: str, model: str = "bge-m3",
                 batch_size: int = 32, timeout: float = 60.0,
                 transport: RemoteTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.batch_size = max(1, int(batch_size))
        self.timeout = timeout
        self.name = f"{model}@{self.base_url}"
        self.dim = None          # 首次真实编码后回填（与 Local 一致）
        self._transport = transport

    def _build_transport(self) -> RemoteTransport:
        """按 base_url 构建真实 urllib transport（仅首次 embed 且未注入时调用）。

        stdlib urllib：不引 httpx/openai——嵌入层保持零网络重依赖，
        mock/测试路径连 import 开销都不付。
        """
        import json
        import urllib.request

        timeout = self.timeout

        def transport(url: str, model: str, texts: list[str]) -> dict:
            payload = json.dumps({"model": model, "input": texts}).encode("utf-8")
            req = urllib.request.Request(
                url, data=payload, method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))

        return transport

    def embed(self, texts: list[str], *, role: str) -> np.ndarray:
        _validate_role(role)
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        if self._transport is None:
            self._transport = self._build_transport()
        url = f"{self.base_url}/embeddings"
        mats: list[np.ndarray] = []
        for i in range(0, len(texts), self.batch_size):
            chunk = texts[i:i + self.batch_size]
            raw = self._transport(url, self.model, chunk)
            mat = np.asarray(_extract_vectors(raw, len(chunk)), dtype=np.float32)
            if self.dim is None:
                self.dim = int(mat.shape[1])
            elif mat.shape[1] != self.dim:
                raise ValueError(
                    f"远程嵌入维度漂移：期望 {self.dim} 实得 {mat.shape[1]}"
                    f"（{self.name}）")
            mats.append(mat)
        return _l2_normalize(np.vstack(mats))


# ---------- 工厂 ----------

def make_embedder(model_name: str, mock: bool, *, backend: str = "local",
                  api_base: str = "", api_model: str = "bge-m3",
                  api_batch: int = 32, api_timeout: float = 60.0) -> Embedder:
    """mock → 确定性伪嵌入；backend=remote → 远程 /v1/embeddings 通道；
    否则（默认 local）本地 sentence-transformers 模型。

    mock 优先于 backend（测试路径零网络零模型，不受 backend 配置影响）；
    remote 时 api_base 必填——缺省空串立即 ValueError，启动即暴露配置
    错误而非首次编码才炸；backend 拼写错误同样立即拒绝。
    """
    if mock:
        return MockEmbedder()
    if backend == "remote":
        if not api_base:
            raise ValueError(
                "backend=remote 需要 api_base"
                "（环境变量 TAGPLT_EMBEDDING_API_BASE，含 /v1 前缀）")
        return RemoteEmbedder(api_base, model=api_model, batch_size=api_batch,
                              timeout=api_timeout)
    if backend != "local":
        raise ValueError(f"未知 embedding backend: {backend!r}（合法取值 local|remote）")
    return LocalEmbedder(model_name)


def build_embedder(settings: Settings) -> Embedder:
    """settings 便捷封装：生产调用点（create_app / reembed / 脚本）统一入口。"""
    return make_embedder(
        settings.embedding_model,
        settings.use_mock_embedder,
        backend=settings.embedding_backend,
        api_base=settings.embedding_api_base,
        api_model=settings.embedding_api_model,
        api_batch=settings.embedding_api_batch,
        api_timeout=settings.embedding_api_timeout,
    )
