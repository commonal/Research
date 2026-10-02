import { describe, expect, it } from "vitest";
import { organizeExplorationDraft, stripCitationAuditAppendix, tokenizeCitations } from "./WorkbenchApp";

describe("tokenizeCitations", () => {
  it("removes the internal citation mapping appendix before rendering a draft", () => {
    const draft = "## 结论\n\nβ 需要做敏感性分析。[text:abc123]\n\n<details><summary>引用映射（本轮实读块，完整 stable id）</summary>\nP-beta = normalized:paper:text:abc123\n</details>";
    expect(stripCitationAuditAppendix(draft)).toBe("## 结论\n\nβ 需要做敏感性分析。[text:abc123]");
  });

  it("removes a plain-text citation mapping appendix emitted without HTML", () => {
    const draft = "结论正文。\n\n### 引用映射\nP-beta = normalized:paper:text:abc123";
    expect(stripCitationAuditAppendix(draft)).toBe("结论正文。");
  });

  it("renders adjacent citations as separate markdown links (no broken code span), deduped to a circled index", () => {
    const { markdown, refs } = tokenizeCitations("方法见 [text:aabbcc][text:112233] 之后。");
    expect(markdown).toContain("[1](#cite-1)");
    expect(markdown).toContain("[2](#cite-2)");
    expect(markdown).not.toContain("<cite:");
    expect(markdown).not.toContain("`");
    expect(refs).toEqual([
      { blockId: "text:aabbcc", label: "1" },
      { blockId: "text:112233", label: "2" },
    ]);
  });

  it("dedupes repeated citations to one stable circled index", () => {
    const { markdown, refs } = tokenizeCitations("[text:ab]\u8bf4 [text:ab] \u518d [text:cd]");
    expect(markdown).toBe("[1](#cite-1)\u8bf4 [1](#cite-1) \u518d [2](#cite-2)");
    expect(refs).toEqual([
      { blockId: "text:ab", label: "1" },
      { blockId: "text:cd", label: "2" },
    ]);
  });

  it("handles figure refs and leaves plain bracketed bibliography [18] untouched", () => {
    const { markdown, refs } = tokenizeCitations("如 Fig. [figure:9f8e] \u6240\u793a\uff0c[18] \u5f15\u7528\u4e0d\u53d8\u3002");
    expect(markdown).toBe("如 Fig. [1](#cite-1) \u6240\u793a\uff0c[18] \u5f15\u7528\u4e0d\u53d8\u3002");
    expect(refs).toEqual([{ blockId: "figure:9f8e", label: "1" }]);
  });

  it("tokenizes the assistant's full normalized evidence-block ids", () => {
    const paper = "9c7c1e0996785f0afac3a28eab5f18e96d2ae2118a1be7d3302b29ffce32f36b";
    const { markdown, refs } = tokenizeCitations(
      `结论见 [normalized:${paper}:text:0e4a47978205] 与 [normalized:${paper}:text:2d772d86a16b]。`
    );
    expect(markdown).toBe(`结论见 [1](#cite-1) 与 [2](#cite-2)。`);
    expect(refs).toEqual([
      { blockId: `normalized:${paper}:text:0e4a47978205`, label: "1" },
      { blockId: `normalized:${paper}:text:2d772d86a16b`, label: "2" },
    ]);
  });

  it("accepts full-width brackets used by Chinese model output", () => {
    const paper = "9c7c1e0996785f0afac3a28eab5f18e96d2ae2118a1be7d3302b29ffce32f36b";
    const { markdown, refs } = tokenizeCitations(
      `论文要解决的问题是：推荐目标存在鸿沟［normalized:${paper}:text:00144a7dd164］。`
    );
    expect(markdown).toBe("论文要解决的问题是：推荐目标存在鸿沟[1](#cite-1)。");
    expect(refs).toEqual([
      { blockId: `normalized:${paper}:text:00144a7dd164`, label: "1" },
    ]);
  });

  it("accepts parenthesized and backtick-wrapped managed ids emitted by models", () => {
    const paper = "9c7c1e0996785f0afac3a28eab5f18e96d2ae2118a1be7d3302b29ffce32f36b";
    const { markdown, refs } = tokenizeCitations(
      `方法依据（\`normalized:${paper}:text:00144a7dd164\`、normalized:${paper}:text:00255b8ee275）。`
    );
    expect(markdown).toBe("方法依据[1](#cite-1)[2](#cite-2)。");
    expect(refs).toEqual([
      { blockId: `normalized:${paper}:text:00144a7dd164`, label: "1" },
      { blockId: `normalized:${paper}:text:00255b8ee275`, label: "2" },
    ]);
  });

  it("renders normalized formula and table blocks as jumpable markers", () => {
    const paper = "9c7c1e0996785f0afac3a28eab5f18e96d2ae2118a1be7d3302b29ffce32f36b";
    const { markdown, refs } = tokenizeCitations(
      `公式［normalized:${paper}:formula:608060b891ca］和表 [normalized:${paper}:table:abcdef123456]。`
    );
    expect(markdown).toBe("公式[1](#cite-1)和表 [2](#cite-2)。");
    expect(refs).toEqual([
      { blockId: `normalized:${paper}:formula:608060b891ca`, label: "1" },
      { blockId: `normalized:${paper}:table:abcdef123456`, label: "2" },
    ]);
  });

  it("splits ids merged into one bracket pair into separate numbered markers", () => {
    const paper = "9c7c1e0996785f0afac3a28eab5f18e96d2ae2118a1be7d3302b29ffce32f36b";
    const { markdown, refs } = tokenizeCitations(
      `同属 [normalized:${paper}:text:eb1889e79804, text:112233] 两个块。`
    );
    expect(markdown).toBe(`同属 [1](#cite-1)[2](#cite-2) 两个块。`);
    expect(refs).toEqual([
      { blockId: `normalized:${paper}:text:eb1889e79804`, label: "1" },
      { blockId: "text:112233", label: "2" },
    ]);
  });

  it("promotes bare abbreviated hex ids to numbered markers (still resolvable via block lookup)", () => {
    const { markdown, refs } = tokenizeCitations("这一大方向 [eb1889e79804, d88e3fb550c6]。");
    expect(markdown).toBe("这一大方向 [1](#cite-1)[2](#cite-2)。");
    expect(refs).toEqual([
      { blockId: "eb1889e79804", label: "1" },
      { blockId: "d88e3fb550c6", label: "2" },
    ]);
  });

  it("leaves short or non-hex bracket text untouched (paper bib numbers, prose)", () => {
    const { markdown, refs } = tokenizeCitations("见 [16] 与 [2023] 与 [et al.]，证据见 [论文证据]。");
    expect(markdown).toBe("见 [16] 与 [2023] 与 [et al.]，证据见 [论文证据]。");
    expect(refs).toEqual([]);
  });
});

