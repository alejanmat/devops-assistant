"""Unit tests for the pure helpers: chunking, stable IDs, and KB loading."""

from __future__ import annotations

import uuid


def test_chunk_text_empty_returns_empty(app_module):
    assert app_module.chunk_text("") == []
    assert app_module.chunk_text("   \n\n  ") == []


def test_chunk_text_groups_small_paragraphs_into_one(app_module):
    text = "Para one.\n\nPara two.\n\nPara three."
    chunks = app_module.chunk_text(text, size=1000, overlap=50)
    assert len(chunks) == 1
    assert "Para one." in chunks[0]
    assert "Para three." in chunks[0]


def test_chunk_text_respects_size_and_overlaps(app_module):
    # Three paragraphs, each ~40 chars; size forces a split.
    paras = [f"Paragraph number {i} " + "x" * 30 for i in range(3)]
    text = "\n\n".join(paras)
    chunks = app_module.chunk_text(text, size=60, overlap=15)
    assert len(chunks) >= 3
    # No chunk should greatly exceed size + overlap.
    assert all(len(c) <= 60 + 15 + 5 for c in chunks)


def test_chunk_text_hard_splits_oversized_paragraph(app_module):
    text = "y" * 500  # single paragraph, no blank lines
    chunks = app_module.chunk_text(text, size=100, overlap=20)
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)
    # Overlap means consecutive chunks share a tail/head.
    assert chunks[0][-20:] == chunks[1][:20]


def test_stable_point_id_is_deterministic_and_uuid(app_module):
    a = app_module._stable_point_id("02-species.md", 3)
    b = app_module._stable_point_id("02-species.md", 3)
    assert a == b
    # Different chunk index -> different id.
    assert a != app_module._stable_point_id("02-species.md", 4)
    # Valid UUID string.
    uuid.UUID(a)


def test_load_kb_documents_reads_real_knowledge_base(app_module):
    docs = app_module.load_kb_documents(app_module.KB_DIR)
    assert docs, "expected the bundled Ticino knowledge base to load"
    sources = {src for _, src, _ in docs}
    # README index must be excluded; content files must be present.
    assert "README.md" not in sources
    assert "02-species.md" in sources
    # Tuples are (text, source, index) with monotonically increasing index/file.
    for text, src, idx in docs:
        assert isinstance(text, str) and text
        assert src.endswith((".md", ".txt"))
        assert isinstance(idx, int) and idx >= 0


def test_load_kb_documents_missing_dir_returns_empty(app_module, tmp_path):
    missing = tmp_path / "does-not-exist"
    assert app_module.load_kb_documents(missing) == []


def test_load_kb_documents_skips_non_text_and_readme(app_module, tmp_path):
    (tmp_path / "README.md").write_text("index file", encoding="utf-8")
    (tmp_path / "notes.md").write_text("A useful note about carp.", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n")
    docs = app_module.load_kb_documents(tmp_path)
    sources = {src for _, src, _ in docs}
    assert sources == {"notes.md"}
