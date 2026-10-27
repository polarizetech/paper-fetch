# pyright: reportMissingImports=false
# (onnxruntime, huggingface_hub and tokenizers come only with the `rerank` extra.)
"""Optional reranking: hybrid retrieval finds passages on the topic; a cross-encoder finds the ones
that answer the query.

    PAPER_FETCH_RERANK_MODEL=onnx-community/bge-reranker-v2-m3-ONNX::onnx/model_int8.onnx

A cross-encoder scores (query, passage) pairs. It is deterministic and generates nothing. It runs
with onnxruntime, no torch (the `rerank` extra); the model is downloaded from the Hugging Face hub
on first use. Unset, passages keep their fused retrieval order.

Or a hosted cross-encoder, so no model is held in memory here:

    PAPER_FETCH_RERANK_URL=https://api.example.com/v1   # serves POST /rerank
    PAPER_FETCH_RERANK_MODEL=BAAI/bge-reranker-v2-m3    # the provider's name for it
    PAPER_FETCH_RERANK_KEY_FILE=~/.config/paper-fetch/rerank.key

The request is the Cohere/Jina form ({model, query, documents}), which most rerank services and
self-hosted servers accept; answers as `results` with `relevance_score`, as a list of
`{index, score}`, or as `scores` are all read.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from .http import Http
from .passages import Passage, key_from_env

__all__ = ["ApiReranker", "OnnxReranker", "Reranker", "reranker_from_env"]


class Reranker(Protocol):
    name: str

    def score(self, query: str, passages: list[Passage]) -> list[float]: ...


class OnnxReranker:
    """A cross-encoder from the Hugging Face hub in ONNX form, e.g. a quantised bge-reranker."""

    def __init__(
        self, repo: str, onnx_file: str = "onnx/model_quantized.onnx", max_length: int = 512
    ) -> None:
        try:
            import numpy as np  # noqa: PLC0415 -- the optional `rerank` extra
            import onnxruntime as ort  # noqa: PLC0415
            from huggingface_hub import hf_hub_download  # noqa: PLC0415
            from tokenizers import Tokenizer  # noqa: PLC0415
        except ImportError as e:
            raise RuntimeError(f"reranking needs paper-fetch[rerank]: {e}") from e
        self._np: Any = np
        self.name = f"onnx:{repo}/{onnx_file}"
        self.session = ort.InferenceSession(
            hf_hub_download(repo, onnx_file), providers=["CPUExecutionProvider"]
        )
        self.tokenizer = Tokenizer.from_file(hf_hub_download(repo, "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=max_length)
        self.inputs = {i.name for i in self.session.get_inputs()}

    def score(self, query: str, passages: list[Passage], batch: int = 8) -> list[float]:
        np = self._np
        out: list[float] = []
        for i in range(0, len(passages), batch):
            enc = self.tokenizer.encode_batch([(query, p.text) for p in passages[i : i + batch]])
            width = max(len(e.ids) for e in enc)
            feed = {
                "input_ids": np.array(
                    [e.ids + [1] * (width - len(e.ids)) for e in enc], dtype=np.int64
                ),
                "attention_mask": np.array(
                    [e.attention_mask + [0] * (width - len(e.ids)) for e in enc], dtype=np.int64
                ),
                "token_type_ids": np.array(
                    [e.type_ids + [0] * (width - len(e.ids)) for e in enc], dtype=np.int64
                ),
            }
            logits = self.session.run(None, {k: v for k, v in feed.items() if k in self.inputs})[0]
            out.extend(float(x) for x in np.asarray(logits).reshape(len(enc), -1)[:, 0])
        return out


class ApiReranker:
    """A cross-encoder behind a hosted `/rerank` endpoint. Scores come back in passage order."""

    def __init__(
        self, model: str, url: str, *, key: str = "", timeout: float = 60, http: Http | None = None
    ) -> None:
        self.model, self.url, self.timeout = model, url.rstrip("/"), timeout
        self.name = f"api:{self.url}/{model}"
        self.headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.http = http or Http()

    def score(self, query: str, passages: list[Passage]) -> list[float]:
        if not passages:
            return []
        payload = {"model": self.model, "query": query, "documents": [p.text for p in passages]}
        try:
            body = self.http.post_json(
                f"{self.url}/rerank", payload, timeout=self.timeout, headers=self.headers
            )
        except ConnectionError as e:
            raise ConnectionError(f"reranker {self.model}: {e}") from e
        if isinstance(body, dict) and "scores" in body:
            scores = [float(x) for x in body["scores"]]
        else:
            rows = body["results"] if isinstance(body, dict) else body
            scores = [float("-inf")] * len(passages)
            for row in rows:
                scores[int(row["index"])] = float(row.get("relevance_score", row.get("score")))
        if len(scores) != len(passages):
            raise ConnectionError(
                f"reranker {self.model}: {len(scores)} scores for {len(passages)} passages"
            )
        return scores


def reranker_from_env() -> Reranker | None:
    spec = os.environ.get("PAPER_FETCH_RERANK_MODEL", "").strip()
    if not spec:
        return None
    url = os.environ.get("PAPER_FETCH_RERANK_URL", "").strip()
    if url:
        return ApiReranker(spec, url, key=key_from_env("PAPER_FETCH_RERANK_KEY"))
    repo, _, file = spec.partition("::")
    return OnnxReranker(repo, file) if file else OnnxReranker(repo)
