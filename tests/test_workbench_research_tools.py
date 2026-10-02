from __future__ import annotations

from pathlib import Path
from unittest import TestCase

from research_pulse.workbench.research_tools import (
    ManagedSource,
    ReadOnlyResearchTools,
    ToolPolicyError,
    rank_sources,
)


class WorkbenchReadOnlyResearchToolsTests(TestCase):
    def setUp(self) -> None:
        self.tools = ReadOnlyResearchTools.from_fixture(
            Path("experiments/workbench-harness-v0/manifest.json"),
            run_id="run-v0",
            status_reader=lambda run_id: {
                "run_id": run_id, "status": "running", "tool_calls": 2
            },
        )

    def test_search_metadata_blocks_and_current_status_are_read_only_and_stable(self) -> None:
        sources = self.tools.invoke("search_sources", {"query": "memory governance"})
        source_id = sources[0]["source_id"]
        metadata = self.tools.invoke("read_paper_metadata", {"source_id": source_id})
        block_id = metadata["sample_block_ids"][0]
        blocks = self.tools.invoke(
            "read_managed_blocks",
            {"source_id": source_id, "block_ids": [block_id]},
        )
        status = self.tools.invoke("read_run_status", {"run_id": "run-v0"})

        self.assertEqual(metadata["source_id"], source_id)
        self.assertEqual(blocks[0]["block_id"], block_id)
        # The full block index lets the model enumerate every block (id +
        # section + page) rather than being limited to the 5 cover-page samples.
        self.assertIn("block_index", metadata)
        self.assertEqual(len(metadata["block_index"]), metadata["block_count"])
        self.assertTrue(all(set(item) >= {"block_id", "section_path"} for item in metadata["block_index"]))
        self.assertNotIn("blocks_path", repr(metadata))
        self.assertNotIn(str(Path.cwd()), repr(metadata))
        self.assertEqual(status["status"], "running")

    def test_rejects_arbitrary_paths_and_unknown_source_or_block(self) -> None:
        attempts = (
            ("read_managed_blocks", {"path": "C:/private/secret.txt"}),
            ("read_paper_metadata", {"source_id": "../../private"}),
            ("read_managed_blocks", {"source_id": "2608.21867", "block_ids": ["B1"]}),
            ("read_run_status", {"run_id": "another-run"}),
        )
        for tool_name, arguments in attempts:
            with self.subTest(tool_name=tool_name, arguments=arguments):
                with self.assertRaises(ToolPolicyError):
                    self.tools.invoke(tool_name, arguments)

    def test_rejects_write_execute_todo_publish_and_subagent_capabilities(self) -> None:
        forbidden = (
            "write_file", "edit_file", "execute", "todo", "task",
            "publish", "publish_note", "subagent", "delegate",
        )
        for tool_name in forbidden:
            with self.subTest(tool_name=tool_name):
                with self.assertRaisesRegex(ToolPolicyError, "tool is not allowed"):
                    self.tools.invoke(tool_name, {})

        self.assertEqual(
            self.tools.capability_names,
            (
                "search_sources", "read_paper_metadata",
                "read_managed_blocks", "read_run_status",
                "search_arxiv",
            ),
        )

    def test_search_arxiv_fails_closed_without_client(self) -> None:
        with self.assertRaisesRegex(ToolPolicyError, "not configured"):
            self.tools.invoke("search_arxiv", {"query": "recommender debiasing"})

    def test_search_sources_fails_closed_on_empty_catalog(self) -> None:
        empty = ReadOnlyResearchTools((), run_id="r1", status_reader=lambda _: {})
        with self.assertRaisesRegex(ToolPolicyError, "no managed papers"):
            empty.invoke("search_sources", {"query": "agent memory"})

    def test_search_arxiv_returns_external_candidates(self) -> None:
        class _Client:
            def search_papers(self, query, *, categories=None, max_results=5):
                return [{"source_id": "arxiv://2501.00001", "arxiv_id": "2501.00001", "title": "A candidate paper", "resource_uri": "arxiv://2501.00001", "url": "https://arxiv.org/abs/2501.00001"}]
        wired = ReadOnlyResearchTools((), run_id="r1", status_reader=lambda _: {}, arxiv_client=_Client())
        result = wired.invoke("search_arxiv", {"query": "debiasing", "max_results": 5})
        self.assertEqual(result[0]["arxiv_id"], "2501.00001")
        self.assertEqual(result[0]["source_id"], "arxiv://2501.00001")

    def test_search_web_normalizes_bounded_url_results_without_managed_blocks(self) -> None:
        class _Web:
            def search(self, query, *, max_results=5):
                return [
                    {"title": "Official result", "url": "https://example.com/a", "snippet": "A useful page."},
                    {"title": "Duplicate", "url": "https://example.com/a", "snippet": "ignored"},
                    {"title": "Invalid", "url": "file:///secret", "snippet": "ignored"},
                ]

        wired = ReadOnlyResearchTools(
            (), run_id="r1", status_reader=lambda _: {}, web_search_provider=_Web()
        )
        result = wired.invoke("search_web", {"query": "latest news", "max_results": 5})
        self.assertEqual(result, [{
            "title": "Official result",
            "url": "https://example.com/a",
            "snippet": "A useful page.",
            "source": "example.com",
            "published_at": None,
        }])
        self.assertIn("search_web", wired.capability_names)

    def test_search_web_exposes_safe_provider_usage_metadata(self) -> None:
        from research_pulse.workbench.web_retrieval import WebSearchResponse

        class _Web:
            def search_with_metadata(self, query, *, max_results=5):
                return WebSearchResponse(
                    results=({"title": "Official", "url": "https://example.com"},),
                    usage={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
                    model="deepseek-v4-flash",
                    response_id="resp-1",
                )

            def search(self, query, *, max_results=5):
                raise AssertionError("metadata path should be preferred")

        wired = ReadOnlyResearchTools(
            (), run_id="r1", status_reader=lambda _: {}, web_search_provider=_Web()
        )
        result = wired.invoke("search_web", {"query": "latest news", "max_results": 5})
        self.assertEqual(result[0]["url"], "https://example.com")
        self.assertEqual(wired.last_web_search_usage, {
            "kind": "web_search_usage",
            "model": "deepseek-v4-flash",
            "response_id": "resp-1",
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
        })

    def test_search_web_does_not_treat_empty_provider_response_as_success(self) -> None:
        class _EmptyWeb:
            def search(self, query, *, max_results=5):
                return []

        wired = ReadOnlyResearchTools(
            (), run_id="r1", status_reader=lambda _: {}, web_search_provider=_EmptyWeb()
        )
        with self.assertRaisesRegex(ToolPolicyError, "returned no results"):
            wired.invoke("search_web", {"query": "latest LLM recommender progress", "max_results": 5})

    def test_search_web_fails_closed_without_provider(self) -> None:
        with self.assertRaisesRegex(ToolPolicyError, "not configured"):
            self.tools.invoke("search_web", {"query": "latest news", "max_results": 5})

    def test_authority_precedes_representation_preference_in_source_ranking(self) -> None:
        original_pdf = ManagedSource(
            source_id="paper-original",
            title="Method paper",
            source_url="https://publisher.example/paper.pdf",
            blocks_path=Path("managed/original.jsonl"),
            source_authority="original_research",
            representation="pdf",
        )
        readable_secondary = ManagedSource(
            source_id="blog-html",
            title="Method paper explained",
            source_url="https://blog.example/method",
            blocks_path=Path("managed/blog.jsonl"),
            source_authority="secondary_discussion",
            representation="html",
        )

        ranked = rank_sources(
            ((3, readable_secondary), (3, original_pdf)),
            representation_preference=("html", "pdf"),
        )

        self.assertEqual([item.source_id for item in ranked], ["paper-original", "blog-html"])