describe("organizeExplorationDraft", () => {
  it("projects a long research draft into user-facing result groups", () => {
    const result = organizeExplorationDraft([
      "## 结论先说",
      "\n当前材料不能回答 beta 敏感性。",
      "## 论文已给边界",
      "\n论文只报告默认值 0.01。",
      "## 机制外推",
      "\n这是一个待验证的机制判断。",
      "## 最小可执行验证方案",
      "\n做 beta sweep 并监控 KL。",
      "## 待精读候选",
      "\narXiv:2406.02900",
    ].join("\n"));
    expect(result.structured).toBe(true);
    expect(result.conclusion.map((item) => item.heading)).toEqual(["结论先说"]);
    expect(result.evidence.map((item) => item.heading)).toEqual(["论文已给边界"]);
    expect(result.uncertainty.map((item) => item.heading)).toEqual(["机制外推"]);
    expect(result.next.map((item) => item.heading)).toEqual(["最小可执行验证方案"]);
    expect(result.internal.map((item) => item.heading)).toEqual(["待精读候选"]);
  });

  it("keeps a short unstructured fixture as a normal answer", () => {
    const result = organizeExplorationDraft("已发现一条待验证的方法路线。");
    expect(result.structured).toBe(false);
    expect(result.conclusion).toEqual([]);
  });
});
