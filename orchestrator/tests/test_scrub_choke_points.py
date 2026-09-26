"""MS4CC#117: every path into the vector store scrubs, and scrubbed text leaves
no trace in the file bytes. Synthetic values only, assembled at runtime.

Needs sqlite_vec (the store's extension); skipped where it isn't installed.
"""

from __future__ import annotations

import importlib
import json
import random
import sqlite3
import string
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
sys.path.insert(0, str(HOOKS))
pytest.importorskip("sqlite_vec")

import indexer  # noqa: E402
import vectorstore  # noqa: E402
from vectorstore import Chunk, VectorStore  # noqa: E402

_rng = random.Random(1170)


def _tg_token() -> str:
    return str(_rng.randint(10**8, 10**9)) + ":" + "AA" + "".join(_rng.choice(string.ascii_letters + string.digits) for _ in range(33))


class _FakeEmbedder:
    def __init__(self, zero: bool = False) -> None:
        self.zero = zero

    def embed_batch(self, texts):
        v = 0.0 if self.zero else 0.01
        return [[v] * vectorstore.EMBEDDING_DIMS for _ in texts]


@pytest.fixture()
def store(tmp_path):
    s = VectorStore(tmp_path / "vectors.db")
    s.init_schema()
    yield s
    s.close()


def _texts(store: VectorStore) -> list[str]:
    return [t for (t,) in store._conn_or_init().execute("SELECT text FROM chunks")]


def test_upsert_is_the_choke_point(store):
    tok = _tg_token()
    ch = Chunk(source_type="memory", source_path="m.md", start_line=1, end_line=1, text=f"bot token {tok} here")
    store.upsert([ch], _FakeEmbedder().embed_batch([ch.text]))
    assert all(tok not in t for t in _texts(store))
    assert any("[REDACTED-TELEGRAM-BOT-TOKEN]" in t for t in _texts(store))


def test_secure_delete_is_on(store):
    assert store._conn_or_init().execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_deleted_chunk_leaves_no_bytes(store, tmp_path):
    # A value no scrub rule matches, so it is stored verbatim, then deleted.
    marker = "UNSCRUBBEDMARKER" + "".join(_rng.choice(string.ascii_lowercase) for _ in range(24))
    ch = Chunk(source_type="memory", source_path="gone.md", start_line=1, end_line=1, text=f"note {marker}")
    store.upsert([ch], _FakeEmbedder().embed_batch([ch.text]))
    store._conn_or_init().commit()
    store.delete_by_source_path("gone.md")
    store._conn_or_init().commit()
    store.close()
    assert marker.encode() not in (tmp_path / "vectors.db").read_bytes()


def test_index_memory_file_scrubs_before_chunking(store, tmp_path, monkeypatch):
    tok = _tg_token()
    md = tmp_path / "note.md"
    md.write_text("# Note\n\n" + "filler line\n" * 5 + f"token {tok}\n")
    seen = {}
    real = indexer.chunk_markdown

    def spy(text, path):
        seen["text"] = text
        return real(text, path)

    monkeypatch.setattr(indexer, "chunk_markdown", spy)
    ix = indexer.Indexer(store, _FakeEmbedder())
    ix.index_memory_file(md)
    assert tok not in seen["text"], "the chunker saw the raw secret"
    assert all(tok not in t for t in _texts(store))


def test_extract_turn_keeps_no_tool_result_body():
    tok = _tg_token()
    obj = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "x" * 290 + tok}]},
                {"type": "text", "text": "looks fine"},
            ],
        },
    }
    turn = indexer._extract_turn(obj, 1)
    assert turn is not None
    assert "[tool-result]" in turn["content"] and "looks fine" in turn["content"]
    assert tok[:10] not in turn["content"] and "xxxx" not in turn["content"]


def test_recall_usage_fails_closed_without_scrubber(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "scrubber", None)  # import now raises
    import recall_usage

    ru = importlib.reload(recall_usage)
    try:
        monkeypatch.setattr(ru, "LOG_PATH", tmp_path / "ru.jsonl")
        ru.log("test", "secret-bearing query " + _tg_token(), [{"chunk_id": "c"}])
        row = json.loads((tmp_path / "ru.jsonl").read_text().splitlines()[0])
        assert row["query"] == ""
    finally:
        monkeypatch.delitem(sys.modules, "scrubber")
        importlib.reload(recall_usage)


