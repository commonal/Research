from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
from hashlib import sha256
from io import StringIO
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch
import json
import os
import shutil
import uuid

from research_pulse.knowledge.models import (
    DurableEvidenceAnchor,
    KnowledgeAsset,
    KnowledgeAssetError,
    KnowledgeBundle,
    KnowledgeClaim,
)
from research_pulse.production.adapters import FilesystemKnowledgePublisher, PostgresProcessedPaperRegistry
from research_pulse.production.publication import (
    ManifestStore,
    PublicationManifest,
    PublicationReconciler,
    ReconciliationSummary,
    RecoveryItem,
    safe_error,
)
from research_pulse.production.reconcile import main as reconcile_main
from research_pulse.rag.chunking import chunk_bundle
from research_pulse.rag.contracts import IndexReceipt, SearchRequest
from research_pulse.rag.postgres import PostgresResearchRAG


ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.getenv("RESEARCH_PULSE_TEST_DATABASE_URL")


class _Rag:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
        self.indexed: set[tuple[str, str, str]] = set()

    def publish(self, bundle: KnowledgeBundle) -> IndexReceipt:
        self.calls += 1
        if self.fail:
            raise RuntimeError("index unavailable token=super-secret-value")
        chunks = chunk_bundle(bundle)
        self.indexed.update(
            (bundle.asset.knowledge_id, bundle.asset.knowledge_version, chunk.chunk_id) for chunk in chunks
        )
        return IndexReceipt(
            bundle.asset.knowledge_id,
            bundle.asset.knowledge_version,
            tuple(chunk.chunk_id for chunk in chunks),
        )

    def search(self, request):
        return []


class _Registry:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.marked: set[tuple[str, str]] = set()

    def mark_processed(self, source_id: str, knowledge_id: str) -> None:
        if self.fail:
            raise RuntimeError("registry unavailable password=do-not-store")
        self.marked.add((source_id, knowledge_id))


class _FailingStore(ManifestStore):
    def __init__(self, vault_root: Path, fail_on_writes: set[int]) -> None:
        super().__init__(vault_root)
        self.fail_on_writes = fail_on_writes
        self.write_count = 0

    def write(self, path: Path, manifest: PublicationManifest) -> PublicationManifest:
        self.write_count += 1
        if self.write_count in self.fail_on_writes:
            raise OSError(f"injected manifest write {self.write_count}")
        return super().write(path, manifest)


