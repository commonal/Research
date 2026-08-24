from __future__ import annotations

import unittest

from worker.analyze import AbstractAnalysis, render_markdown
from worker.discover import Subscription, build_query_url, parse_arxiv_feed


ATOM_FEED = b"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<feed xmlns=\"http://www.w3.org/2005/Atom\">
  <entry>
    <id>http://arxiv.org/abs/2608.00001v1</id>
    <updated>2026-08-22T00:00:00Z</updated>
    <published>2026-08-21T00:00:00Z</published>
    <title>  Memory   for Agents  </title>
    <summary> A   normalized\nabstract. </summary>
    <author><name>Ada Lovelace</name></author>
    <author><name>Grace Hopper</name></author>
    <link href=\"https://arxiv.org/abs/2608.00001v1\" rel=\"alternate\" type=\"text/html\" />
    <category term=\"cs.AI\" />
  </entry>
</feed>"""


class DiscoverTests(unittest.TestCase):
    def test_parse_arxiv_feed_normalizes_and_preserves_source_fields(self) -> None:
        candidates = parse_arxiv_feed(ATOM_FEED)

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.source, "arxiv")
        self.assertEqual(candidate.source_id, "2608.00001v1")
        self.assertEqual(candidate.title, "Memory for Agents")
        self.assertEqual(candidate.abstract, "A normalized abstract.")
        self.assertEqual(candidate.authors, ["Ada Lovelace", "Grace Hopper"])
        self.assertEqual(candidate.categories, ["cs.AI"])

    def test_query_is_newest_first_and_respects_max_results(self) -> None:
        subscription = Subscription(
            name="test",
            query="LLM Agent Memory",
            exclude_terms=(),
            daily_limit=3,
            max_results=10,
            source="arxiv",
        )

        url = build_query_url(subscription)

        self.assertIn("search_query=all%3A%22LLM+Agent+Memory%22", url)
        self.assertIn("max_results=10", url)
        self.assertIn("sortBy=submittedDate", url)
        self.assertIn("sortOrder=descending", url)

    def test_abstract_only_markdown_does_not_claim_full_text_analysis(self) -> None:
        candidate = parse_arxiv_feed(ATOM_FEED)[0]
        analysis = AbstractAnalysis(
            one_sentence_summary="The abstract describes a memory mechanism.",
            problem="Agents need memory.",
            approach_from_abstract="The abstract proposes a structured approach.",
            reported_results="The abstract reports an evaluation.",
            limitations_and_unknowns=["Full-text limitations need verification."],
            next_reading_questions=["Which benchmark was used?"],
        )

        markdown = render_markdown(candidate, analysis)

        self.assertIn("analysis_level: \"abstract_only\"", markdown)
        self.assertIn("图表、公式与实验细节", markdown)
        self.assertIn("未分析", markdown)
        self.assertIn("https://arxiv.org/abs/2608.00001v1", markdown)


if __name__ == "__main__":
    unittest.main()