def _load_runbook():
    spec = importlib.util.spec_from_file_location(
        "rescrub_vectors", Path(__file__).resolve().parents[1] / "runbooks" / "rescrub_vectors.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _raw_insert(db: Path, text: str, source_path: str = "t.jsonl") -> None:
    """Insert a chunk WITHOUT going through upsert's scrub (an old, pre-#117 row)."""
    s = VectorStore(db)
    conn = s._conn_or_init()
    conn.execute(
        "INSERT INTO chunks (chunk_id, source_type, source_path, start_line, end_line, text, metadata_json, created_at, last_seen_at)"
        " VALUES (?, 'transcript', ?, 1, 1, ?, '{}', 0, 0)",
        ("cid" + str(_rng.random()), source_path, text),
    )
    rowid = conn.execute("SELECT max(rowid) FROM chunks").fetchone()[0]
    conn.execute(
        "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
        (rowid, vectorstore._vec_to_blob([0.02] * vectorstore.EMBEDDING_DIMS)),
    )
    conn.commit()
    s.close()


def test_runbook_apply_leaves_no_bytes_and_is_idempotent(tmp_path, monkeypatch, capsys):
    db = tmp_path / "vectors.db"
    VectorStore(db).init_schema()
    tok = _tg_token()
    _raw_insert(db, f"old turn with {tok} in it")
    rb = _load_runbook()
    monkeypatch.setitem(sys.modules, "embedder", type(sys)("embedder"))
    sys.modules["embedder"].Embedder = _FakeEmbedder
    monkeypatch.setattr(sys, "argv", ["rescrub", "--db", str(db), "--apply"])
    assert rb.main() == 0
    out = capsys.readouterr().out
    assert "removed_values_still_in_file_bytes=0" in out and tok not in out
    assert tok.encode() not in db.read_bytes()
    monkeypatch.setattr(sys, "argv", ["rescrub", "--db", str(db)])
    rb.main()
    assert "to_rewrite=0" in capsys.readouterr().out


def test_runbook_does_not_clobber_a_row_changed_mid_run(tmp_path, monkeypatch, capsys):
    db = tmp_path / "vectors.db"
    VectorStore(db).init_schema()
    _raw_insert(db, f"old turn with {_tg_token()}")
    rb = _load_runbook()

    class RacingEmbedder(_FakeEmbedder):
        def embed_batch(self, texts):
            # A concurrent re-index rewrites the row while we embed.
            c = sqlite3.connect(db)
            c.execute("UPDATE chunks SET text = 'reindexed meanwhile', chunk_id = 'new-id'")
            c.commit()
            c.close()
            return super().embed_batch(texts)

    monkeypatch.setitem(sys.modules, "embedder", type(sys)("embedder"))
    sys.modules["embedder"].Embedder = RacingEmbedder
    monkeypatch.setattr(sys, "argv", ["rescrub", "--db", str(db), "--apply"])
    rb.main()
    assert "raced=1" in capsys.readouterr().out
    c = sqlite3.connect(db)
    assert c.execute("SELECT text FROM chunks").fetchone()[0] == "reindexed meanwhile"


def _install_embedder(monkeypatch, factory):
    monkeypatch.setitem(sys.modules, "embedder", type(sys)("embedder"))
    sys.modules["embedder"].Embedder = factory


def test_runbook_preflight_refuses_when_embedder_is_down(tmp_path, monkeypatch, capsys):
    db = tmp_path / "vectors.db"
    VectorStore(db).init_schema()
    tok = _tg_token()
    _raw_insert(db, f"old turn with {tok}")
    rb = _load_runbook()
    _install_embedder(monkeypatch, lambda: _FakeEmbedder(zero=True))
    monkeypatch.setattr(sys, "argv", ["rescrub", "--db", str(db), "--apply"])
    assert rb.main() == 3
    assert "preflight FAILED" in capsys.readouterr().out
    assert tok.encode() in db.read_bytes(), "nothing may be written when the preflight fails"


def test_runbook_outage_mid_run_is_resumable(tmp_path, monkeypatch, capsys):
    db = tmp_path / "vectors.db"
    VectorStore(db).init_schema()
    tok = _tg_token()
    _raw_insert(db, f"old turn with {tok}")
    rb = _load_runbook()

    class Flaky(_FakeEmbedder):
        calls = 0

        def embed_batch(self, texts):
            Flaky.calls += 1
            self.zero = Flaky.calls > 1  # preflight OK, then the embedder dies
            return super().embed_batch(texts)

    _install_embedder(monkeypatch, Flaky)
    monkeypatch.setattr(sys, "argv", ["rescrub", "--db", str(db), "--apply"])
    assert rb.main() == 1
    out = capsys.readouterr().out
    assert "vector_pending=1" in out
    assert tok.encode() not in db.read_bytes(), "text is scrubbed even when the vector could not be refreshed"
    # A dry run still reports the stale vector and exits non-zero.
    monkeypatch.setattr(sys, "argv", ["rescrub", "--db", str(db)])
    assert rb.main() == 1
    capsys.readouterr()
    # Embedder back: the pending vector is refreshed and the run is clean.
    _install_embedder(monkeypatch, _FakeEmbedder)
    monkeypatch.setattr(sys, "argv", ["rescrub", "--db", str(db), "--apply"])
    assert rb.main() == 0
    out = capsys.readouterr().out
    assert "refreshed=1" in out and "vector_pending=0" in out


def test_byte_gate_can_fail(tmp_path, monkeypatch, capsys):
    """A copy of a removed secret outside `chunks` (here a stray table) must
    fail the byte check and the exit code."""
    db = tmp_path / "vectors.db"
    VectorStore(db).init_schema()
    pw = "Zq9!" + "".join(_rng.choice(string.ascii_letters + string.digits) for _ in range(8))
    _raw_insert(db, f"config password: {pw}")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE stray (t TEXT)")
    c.execute("INSERT INTO stray VALUES (?)", (f"copy {pw}",))
    c.commit()
    c.close()
    rb = _load_runbook()
    _install_embedder(monkeypatch, _FakeEmbedder)
    monkeypatch.setattr(sys, "argv", ["rescrub", "--db", str(db), "--apply"])
    assert rb.main() == 1
    assert "removed_values_still_in_file_bytes=1" in capsys.readouterr().out


def test_backup_is_owner_only(tmp_path, monkeypatch, capsys):
    import stat

    db = tmp_path / "vectors.db"
    VectorStore(db).init_schema()
    _raw_insert(db, f"old turn with {_tg_token()}")
    rb = _load_runbook()
    _install_embedder(monkeypatch, _FakeEmbedder)
    monkeypatch.setattr(sys, "argv", ["rescrub", "--db", str(db), "--apply"])
    rb.main()
    capsys.readouterr()
    backups = list(tmp_path.glob("vectors.db.bak-rescrub-*"))
    assert len(backups) == 1
    # A second apply in the same second must not collide with the first backup.
    _raw_insert(db, f"another old turn with {_tg_token()}")
    monkeypatch.setattr(sys, "argv", ["rescrub", "--db", str(db), "--apply"])
    rb.main()
    capsys.readouterr()
    assert len(list(tmp_path.glob("vectors.db.bak-rescrub-*"))) == 2
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


def test_extract_turn_scrubs_each_turn():
    tok = _tg_token()
    turn = indexer._extract_turn({"type": "user", "message": {"role": "user", "content": f"my bot is {tok}"}}, 1)
    assert tok not in turn["content"] and "[REDACTED-TELEGRAM-BOT-TOKEN]" in turn["content"]


def test_extract_turn_drops_bash_mode_output():
    turn = indexer._extract_turn(
        {"type": "user", "message": {"role": "user", "content": "<bash-stdout>\"TOKEN\" => \"secretplistvalue\"</bash-stdout> ok"}},
        1,
    )
    assert "secretplistvalue" not in turn["content"] and "[output omitted]" in turn["content"]


def test_runbook_vacuum_purges_legacy_free_pages(tmp_path, monkeypatch, capsys):
    """Stores written before secure_delete have deleted rows' text on free
    pages; only VACUUM removes it."""
    db = tmp_path / "vectors.db"
    VectorStore(db).init_schema()
    _raw_insert(db, f"old turn with {_tg_token()}")  # something for the runbook to rewrite
    legacy = "LEGACYFREEPAGE" + "".join(_rng.choice(string.ascii_lowercase) for _ in range(30))
    c = sqlite3.connect(db)  # plain connection: secure_delete OFF, like an old install
    c.execute("PRAGMA secure_delete=OFF")
    c.execute(
        "INSERT INTO chunks (chunk_id, source_type, source_path, start_line, end_line, text, metadata_json, created_at, last_seen_at)"
        " VALUES ('old', 'memory', 'old.md', 1, 1, ?, '{}', 0, 0)",
        ("x" * 20000 + legacy,),  # spans overflow pages, which go to the freelist
    )
    c.commit()
    c.execute("DELETE FROM chunks WHERE chunk_id = 'old'")
    c.commit()
    c.close()
    assert legacy.encode() in db.read_bytes(), "precondition: the deleted text is still on a free page"
    rb = _load_runbook()
    monkeypatch.setitem(sys.modules, "embedder", type(sys)("embedder"))
    sys.modules["embedder"].Embedder = _FakeEmbedder
    monkeypatch.setattr(sys, "argv", ["rescrub", "--db", str(db), "--apply"])
    rb.main()
    capsys.readouterr()
    assert legacy.encode() not in db.read_bytes()


def test_runbook_hunts_a_redacted_value_across_rows(tmp_path, monkeypatch, capsys):
    db = tmp_path / "vectors.db"
    VectorStore(db).init_schema()
    val = "".join(_rng.choice("0123456789abcdef") for _ in range(40))
    _raw_insert(db, f"config token: {val}")  # context gives it away here
    _raw_insert(db, f"earlier I saw {val} mid-sentence", source_path="u.jsonl")  # no context here
    rb = _load_runbook()
    monkeypatch.setitem(sys.modules, "embedder", type(sys)("embedder"))
    sys.modules["embedder"].Embedder = _FakeEmbedder
    monkeypatch.setattr(sys, "argv", ["rescrub", "--db", str(db), "--apply"])
    rb.main()
    out = capsys.readouterr().out
    assert "rows changed only by that hunt=1" in out and val not in out
    assert val.encode() not in db.read_bytes()