class PublicationContractTests(TestCase):
    def test_manifest_and_receipt_reject_invalid_identity_paths_hashes_and_state(self) -> None:
        asset = _bundle().asset
        pending = _pending_manifest(asset)
        invalid_values = (
            ("source_id", ""),
            ("markdown_path", "../outside.md"),
            ("markdown_path", "C:/outside.md"),
            ("content_sha256", "short"),
            ("index_status", "unknown"),
        )
        for field, value in invalid_values:
            payload = pending.to_payload()
            payload[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(KnowledgeAssetError):
                PublicationManifest.from_payload(payload)
        with self.assertRaises(ValueError):
            IndexReceipt("", "v1", ("chunk:1",))
        with self.assertRaises(ValueError):
            IndexReceipt("kid", "v1", ())
        with self.assertRaises(ValueError):
            RecoveryItem("", "recovered")
        with self.assertRaises(ValueError):
            RecoveryItem("manifest.json", "unknown")

    def test_manifest_store_round_trip_is_deterministic_and_indexed_never_downgrades(self) -> None:
        with _vault() as vault:
            store = ManifestStore(vault)
            paths = store.paths_for(_bundle().asset)
            pending = _pending_manifest(_bundle().asset, store=store, paths=paths)
            store.write(paths.manifest, pending)
            first_bytes = paths.manifest.read_bytes()
            store.write(paths.manifest, pending)
            self.assertEqual(paths.manifest.read_bytes(), first_bytes)
            indexed = pending.indexed(IndexReceipt(pending.knowledge_id, pending.knowledge_version, ("chunk:1",)))
            store.write(paths.manifest, indexed)

            result = store.write(paths.manifest, pending.failed(RuntimeError("late failure")))

            self.assertEqual(result.index_status, "indexed")
            self.assertEqual(store.read(paths.manifest).index_status, "indexed")
            with self.assertRaisesRegex(KnowledgeAssetError, "cannot be reused"):
                store.write(paths.manifest, replace(indexed, content_sha256="c" * 64))
            paths.manifest.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(KnowledgeAssetError, "invalid JSON"):
                store.read(paths.manifest)

    def test_paths_are_vault_scoped_and_scan_excludes_fixtures(self) -> None:
        with _vault() as vault:
            store = ManifestStore(vault)
            with self.assertRaises(KnowledgeAssetError):
                store.managed_path("../outside.md")
            with self.assertRaises(KnowledgeAssetError):
                store.managed_path("C:/outside.md")
            fixture = vault / "fixtures" / "ignored.md"
            fixture.parent.mkdir(parents=True)
            fixture.write_text("ignored", encoding="utf-8")
            self.assertEqual(store.iter_markdown_paths(), ())

            outside = vault.parent / "outside.md"
            with patch.object(Path, "resolve", autospec=True, return_value=outside):
                with self.assertRaisesRegex(KnowledgeAssetError, "escapes"):
                    store.ensure_managed(vault / "papers" / "linked" / "paper.md")

    def test_safe_error_redacts_secrets_and_summary_counts_are_stable(self) -> None:
        code, summary = safe_error(
            RuntimeError("Bearer abc.def token=my-token password=hunter2 sk-12345678901234567890\nraw")
        )
        self.assertEqual(code, "RuntimeError")
        self.assertNotIn("hunter2", summary)
        self.assertNotIn("my-token", summary)
        self.assertNotIn("sk-", summary)
        self.assertNotIn("abc.def", summary)
        result = ReconciliationSummary(
            (
                RecoveryItem("a", "recovered"),
                RecoveryItem("b", "damaged"),
                RecoveryItem("c", "skipped"),
            )
        )
        self.assertEqual(result.to_payload()["recovered"], 1)
        self.assertTrue(result.has_failures)


class RecoverablePublisherTests(TestCase):
    def test_success_commits_bundle_manifest_index_and_processed_identity(self) -> None:
        with _vault() as vault:
            rag, registry = _Rag(), _Registry()
            manifest = FilesystemKnowledgePublisher(vault, rag, registry).publish(_bundle(), "paper-test")
            store = ManifestStore(vault)
            paths = store.paths_for(_bundle().asset)

            self.assertTrue(paths.markdown.exists())
            self.assertTrue(paths.provenance.exists())
            self.assertTrue(paths.manifest.exists())
            self.assertEqual(manifest.index_status, "indexed")
            self.assertEqual(store.read(paths.manifest), manifest)
            self.assertEqual(registry.marked, {("paper-test", _bundle().asset.knowledge_id)})
            self.assertEqual(len(rag.indexed), 1)

    def test_manifest_or_markdown_failure_before_commit_never_calls_projections(self) -> None:
        for failure in ("manifest", "markdown"):
            with self.subTest(failure=failure), _vault() as vault:
                rag, registry = _Rag(), _Registry()
                store = _FailingStore(vault, {1}) if failure == "manifest" else ManifestStore(vault)
                publisher = FilesystemKnowledgePublisher(vault, rag, registry, store)
                if failure == "manifest":
                    context = self.assertRaisesRegex(OSError, "manifest write")
                else:
                    real_write = Path.write_text

                    def fail_markdown(path: Path, value: str, **kwargs):
                        if path.name.endswith(".md.tmp"):
                            raise OSError("injected markdown failure")
                        return real_write(path, value, **kwargs)

                    context = patch.object(Path, "write_text", autospec=True, side_effect=fail_markdown)
                with context:
                    if failure == "markdown":
                        with self.assertRaisesRegex(OSError, "markdown failure"):
                            publisher.publish(_bundle(), "paper-test")
                    else:
                        publisher.publish(_bundle(), "paper-test")
                self.assertEqual(list((vault / "papers").rglob("*.md")), [])
                self.assertEqual(rag.calls, 0)
                self.assertEqual(registry.marked, set())

    def test_index_failure_is_recovered_without_changing_canonical_bytes(self) -> None:
        with _vault() as vault:
            rag, registry = _Rag(fail=True), _Registry()
            publisher = FilesystemKnowledgePublisher(vault, rag, registry)
            with self.assertRaisesRegex(RuntimeError, "index unavailable"):
                publisher.publish(_bundle(), "paper-test")
            store = ManifestStore(vault)
            paths = store.paths_for(_bundle().asset)
            before = (paths.markdown.read_bytes(), paths.provenance.read_bytes())
            failed = store.read(paths.manifest)
            self.assertEqual(failed.index_status, "index_failed")
            self.assertEqual(failed.attempt_count, 1)
            self.assertNotIn("super-secret-value", failed.last_error_summary or "")
            self.assertEqual(registry.marked, set())

            still_failed = PublicationReconciler(store, rag, registry).reconcile()
            self.assertEqual(still_failed.count("failed"), 1)
            self.assertEqual(store.read(paths.manifest).attempt_count, 2)
            rag.fail = False
            summary = PublicationReconciler(store, rag, registry).reconcile()

            self.assertEqual(summary.count("recovered"), 1)
            self.assertEqual(store.read(paths.manifest).index_status, "indexed")
            self.assertEqual(before, (paths.markdown.read_bytes(), paths.provenance.read_bytes()))
            self.assertEqual(registry.marked, {("paper-test", _bundle().asset.knowledge_id)})

    def test_registry_and_final_manifest_failures_are_idempotently_recoverable(self) -> None:
        for failure in ("registry", "final_manifest"):
            with self.subTest(failure=failure), _vault() as vault:
                rag = _Rag()
                registry = _Registry(fail=failure == "registry")
                store = _FailingStore(vault, {3}) if failure == "final_manifest" else ManifestStore(vault)
                publisher = FilesystemKnowledgePublisher(vault, rag, registry, store)
                with self.assertRaises(Exception):
                    publisher.publish(_bundle(), "paper-test")
                paths = ManifestStore(vault).paths_for(_bundle().asset)
                before = (paths.markdown.read_bytes(), paths.provenance.read_bytes())

                registry.fail = False
                summary = PublicationReconciler(ManifestStore(vault), rag, registry).reconcile()

                self.assertEqual(summary.count("recovered"), 1)
                self.assertEqual(ManifestStore(vault).read(paths.manifest).index_status, "indexed")
                self.assertEqual(len(rag.indexed), 1)
                self.assertEqual(before, (paths.markdown.read_bytes(), paths.provenance.read_bytes()))

    def test_draft_legacy_and_missing_anchor_cannot_enter_publication(self) -> None:
        with _vault() as vault:
            rag, registry = _Rag(), _Registry()
            with self.assertRaisesRegex(ValueError, "quality-approved"):
                FilesystemKnowledgePublisher(vault, rag, registry).publish(
                    replace(_bundle(), asset=replace(_bundle().asset, publication_status="draft")), "paper-test"
                )
            legacy = KnowledgeBundle(_bundle().asset, (), (), provenance_status="legacy_missing_provenance")
            with self.assertRaisesRegex(KnowledgeAssetError, "Legacy"):
                FilesystemKnowledgePublisher(vault, rag, registry).publish(legacy, "paper-test")
            with self.assertRaisesRegex(KnowledgeAssetError, "durable anchor"):
                KnowledgeBundle(
                    _bundle().asset,
                    (KnowledgeClaim("claim:bad", "source_fact", "unsupported", ()),),
                    (),
                )
            self.assertEqual(rag.calls, 0)


class ReconciliationTests(TestCase):
    def test_empty_indexed_corrupt_and_path_escape_are_classified(self) -> None:
        with _vault() as vault:
            store, rag, registry = ManifestStore(vault), _Rag(), _Registry()
            self.assertEqual(PublicationReconciler(store, rag, registry).reconcile().items, ())
            FilesystemKnowledgePublisher(vault, rag, registry).publish(_bundle(), "paper-test")
            summary = PublicationReconciler(store, rag, registry).reconcile()
            self.assertEqual(summary.count("already_indexed"), 1)
            self.assertEqual(rag.calls, 1)

            paths = store.paths_for(_bundle().asset)
            payload = store.read(paths.manifest).to_payload()
            payload["markdown_path"] = "../escape.md"
            paths.manifest.write_text(json.dumps(payload), encoding="utf-8")
            damaged = PublicationReconciler(store, rag, registry).reconcile()
            self.assertEqual(damaged.count("damaged"), 1)
            self.assertEqual(rag.calls, 1)

    def test_missing_manifest_migrates_only_unambiguous_arxiv_bundle(self) -> None:
        with _vault() as vault:
            initial_rag, initial_registry = _Rag(), _Registry()
            publisher = FilesystemKnowledgePublisher(vault, initial_rag, initial_registry)
            publisher.publish(_bundle(), "paper-test")
            store = ManifestStore(vault)
            paths = store.paths_for(_bundle().asset)
            paths.manifest.unlink()

            rag, registry = _Rag(), _Registry()
            summary = PublicationReconciler(store, rag, registry).reconcile()

            self.assertEqual([item.code for item in summary.items if item.code], ["manifest_created"])
            self.assertEqual(summary.count("recovered"), 1)
            self.assertEqual(store.read(paths.manifest).source_id, "paper-test")

            other = _bundle(knowledge_id="kp:test:other", source_id="other")
            FilesystemKnowledgePublisher(vault, _Rag(), _Registry()).publish(other, "other")
            other_paths = store.paths_for(other.asset)
            other_paths.manifest.unlink()
            skipped = PublicationReconciler(store, rag, registry).reconcile()
            self.assertIn("missing_source_identity", [item.code for item in skipped.items])
            self.assertFalse(other_paths.manifest.exists())

    def test_cli_reports_success_and_failure_exit_codes_without_external_providers(self) -> None:
        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = "postgresql://unused"
        try:
            cases = (
                (ReconciliationSummary(()), 0),
                (ReconciliationSummary((RecoveryItem("a", "recovered"),)), 0),
                (ReconciliationSummary((RecoveryItem("a", "damaged", code="bad"),)), 1),
            )
            for summary, expected in cases:
                class Reconciler:
                    def reconcile(self, *, include_indexed: bool = False):
                        return summary

                output = StringIO()
                with self.subTest(summary=summary), redirect_stdout(output):
                    code = reconcile_main([], reconciler_factory=lambda database_url, vault: Reconciler())
                self.assertEqual(code, expected)
                self.assertEqual(json.loads(output.getvalue())["damaged"], summary.count("damaged"))
        finally:
            if previous is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous


@skipUnless(DATABASE_URL, "RESEARCH_PULSE_TEST_DATABASE_URL is not configured")
class PostgresPublicationRecoveryTests(TestCase):
    def test_failed_publish_recovers_and_repeats_without_duplicate_rows(self) -> None:
        assert DATABASE_URL is not None
        bundle = _bundle(knowledge_id="kp:arxiv:recovery-postgres-test", source_id="recovery-postgres-test")
        rag = PostgresResearchRAG(DATABASE_URL)
        registry = PostgresProcessedPaperRegistry(DATABASE_URL)
        rag.initialize()
        registry.initialize()
        with _vault() as vault:
            failing = _Rag(fail=True)
            with self.assertRaises(RuntimeError):
                FilesystemKnowledgePublisher(vault, failing, registry).publish(bundle, "recovery-postgres-test")
            reconciler = PublicationReconciler(ManifestStore(vault), rag, registry)
            previous_database_url = os.environ.get("DATABASE_URL")
            os.environ["DATABASE_URL"] = DATABASE_URL
            try:
                first_output, second_output = StringIO(), StringIO()
                factory = lambda database_url, vault_root: reconciler
                with redirect_stdout(first_output):
                    first_code = reconcile_main(["--vault-root", str(vault)], reconciler_factory=factory)
                with redirect_stdout(second_output):
                    second_code = reconcile_main(
                        ["--vault-root", str(vault), "--check-indexed"], reconciler_factory=factory
                    )
                first_payload = json.loads(first_output.getvalue())
                second_payload = json.loads(second_output.getvalue())
            finally:
                if previous_database_url is None:
                    os.environ.pop("DATABASE_URL", None)
                else:
                    os.environ["DATABASE_URL"] = previous_database_url
            hits = rag.search(SearchRequest(query="accuracy", domain="test"))
            manifest = ManifestStore(vault).read(ManifestStore(vault).paths_for(bundle.asset).manifest)
            with rag._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT count(*) FROM knowledge_chunks WHERE knowledge_id = %s",
                        (bundle.asset.knowledge_id,),
                    )
                    chunk_count = cursor.fetchone()[0]
                    cursor.execute("SELECT knowledge_id FROM processed_papers WHERE source_id = %s", ("recovery-postgres-test",))
                    processed = cursor.fetchone()
                    cursor.execute("DELETE FROM processed_papers WHERE source_id = %s", ("recovery-postgres-test",))
                    cursor.execute("DELETE FROM knowledge_assets WHERE knowledge_id = %s", (bundle.asset.knowledge_id,))

        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 0)
        self.assertEqual(first_payload["recovered"], 1)
        self.assertEqual(second_payload["already_indexed"], 1)
        self.assertTrue(hits)
        self.assertEqual(chunk_count, 1)
        self.assertEqual(processed[0], bundle.asset.knowledge_id)
        self.assertEqual(manifest.index_status, "indexed")


