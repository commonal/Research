import unittest

from research_pulse.pedagogical.artifact_quality import ArtifactQualityGate
from research_pulse.pedagogical.contracts import RenderedNote, RenderResult
from research_pulse.pedagogical.publication import PublicationManifest


class ArtifactQualityGateTests(unittest.TestCase):
    def test_allows_context_as_a_legitimate_technical_concept(self) -> None:
        note = RenderedNote(markdown=(
            "# 选择机制\n\n"
            "归纳头任务要求模型根据上下文中的模式进行推理，同样需要内容感知。"
        ))

        report = ArtifactQualityGate().evaluate(note)

        self.assertTrue(report.passed, report.issues)

    def test_rejects_explicit_provided_context_model_phrase(self) -> None:
        note = RenderedNote(markdown=(
            "# 阅读结果\n\n"
            "根据你提供的上下文，我将从以下几个方面总结这篇论文。"
        ))

        report = ArtifactQualityGate().evaluate(note)

        self.assertFalse(report.passed)
        self.assertIn("editorial_residue", {issue.code for issue in report.issues})

    def test_rejects_data_row_misclassified_as_table_header(self) -> None:
        note = RenderedNote(markdown="""# 实验结果

| w/o Adm. | 2.9 | 2.3 | 4.3 | 3.2 |
| --- | --- | --- | --- | --- |
| w/o Rep. | 3.1 | 2.8 | 4.7 | 3.6 |
""")

        report = ArtifactQualityGate().evaluate(note)

        self.assertFalse(report.passed)
        self.assertIn("suspicious_data_row_as_table_header", {issue.code for issue in report.issues})

    def test_rejects_pdf_hyphenation_left_in_final_prose(self) -> None:
        note = RenderedNote(markdown="# 方法\n\n该模块改善了 resolu- tion，并保持语义一致。")

        report = ArtifactQualityGate().evaluate(note)

        self.assertFalse(report.passed)
        self.assertIn("pdf_hyphenation", {issue.code for issue in report.issues})

    def test_rejects_unresolved_or_failed_assets(self) -> None:
        note = RenderedNote(
            markdown="# 方法\n\n见 {{asset:table-01}}。",
            render_results=(RenderResult(anchor_id="table-01", status="missing"),),
        )

        report = ArtifactQualityGate().evaluate(note)

        self.assertFalse(report.passed)
        self.assertEqual(
            {"unresolved_asset_anchor", "failed_asset_render"},
            {issue.code for issue in report.issues},
        )

    def test_rejects_obvious_unfinished_editorial_artifact(self) -> None:
        note = RenderedNote(markdown="""# 阅读笔记

## 核心方法
## 实验

TODO：补充实验设置。

```text
尚未闭合
""")

        report = ArtifactQualityGate().evaluate(note)

        self.assertFalse(report.passed)
        self.assertEqual(
            {"empty_section", "editorial_residue", "unclosed_fenced_block"},
            {issue.code for issue in report.issues},
        )

    def test_rejects_markdown_table_with_inconsistent_column_count(self) -> None:
        note = RenderedNote(markdown="""# 实验

| 方法 | Acc | F1 |
| --- | --- | --- |
| Baseline | 81.2 |
""")

        report = ArtifactQualityGate().evaluate(note)

        self.assertFalse(report.passed)
        self.assertIn("inconsistent_table_columns", {issue.code for issue in report.issues})

    def test_rejects_heading_level_jump(self) -> None:
        note = RenderedNote(markdown="# 阅读笔记\n\n开场说明。\n\n### 核心方法\n\n方法说明。")

        report = ArtifactQualityGate().evaluate(note)

        self.assertFalse(report.passed)
        self.assertIn("heading_level_jump", {issue.code for issue in report.issues})

    def test_rejects_exact_repeated_prose_paragraph(self) -> None:
        repeated = "该机制先筛选可靠记忆，再将经过验证的经验注入后续任务，从而减少错误传播。"
        note = RenderedNote(markdown=f"# 阅读笔记\n\n{repeated}\n\n## 实验\n\n实验说明。\n\n{repeated}")

        report = ArtifactQualityGate().evaluate(note)

        self.assertFalse(report.passed)
        issue = next(issue for issue in report.issues if issue.code == "duplicate_prose_paragraph")
        self.assertEqual(9, issue.line)

    def test_rejects_unclosed_display_math(self) -> None:
        note = RenderedNote(markdown="# 方法\n\n目标函数为：\n\n$$\nL(\\theta)=L_{task}+\\lambda R")

        report = ArtifactQualityGate().evaluate(note)

        self.assertFalse(report.passed)
        self.assertIn("unclosed_display_math", {issue.code for issue in report.issues})

    def test_rejects_image_link_to_local_machine_path(self) -> None:
        note = RenderedNote(markdown="# 架构\n\n![图 1](C:\\Users\\reader\\tmp\\figure.png)")

        report = ArtifactQualityGate().evaluate(note)

        self.assertFalse(report.passed)
        self.assertIn("local_image_path", {issue.code for issue in report.issues})

    def test_rejects_image_alt_repeated_as_visible_caption(self) -> None:
        caption = "Figure 1: Overview of the complete research pipeline and its verification stages."
        note = RenderedNote(markdown=f"# 架构\n\n![{caption}](assets/figure.png)\n\n*{caption}*")

        report = ArtifactQualityGate().evaluate(note)

        self.assertFalse(report.passed)
        self.assertIn("duplicate_visible_image_caption", {issue.code for issue in report.issues})

    def test_rejects_long_visible_english_source_caption_after_image(self) -> None:
        caption = "Figure 2: The architecture combines the verifier, memory controller, and retrieval policy across all stages."
        note = RenderedNote(markdown=f"# 架构\n\n![图 2](assets/figure.png)\n\n*{caption}*\n\n正文用中文解释该图。")

        report = ArtifactQualityGate().evaluate(note)

        self.assertFalse(report.passed)
        self.assertIn("visible_english_source_caption", {issue.code for issue in report.issues})

    def test_rejects_nested_image_markdown(self) -> None:
        note = RenderedNote(markdown="# 架构\n\n![示意](![图 1](assets/figure.png))")

        report = ArtifactQualityGate().evaluate(note)

        self.assertFalse(report.passed)
        self.assertIn("nested_image_markdown", {issue.code for issue in report.issues})

    def test_rejects_unmatched_latex_environment(self) -> None:
        note = RenderedNote(markdown="# 方法\n\n$$\\begin{aligned} a &= b \\ c &= d$$")

        report = ArtifactQualityGate().evaluate(note)

        self.assertFalse(report.passed)
        self.assertIn("unmatched_latex_environment", {issue.code for issue in report.issues})

    def test_rejects_artifact_ending_with_continuation_punctuation(self) -> None:
        note = RenderedNote(markdown="# 局限\n\n该方法仍存在以下限制：")

        report = ArtifactQualityGate().evaluate(note)

        self.assertFalse(report.passed)
        self.assertIn("abrupt_artifact_ending", {issue.code for issue in report.issues})

    def test_ignores_markdown_defects_inside_closed_code_example(self) -> None:
        note = RenderedNote(markdown="""# 示例

下面展示原始 Markdown 输入。

```markdown
### TODO
![图](C:\\tmp\\figure.png)
| A | B |
| --- | --- |
| 1 |
resolu- tion
```

示例到此结束。
""")

        report = ArtifactQualityGate().evaluate(note)

        self.assertTrue(report.passed, report.issues)

    def test_rejects_publication_manifest_with_missing_asset(self) -> None:
        note = RenderedNote(markdown="# 图示\n\n![图 1](assets/missing.png)")
        manifest = PublicationManifest(
            referenced_files=("missing.png",),
            missing_files=("missing.png",),
        )

        report = ArtifactQualityGate().evaluate(note, publication_manifest=manifest)

        self.assertFalse(report.passed)
        self.assertIn("missing_publication_asset", {issue.code for issue in report.issues})


if __name__ == "__main__":
    unittest.main()
