"""Lazy local NLI. Normalize over relations for each pair, never over labels."""
from __future__ import annotations

from threading import Lock

import numpy as np


def entailment_scores(logits, label2id: dict) -> list[float]:
    ids = [int(i) for name, i in label2id.items() if name.casefold() == "entailment"]
    values = np.asarray(logits, dtype=np.float64)
    if (len(ids) != 1 or values.ndim != 2 or values.shape[1] < 2
            or not 0 <= ids[0] < values.shape[1] or not np.isfinite(values).all()):
        raise ValueError("NLI 必须提供有效 logits 和明确的 entailment 标签映射")
    exp = np.exp(values - values.max(axis=1, keepdims=True))
    return (exp[:, ids[0]] / exp.sum(axis=1)).tolist()


class LocalNLI:
    def __init__(self, settings):
        self.settings = settings
        self.model = None
        self.tokenizer = None
        self.lock = Lock()

    def score(self, pairs: list[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        with self.lock:
            if self.model is None:
                name = self.settings.xmlclass_nli_model
                tokenizer = AutoTokenizer.from_pretrained(name)
                model = AutoModelForSequenceClassification.from_pretrained(name)
                model.to(self.settings.xmlclass_nli_device).eval()
                self.tokenizer, self.model = tokenizer, model
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
