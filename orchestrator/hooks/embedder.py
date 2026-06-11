"""Local embedding client (Ollama nomic-embed-text) with secret scrubbing.

Uses Ollama's OpenAI-compatible /v1/embeddings endpoint. No API key required
for the default local-Ollama setup. Scrubs obvious secret-shaped tokens from
text before sending, so accidental key leaks in transcripts don't end up
embedded in the vector store.

Usage:
    from embedder import Embedder
    e = Embedder()
    vec = e.embed("some text")         # single string → list[float]
    vecs = e.embed_batch(["a", "b"])   # batch → list[list[float]]

Switched from OpenAI text-embedding-3-small (1536-dim) to nomic-embed-text
(768-dim) on 2026-05-15 — OpenAI account quota exhausted, family already on
local Ollama for inference.

CONTEXT CEILING (verified empirically 2026-05-29): nomic-embed-text as served
by Ollama enforces the model's TRAINED context of **2048 tokens**, NOT the 8192
implied by the Modelfile's `PARAMETER num_ctx 8192`. Past 2048 tokens Ollama
silently truncates prose and returns HTTP 400 "input length exceeds the context
length" for token-dense input (JSON / logs / hex tokenize at ~1.3–1.5
chars/token). That 400, on a single chunk inside an embed_batch() request, used
to fail the WHOLE batch and abort a transcript's indexing — silently dropping a
month of session texture. The chunker now sizes chunks to SAFE_INPUT_CHARS, and
embed_batch() degrades to per-item embedding so one bad chunk can't poison the
rest. See orchestrator/memory/project_scri_vectorization_failure_2026-05-21.md.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Lazy import — we only need openai when actually embedding.
_openai = None

def _get_openai():
    global _openai
    if _openai is None:
        import openai as _o
        _openai = _o
    return _openai

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "nomic-embed-text"
DEFAULT_DIMS = 768
DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
BATCH_SIZE = 64      # conservative batch keeps local Ollama responsive

# Hard input ceiling of the active embedding model, in tokens. nomic-embed-text
# via Ollama enforces its trained 2048-token context (see module docstring).
MAX_INPUT_TOKENS = 2048
# Char cap the chunker (indexer.py) targets and embed() enforces as a
# last-resort safety net. Sized to stay under MAX_INPUT_TOKENS even at
# worst-case tokenization density (~1.3 chars/token for hex/base64/JSON):
# 2400 / 1.3 ≈ 1846 tokens < 2048. The OpenAI build of this file sets these
# to 8191 / ~24000 instead; indexer.py reads SAFE_INPUT_CHARS from here so the
# chunker stays correct (and byte-identical) across both embedder builds.
SAFE_INPUT_CHARS = 2400

API_KEY_FILE = Path.home() / ".config" / "openai-api-key"  # only consulted if base_url overridden to cloud

# ---------------------------------------------------------------------------
# Secret scrubbing
# ---------------------------------------------------------------------------

SECRET_PATTERNS = [
    (re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"), "[REDACTED-OPENAI-KEY]"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "[REDACTED-OPENAI-KEY]"),
    (re.compile(r"ghp_[A-Za-z0-9]{30,}"), "[REDACTED-GITHUB-TOKEN]"),
    (re.compile(r"ghs_[A-Za-z0-9]{30,}"), "[REDACTED-GITHUB-TOKEN]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{30,}"), "[REDACTED-GITHUB-PAT]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED-AWS-ACCESS-KEY]"),
    (re.compile(r"xoxb-[A-Za-z0-9\-]{30,}"), "[REDACTED-SLACK-TOKEN]"),
    (re.compile(r"voyage-[A-Za-z0-9_\-]{20,}"), "[REDACTED-VOYAGE-KEY]"),
    (re.compile(r"anthropic-[A-Za-z0-9_\-]{20,}"), "[REDACTED-ANTHROPIC-KEY]"),
    (re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+PRIVATE KEY-----"), "[REDACTED-PRIVATE-KEY]"),
    (re.compile(r"ssh-(?:rsa|ed25519|ecdsa)\s+[A-Za-z0-9+/=]{100,}"), "[REDACTED-SSH-KEY]"),
]

def scrub(text: str) -> str:
    """Replace secret-shaped tokens in text with placeholders.

    Applied before embedding or storing any text chunk. Irreversible.
    """
    if not isinstance(text, str):
        return text
    for pattern, placeholder in SECRET_PATTERNS:
        text = pattern.sub(placeholder, text)
    return text

# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------

class Embedder:
    """OpenAI-compatible embedding client.

    Defaults to local Ollama at 127.0.0.1:11434 with nomic-embed-text.
    Set EMBEDDER_BASE_URL / EMBEDDER_API_KEY / EMBEDDER_MODEL to override.
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self.model = model or os.environ.get("EMBEDDER_MODEL", DEFAULT_MODEL)
        self.base_url = base_url or os.environ.get("EMBEDDER_BASE_URL", DEFAULT_BASE_URL)
        # The openai client constructor requires a non-empty key string, but the
        # default local Ollama endpoint ignores it. Use an explicit key if given,
        # else a harmless "ollama" placeholder. OPENAI_API_KEY is consulted ONLY
        # when base_url points at a cloud OpenAI host — so a local-embedding install
        # never appears to "need an OpenAI API key".
        self.api_key = (
            api_key
            or os.environ.get("EMBEDDER_API_KEY")
            or (os.environ.get("OPENAI_API_KEY") if "openai.com" in self.base_url else None)
            or "ollama"
        )
        self._client = None
        # Lossy-operation counters. Read (and reset) by callers like
        # session_end.py so a silently-truncated or dropped chunk is never
        # invisible the way the month-long failure was.
        self.stats = {"truncated": 0, "failed": 0}

    def _client_or_init(self):
        if self._client is None:
            openai = _get_openai()
            self._client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def embed(self, text: str) -> list[float]:
        """Embed a single string. Over-long input is capped to SAFE_INPUT_CHARS
        (counted in self.stats) so a stray long input can never raise. Returns a
        vector (zero-vector only if the model rejects it even after truncation)."""
        clean = scrub(text) if text else ""
        if not clean:
            return [0.0] * DEFAULT_DIMS
        client = self._client_or_init()
        return self._embed_one_safe(client, clean)

    def _embed_one_safe(self, client, text: str) -> list[float]:
        """Embed one already-scrubbed string, guaranteeing no exception escapes.

        Cap at SAFE_INPUT_CHARS up front (keeps us under the model's token
        ceiling at any density); if the model still rejects it for length,
        halve and retry; as a final fallback return a zero vector. Every lossy
        step is counted and logged so failures are never silent.
        """
        if len(text) > SAFE_INPUT_CHARS:
            text = text[:SAFE_INPUT_CHARS]
            self.stats["truncated"] += 1
        attempt = text
        for _ in range(5):
            try:
                resp = client.embeddings.create(model=self.model, input=attempt)
                return resp.data[0].embedding
            except Exception as e:
                msg = str(e).lower()
                if "context length" in msg or "maximum context" in msg or "too long" in msg or " 400" in msg:
                    # Length rejection — shrink and retry.
                    attempt = attempt[: max(256, len(attempt) // 2)]
                    self.stats["truncated"] += 1
                    continue
                # Non-length error (network, model down, etc.): don't spin.
                self.stats["failed"] += 1
                print(f"[embedder] embed failed ({type(e).__name__}: {str(e)[:120]}); "
                      f"using zero vector", file=sys.stderr)
                return [0.0] * DEFAULT_DIMS
        self.stats["failed"] += 1
        print("[embedder] embed still rejected after truncation; using zero vector",
              file=sys.stderr)
        return [0.0] * DEFAULT_DIMS

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of strings. Returns a list of vectors (1:1 with input).

        Each input is scrubbed and capped to SAFE_INPUT_CHARS, so one oversized
        item can no longer 400 the whole request. If a batch call fails anyway,
        fall back to per-item embedding so a single bad item can never drop the
        other 63 — the all-or-nothing failure that silently lost a month of
        transcript texture.
        """
        if not texts:
            return []
        client = self._client_or_init()
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH_SIZE):
            batch: list[str] = []
            for t in texts[i : i + BATCH_SIZE]:
                c = scrub(t) if t else ""
                c = c if c else " "
                if len(c) > SAFE_INPUT_CHARS:
                    c = c[:SAFE_INPUT_CHARS]
                    self.stats["truncated"] += 1
                batch.append(c)
            try:
                resp = client.embeddings.create(model=self.model, input=batch)
                out.extend(item.embedding for item in resp.data)
            except Exception as e:
                print(f"[embedder] batch embed failed ({str(e)[:100]}); "
                      f"retrying per-item", file=sys.stderr)
                for c in batch:
                    out.append(self._embed_one_safe(client, c))
        return out

# ---------------------------------------------------------------------------
# Self-test (run as `python3 embedder.py`)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    e = Embedder()
    v = e.embed("Hello, I am Hearth.")
    print(f"Single embed: model={e.model} base={e.base_url} dim={len(v)} first-3={v[:3]}")
    vs = e.embed_batch(["a test", "another test"])
    print(f"Batch embed: got {len(vs)} vectors, dim={len(vs[0])}")
    sample = "my key is sk-proj-ABC123DEFGHIJKLMNOP and my github is ghp_AAABBBCCCDDDEEEFFFGGGHHHIIIJJJ"
    print(f"Scrub test: {scrub(sample)}")
