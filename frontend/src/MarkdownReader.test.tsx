import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { MarkdownReader, normalizeReaderMarkdown } from "./MarkdownReader";

describe("MarkdownReader publication compatibility", () => {
  afterEach(() => cleanup());

  it("removes internal finding markers and projects simple legacy HTML tables to GFM", () => {
    const markdown = normalizeReaderMarkdown(
      "结论[^finding:f-1]。。（对应 Finding f-1、f-2）\n\n<table><tr><th>指标</th><th>得分</th></tr><tr><td>Recall</td><td>0.91</td></tr></table>",
    );

    expect(markdown).not.toContain("[^finding:");
    expect(markdown).not.toContain("对应 Finding");
    expect(markdown).toContain("结论。\n");
    expect(markdown).not.toContain("<table");
    expect(markdown).toContain("| 指标 | 得分 |");
    expect(markdown).toContain("| Recall | 0.91 |");
  });

  it("renders the normalized table as a real reader table", () => {
    render(
      <MarkdownReader
        markdown={"## 实验\n\n结论[^finding:f-1]。\n\n<table><tr><th>指标</th><th>得分</th></tr><tr><td>Recall</td><td>0.91</td></tr></table>"}
      />,
    );

    expect(screen.getByRole("table")).toBeTruthy();
    expect(screen.getByText("Recall")).toBeTruthy();
    expect(screen.queryByText("[^finding:f-1]")).toBeNull();
  });

  it("keeps complex span tables intact for the source-image fallback", () => {
    const markdown = normalizeReaderMarkdown(
      "<table><tr><th rowspan='2'>指标</th><th>得分</th></tr><tr><td>0.91</td></tr></table>",
    );
    expect(markdown).toContain("<table");
  });
});
