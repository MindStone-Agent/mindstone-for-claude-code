"""Local embedding client (Ollama mxbai-embed-large) with secret scrubbing.

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
(768-dim, 8192-token context) on 2026-05-15 — OpenAI account quota exhausted,
family already on local Ollama for inference. nomic chosen over mxbai-embed-large
for its 8K context window (mxbai is 512 and overflows long memory sections).
"""

from __future__ import annotations

import os
import re
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
BATCH_SIZE = 64      # mxbai is small; conservative batch keeps Ollama responsive
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

    Defaults to local Ollama at 127.0.0.1:11434 with mxbai-embed-large.
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
        # Ollama ignores the key but the openai client requires a non-empty string.
        self.api_key = (
            api_key
            or os.environ.get("EMBEDDER_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or "ollama"
        )
        self._client = None

    def _client_or_init(self):
        if self._client is None:
            openai = _get_openai()
            self._client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def embed(self, text: str) -> list[float]:
        """Embed a single string. Returns a list of floats."""
        clean = scrub(text) if text else ""
        if not clean:
            return [0.0] * DEFAULT_DIMS
        client = self._client_or_init()
        resp = client.embeddings.create(model=self.model, input=clean)
        return resp.data[0].embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of strings. Returns a list of vectors."""
        if not texts:
            return []
        client = self._client_or_init()
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH_SIZE):
            batch = [scrub(t) if t else "" for t in texts[i : i + BATCH_SIZE]]
            batch = [b if b else " " for b in batch]
            resp = client.embeddings.create(model=self.model, input=batch)
            out.extend(item.embedding for item in resp.data)
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
