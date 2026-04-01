"""In-memory semantic search: embed query + notes, cosine similarity in NumPy."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from .paths import iter_markdown_files, to_rel_posix
from .schema import normalize_tags
from .writer import read_frontmatter_head


@dataclass
class FindHit:
    path:  str
    score: float


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """a: (d,), b: (n, d) -> (n,)"""
    a_norm = a / (np.linalg.norm(a) + 1e-12)
    b_norm = b / (np.linalg.norm(b, axis=1, keepdims=True) + 1e-12)
    return b_norm @ a_norm


def _default_embed_fn(texts: list[str]) -> np.ndarray:
    """OpenAI embeddings; requires OPENAI_API_KEY."""
    from openai import OpenAI

    model = os.environ.get("HOMEAGENT_EMBED_MODEL", "text-embedding-3-small")
    client = OpenAI()
    resp = client.embeddings.create(input=texts, model=model)
    items = sorted(resp.data, key=lambda e: e.index)
    vecs = [e.embedding for e in items]
    return np.array(vecs, dtype=np.float64)


def build_embed_text(fm: dict | None, body: str, max_chars: int = 1200) -> str:
    if fm is None:
        fm = {}
    summary = fm.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()[:max_chars]
    title = fm.get("title")
    parts: list[str] = []
    if isinstance(title, str) and title.strip():
        parts.append(title.strip())
    body_stripped = body.strip()
    if body_stripped:
        rest = max_chars - sum(len(p) for p in parts) - 10
        if rest > 0:
            parts.append(body_stripped[:rest])
    return "\n".join(parts) if parts else body_stripped[:max_chars]


def semantic_find(
    vault_root: Path,
    query: str,
    limit: int,
    tag_filter: str | None,
    recent_days: int | None,
    embed_fn: Callable[[list[str]], np.ndarray] | None = None,
) -> list[FindHit]:
    """
    Load all matching notes, embed query + each note's summary text, rank by cosine.
    embed_fn defaults to OpenAI; inject a test double for unit tests.
    """
    import time as time_module

    embed_fn = embed_fn or _default_embed_fn

    now = time_module.time()
    day_sec = 86400.0
    cutoff = now - (recent_days * day_sec) if recent_days is not None else None

    candidates: list[tuple[str, str]] = []  # rel_posix, embed_text

    for abs_path in iter_markdown_files(vault_root):
        st = abs_path.stat()
        if cutoff is not None and st.st_mtime < cutoff:
            continue

        fm, body = read_frontmatter_head(abs_path)
        tags = normalize_tags((fm or {}).get("tags"))

        if tag_filter is not None and tag_filter.strip():
            tf = tag_filter.strip()
            if tf not in tags:
                continue

        rel = to_rel_posix(vault_root, abs_path)
        etext = build_embed_text(fm, body)
        if not etext.strip():
            etext = rel
        candidates.append((rel, etext))

    if not candidates:
        return []

    texts = [query] + [c[1] for c in candidates]
    mat = embed_fn(texts)
    qv = mat[0]
    doc_mat = mat[1:]
    sims = _cosine_sim(qv, doc_mat)

    order = np.argsort(-sims)[:limit]
    return [
        FindHit(path=candidates[i][0], score=float(sims[i]))
        for i in order
    ]