def _bundle(
    *, knowledge_id: str = "kp:arxiv:paper-test", source_id: str = "paper-test"
) -> KnowledgeBundle:
    body = "# Test\n\nThe method reaches 90% accuracy.\n"
    asset = KnowledgeAsset(
        knowledge_id=knowledge_id,
        knowledge_version="2026-08-22T00:00:00+00:00",
        publication_status="published",
        evidence_level="full_text_text",
        source_urls=(f"https://arxiv.org/abs/{source_id}",),
        domain="test",
        title="Recovery Test",
        body=body,
        content_sha256=sha256(body.encode("utf-8")).hexdigest(),
    )
    excerpt = "The method reaches 90% accuracy."
    anchor = DurableEvidenceAnchor(
        anchor_id=f"source:{source_id}:results:1",
        source_url=asset.source_urls[0],
        evidence_excerpt=excerpt,
        excerpt_sha256=sha256(excerpt.encode("utf-8")).hexdigest(),
        section="Results",
    )
    return KnowledgeBundle(
        asset,
        (KnowledgeClaim("claim:test", "source_fact", excerpt, (anchor.anchor_id,)),),
        (anchor,),
    )


def _pending_manifest(
    asset: KnowledgeAsset,
    *,
    store: ManifestStore | None = None,
    paths=None,
) -> PublicationManifest:
    persisted = replace(
        asset,
        schema_version=2,
        provenance_file="v1.provenance.json",
        provenance_sha256="b" * 64,
    )
    markdown = "papers/paper/v1.md" if store is None else store.relative(paths.markdown)
    provenance = "papers/paper/v1.provenance.json" if store is None else store.relative(paths.provenance)
    return PublicationManifest.pending(
        source_id="paper-test",
        asset=persisted,
        markdown_path=markdown,
        provenance_path=provenance,
        now="2026-08-22T00:00:00+00:00",
    )


@contextmanager
def _vault():
    path = ROOT / "data" / f"test-publication-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=False)
