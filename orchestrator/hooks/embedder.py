"""OpenAI embedding client with secret scrubbing.

Thin wrapper around the OpenAI embeddings API. Loads the key from an env
var or from ~/.config/openai-api-key (in that order). Scrubs obvious
secret-shaped tokens from text before sending, so accidental key leaks
in transcripts don't end up embedded in the vector store.

Usage:
    from embedder import Embedder
    e = Embedder()
    vec = e.embed("some text")         # single string → list[float]
    vecs = e.embed_batch(["a", "b"])   # batch → list[list[float]]
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

DEFAULT_MODEL = "text-embedding-3-small"
DEFAULT_DIMS = 1536  # text-embedding-3-small default
BATCH_SIZE = 96      # OpenAI allows up to 2048; 96 is conservative per-request
API_KEY_FILE = Path.home() / ".config" / "openai-api-key"

# ---------------------------------------------------------------------------
# Secret scrubbing
# ---------------------------------------------------------------------------

# Regex patterns for common secret shapes. Applied to any text BEFORE embedding.
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
    # SSH-style keys too
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
# API key loading
# ---------------------------------------------------------------------------

def load_api_key() -> str | None:
    """Load OpenAI API key from env var or ~/.config/openai-api-key.

    Returns None if neither source has a valid-looking key.
    """
    env_key = os.environ.get("OPENAI_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()

    if API_KEY_FILE.exists():
        try:
            content = API_KEY_FILE.read_text().strip()
            if content:
                return content
        except Exception:
            pass

    return None

# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------

class Embedder:
    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None):
        self.model = model
        self.api_key = api_key or load_api_key()
        if not self.api_key:
            raise RuntimeError(
                "No OpenAI API key found. Set OPENAI_API_KEY env var "
                f"or write the key to {API_KEY_FILE}."
            )
        self._client = None

    def _client_or_init(self):
        if self._client is None:
            openai = _get_openai()
            self._client = openai.OpenAI(api_key=self.api_key)
        return self._client

    def embed(self, text: str) -> list[float]:
        """Embed a single string. Returns a list of floats."""
        clean = scrub(text) if text else ""
        if not clean:
            return [0.0] * DEFAULT_DIMS  # Zero vector for empty input
        client = self._client_or_init()
        resp = client.embeddings.create(model=self.model, input=clean)
        return resp.data[0].embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of strings. Returns a list of vectors.

        Chunks into API-friendly batch sizes and scrubs each input.
        """
        if not texts:
            return []
        client = self._client_or_init()
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH_SIZE):
            batch = [scrub(t) if t else "" for t in texts[i : i + BATCH_SIZE]]
            # Replace empty strings with a single space so OpenAI doesn't reject.
            batch = [b if b else " " for b in batch]
            resp = client.embeddings.create(model=self.model, input=batch)
            # resp.data is ordered to match input
            out.extend(item.embedding for item in resp.data)
        return out

# ---------------------------------------------------------------------------
# Self-test (run as `python3 embedder.py`)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    e = Embedder()
    v = e.embed("Hello, I am Cairn.")
    print(f"Single embed: dim={len(v)}, first-3={v[:3]}")
    vs = e.embed_batch(["a test", "another test"])
    print(f"Batch embed: got {len(vs)} vectors, dim={len(vs[0])}")
    # Scrub test
    sample = "my key is sk-proj-ABC123DEFGHIJKLMNOP and my github is ghp_AAABBBCCCDDDEEEFFFGGGHHHIIIJJJ"
    print(f"Scrub test: {scrub(sample)}")
