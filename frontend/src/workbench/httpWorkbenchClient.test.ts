import { describe, expect, it } from "vitest";
import { inferCitationsFromText } from "./httpWorkbenchClient";

describe("historical citation hydration", () => {
  const paperId = "paper-1";
  const blocks = [
    {
      paper_id: paperId,
      block: {
        block_id: "normalized:paper-1:text:abc12345",
        text: "摘要内容",
        section_path: ["Abstract"],
        page: 1,
        bbox: [73, 258, 493, 575] as [number, number, number, number],
        bbox_format: "xyxy" as const,
        bbox_space: "page_points" as const,
      },
    },
  ];

  it("hydrates a normalized marker when the API omitted structured citations", () => {
    const result = inferCitationsFromText(
      "结论来自（`normalized:paper-1:text:abc12345`）。",
      [],
      blocks,
    );

    expect(result).toEqual([
      {
        block_id: "normalized:paper-1:text:abc12345",
        paper_id: paperId,
        label: "论文证据 · 第 1 页",
        status: "resolved",
        page: 1,
        bbox: { x: 73, y: 258, width: 420, height: 317 },
      },
    ]);
  });

  it("keeps the marker order and locator for multiple normalized blocks", () => {
    const result = inferCitationsFromText(
      "一 [normalized:paper-1:text:abc12345]，二 [normalized:paper-1:text:def67890]。",
      [],
      [
        ...blocks,
        {
          paper_id: paperId,
          block: {
            block_id: "normalized:paper-1:text:def67890",
            text: "正文内容",
            section_path: ["Introduction"],
            page: 2,
            bbox: [91, 93, 491, 169] as [number, number, number, number],
            bbox_format: "xyxy" as const,
            bbox_space: "page_points" as const,
          },
        },
      ],
    );
    expect(result.map((citation) => [citation.block_id, citation.page, citation.bbox])).toEqual([
      ["normalized:paper-1:text:abc12345", 1, { x: 73, y: 258, width: 420, height: 317 }],
      ["normalized:paper-1:text:def67890", 2, { x: 91, y: 93, width: 400, height: 76 }],
    ]);
  });

  it("does not manufacture a locator for an unknown marker", () => {
    const result = inferCitationsFromText("未知证据 [normalized:paper-1:text:missing]。", [], blocks);
    expect(result).toEqual([]);
  });

  it("preserves server citations and appends only missing hydrated blocks", () => {
    const existing = [{
      block_id: "server-block",
      paper_id: paperId,
      label: "论文证据 · 第 2 页",
      status: "resolved" as const,
      page: 2,
    }];
    const result = inferCitationsFromText(
      "已有 [server-block]，另见 [text:abc12345]。",
      existing,
      blocks,
    );
    expect(result.map((citation) => citation.block_id)).toEqual(["server-block", "normalized:paper-1:text:abc12345"]);
  });

  it("maps MinerU normalized-1000 coordinates into page-relative coordinates", () => {
    const result = inferCitationsFromText(
      "右栏证据 [normalized:paper-1:text:right-column]。",
      [],
      [{
        paper_id: "paper-1",
        block: {
          block_id: "normalized:paper-1:text:right-column",
          text: "右栏证据",
          section_path: ["Introduction"],
          page: 1,
          bbox: [519, 816, 923, 877],
          bbox_format: "xyxy",
          bbox_space: "normalized_1000",
        } as any,
      }],
    );

    expect(result[0]?.bbox).toEqual({ x: 0.519, y: 0.816, width: 0.404, height: 0.061 });
  });
});
