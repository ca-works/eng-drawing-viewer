"""Runs last: nothing the app did during the whole test session changed any PDF."""
import hashlib
from pathlib import Path


def test_corpus_files_unchanged(corpus, corpus_hashes):
    assert corpus_hashes, "baseline hashes missing"
    for path, (digest, mtime) in corpus_hashes.items():
        assert path.exists(), f"{path} disappeared"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, f"{path.name} was modified"
        assert path.stat().st_mtime_ns == mtime, f"{path.name} timestamp changed"
    # No stray files were written next to the drawings either.
    now = {p for p in Path(corpus["root"]).rglob("*") if p.is_file()}
    assert now == set(corpus_hashes)
