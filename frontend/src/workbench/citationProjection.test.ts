import { describe, expect, it } from "vitest";
import { projectDraftCitations } from "./citationProjection";

describe("projectDraftCitations", () => {
  const sentBlocks = [{ block_id: "B1", page: 1, label: "论文摘要", bbox: { x: 0.1, y: 0.2, width: 0.5, height: 0.1 } }];

  it("resolves only citations belonging to blocks sent in this turn", () => {
    const citations = projectDraftCitations("结论来自摘要 [B1]。", sentBlocks);
    expect(citations).toEqual([{ block_id: "B1", label: "[B1]", status: "resolved", page: 1, bbox: sentBlocks[0].bbox }]);
  });

  it("keeps unknown citations unresolved without a fake locator", () => {
    const citations = projectDraftCitations("另一个说法 [B999]。", sentBlocks);
    expect(citations).toEqual([{ block_id: "B999", label: "[B999]", status: "unresolved" }]);
    expect(citations[0]).not.toHaveProperty("page");
    expect(citations[0]).not.toHaveProperty("bbox");
  });
});
