"""Lazy local NLI. Normalize over relations for each pair, never over labels."""
from __future__ import annotations

import logging
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from itertools import chain
from multiprocessing import get_context
from threading import Lock
from time import monotonic

import numpy as np

logger = logging.getLogger(__name__)


def entailment_scores(logits, label2id: dict) -> list[float]:
    ids = [int(i) for name, i in label2id.items() if name.casefold() == "entailment"]
    values = np.asarray(logits, dtype=np.float64)
    if (len(ids) != 1 or values.ndim != 2 or values.shape[1] < 2
            or not 0 <= ids[0] < values.shape[1] or not np.isfinite(values).all()):
        raise ValueError("NLI 必须提供有效 logits 和明确的 entailment 标签映射")
    exp = np.exp(values - values.max(axis=1, keepdims=True))
    return (exp[:, ids[0]] / exp.sum(axis=1)).tolist()


class _NLIModel:
    def __init__(self, settings):
        self.settings = settings
        self.model = None
        self.tokenizer = None
        self.model_load_elapsed_s = 0.0

    def score(self, pairs: list[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        import torch

        if self.model is None:
            started = monotonic()
            try:
                from transformers import AutoModelForSequenceClassification, AutoTokenizer

                name = self.settings.xmlclass_nli_model
                tokenizer = AutoTokenizer.from_pretrained(name)
                # FP16 forward is substantially slower than FP32 on this CPU backend.
                model_options = {"dtype": torch.float32} if self.settings.xmlclass_nli_device == "cpu" else {}
                model = AutoModelForSequenceClassification.from_pretrained(name, **model_options)
                model.to(self.settings.xmlclass_nli_device).eval()
                self.tokenizer, self.model = tokenizer, model
            finally:
                self.model_load_elapsed_s += monotonic() - started
        scores = []
        size = self.settings.xmlclass_nli_batch_size
        for start in range(0, len(pairs), size):
            batch = pairs[start:start + size]
            inputs = self.tokenizer(
                [p for p, _ in batch], [h for _, h in batch], padding=True,
                truncation="only_first", max_length=self.settings.xmlclass_nli_max_length,
                return_tensors="pt",
            ).to(self.settings.xmlclass_nli_device)
            with torch.inference_mode():
                logits = self.model(**inputs).logits.float().cpu().numpy()
            scores.extend(entailment_scores(logits, self.model.config.label2id))
        return scores


_worker_model: _NLIModel | None = None


def _init_worker(settings):
    import torch

    torch.set_num_threads(settings.xmlclass_nli_threads)
    torch.set_num_interop_threads(1)
    global _worker_model
    _worker_model = _NLIModel(settings)


def _score_batch(batch):
    assert _worker_model is not None
    before = _worker_model.model_load_elapsed_s
    scores = _worker_model.score(batch)
    return scores, _worker_model.model_load_elapsed_s - before


class LocalNLI:
    """Distribute ordered CPU batches across workers owned by one benchmark."""

    def __init__(self, settings):
        self.settings = settings
        self.model_load_elapsed_s = 0.0
        self._lock = Lock()
        self._pool = None
        self._local = (_NLIModel(settings)
                       if settings.xmlclass_nli_device != "cpu" else None)

    def score(self, pairs: list[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        with self._lock:
            if self._local is not None:
                try:
                    return self._local.score(pairs)
                finally:
                    self.model_load_elapsed_s = self._local.model_load_elapsed_s
            if self._pool is None:
                logger.info("NLI 启动 %d 个推理进程，每进程 %d 个线程，首次推理时加载模型",
                            self.settings.xmlclass_nli_processes, self.settings.xmlclass_nli_threads)
                self._pool = ProcessPoolExecutor(
                    max_workers=self.settings.xmlclass_nli_processes,
                    mp_context=get_context("spawn"),
                    initializer=_init_worker,
                    initargs=(self.settings,),
                )
            size = self.settings.xmlclass_nli_batch_size
            total_batches = (len(pairs) + size - 1) // size
            started = monotonic()
            logger.info("NLI 推理开始：%d 对，%d 批，每批最多 %d 对",
                        len(pairs), total_batches, size)
            futures = {
                self._pool.submit(_score_batch, pairs[start:start + size]): index
                for index, start in enumerate(range(0, len(pairs), size))
            }
            pending = set(futures)
            completed_batches = completed_pairs = 0
            longest_load = 0.0
            ordered_scores = [[] for _ in range(total_batches)]
            try:
                while pending:
                    done, pending = wait(pending, timeout=15, return_when=FIRST_COMPLETED)
                    for future in done:
                        scores, load_s = future.result()
                        ordered_scores[futures[future]] = scores
                        completed_batches += 1
                        completed_pairs += len(scores)
                        if load_s > 0:
                            logger.info("NLI 推理进程已完成模型加载，耗时 %.1f 秒", load_s)
                        longest_load = max(longest_load, load_s)
                    logger.info("NLI 进度：%d/%d 批，%d/%d 对，已用 %.1f 秒",
                                completed_batches, total_batches, completed_pairs, len(pairs),
                                monotonic() - started)
            except Exception:
                for future in pending:
                    future.cancel()
                logger.exception("NLI 推理失败，已完成 %d/%d 对", completed_pairs, len(pairs))
                raise
            finally:
                self.model_load_elapsed_s += longest_load
            return list(chain.from_iterable(ordered_scores))

    def close(self):
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
