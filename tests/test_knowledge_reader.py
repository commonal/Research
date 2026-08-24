from __future__ import annotations

from hashlib import sha256
import inspect
from pathlib import Path
from unittest import TestCase

import research_pulse.knowledge.reader as reader_module
from research_pulse.knowledge.models import KnowledgeAsset
from research_pulse.knowledge.reader import FilesystemKnowledgeReader
from research_pulse.production.adapters import render_asset_markdown


ROOT = Path(__file__).resolve().parents[1]


class KnowledgeReaderTests(TestCase):
    def test_contract_has_no_framework_or_database_dependency(self) -> None:
        source = inspect.getsource(reader_module).casefold()

        self.assertNotIn("fastapi", source)
        self.assertNotIn("postgres", source)
        self.assertNotIn("typescript", source)

    def test_empty_vault_returns_no_timeline_or_detail(self) -> None:
        vault = ROOT / "data" / "test-empty-reader-vault"
        reader = FilesystemKnowledgeReader(vault)

        self.assertEqual(reader.recent(limit=10), [])
        self.assertIsNone(reader.get_current("kp:missing"))

    def test_reader_selects_latest_published_valid_version(self) -> None:
        vault = ROOT / "data" / "test-reader-vault"
        paths = [
            _write_asset(vault, "kp:test:paper", "2026-08-20T00:00:00Z", "published", "# Old\n"),
            _write_asset(vault, "kp:test:paper", "2026-08-21T00:00:00Z", "published", "# Current\n"),
            _write_asset(vault, "kp:test:paper", "2026-08-22T00:00:00Z", "draft", "# Draft\n"),
            _write_asset(vault, "kp:test:draft-only", "2026-08-22T00:00:00Z", "draft", "# Draft\n"),
        ]
        broken = vault / "papers" / "broken" / "broken.md"
        invalid_version = vault / "papers" / "invalid" / "invalid.md"
        broken.parent.mkdir(parents=True, exist_ok=True)
        invalid_version.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("not markdown", encoding="utf-8")
        invalid_version.write_text(
            paths[0].read_text(encoding="utf-8").replace("2026-08-20T00:00:00Z", "not-a-version"),
            encoding="utf-8",
        )
        paths.extend((broken, invalid_version))
        try:
            reader = FilesystemKnowledgeReader(vault)
            timeline = reader.recent(limit=10)
            detail = reader.get_current("kp:test:paper")
        finally:
            for path in paths:
                path.unlink(missing_ok=True)

        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0].knowledge_version, "2026-08-21T00:00:00Z")
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.markdown, "# Current\n")
        self.assertEqual(detail.source_urls, ("https://example.com/paper",))
        self.assertIsNone(reader.get_current("kp:test:draft-only"))


def _write_asset(vault: Path, knowledge_id: str, version: str, status: str, body: str) -> Path:
    asset = KnowledgeAsset(
        knowledge_id=knowledge_id,
        knowledge_version=version,
        publication_status=status,
        evidence_level="full_text_text",
        source_urls=("https://example.com/paper",),
        domain="test",
        title=f"Paper {version}",
        body=body,
        content_sha256=sha256(body.encode("utf-8")).hexdigest(),
    )
    path = vault / "papers" / version.replace(":", "-") / "asset.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_asset_markdown(asset), encoding="utf-8")
    return path
