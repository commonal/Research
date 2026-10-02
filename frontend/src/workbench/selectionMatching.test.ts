import { describe, expect, it } from "vitest";
import { matchSelectionToBlock, matchSelectionToBlocks, normalizeBlockBbox, normalizeSelectionText } from "./selectionMatching";

describe("PDF selection block matching", () => {
  const blocks = [
    { block_id: "B-left", page: 1, text: "A memory retriever ranks candidates.", bbox: { x: 0.1, y: 0.2, width: 0.35, height: 0.1 } },
    { block_id: "B-right", page: 1, text: "A complementary signal improves recall.", bbox: { x: 0.55, y: 0.2, width: 0.35, height: 0.1 } },
  ];

  it("normalizes whitespace, line breaks, and ligatures", () => {
    expect(normalizeSelectionText("ofﬁce-\naware")).toBe("officeaware");
  });

  it("normalizes MinerU 1000x1000 bboxes before matching", () => {
    expect(normalizeBlockBbox({
      bbox: [519, 816, 923, 877],
      bbox_space: "normalized_1000",
      bbox_dimensions: [1000, 1000],
    }, { width: 612, height: 792 })).toEqual({
      x: 0.519,
      y: 0.816,
      width: 0.404,
      height: 0.061,
    });
  });

  it("matches text on the same page without merging two columns", () => {
    expect(matchSelectionToBlock({ text: "complementary signal", page: 1, bbox: { x: 0.56, y: 0.21, width: 0.2, height: 0.05 } }, blocks)?.block_id).toBe("B-right");
  });

  it("uses bbox as a fallback and returns unresolved when no block is credible", () => {
    expect(matchSelectionToBlock({ text: "unseen formula", page: 1, bbox: { x: 0.11, y: 0.21, width: 0.12, height: 0.04 } }, blocks)?.block_id).toBe("B-left");
    expect(matchSelectionToBlock({ text: "missing", page: 4 }, blocks)).toBeNull();
  });

  it("does not borrow a neighboring page or guess the only block", () => {
    expect(matchSelectionToBlock({ text: "A memory retriever ranks candidates.", page: 2 }, blocks)).toBeNull();
    expect(matchSelectionToBlock({ text: "unrelated text", page: 1 }, [blocks[0]])).toBeNull();
  });

  it("returns the contiguous blocks covered by a cross-block selection", () => {
    expect(matchSelectionToBlocks({ text: "ranks candidates. A complementary signal", page: 1 }, [
      { ...blocks[0], order: 10 },
      { ...blocks[1], order: 11 },
    ])?.map((block) => block.block_id)).toEqual(["B-left", "B-right"]);
  });

  it("does not attach discontiguous blocks for an ambiguous selection", () => {
    expect(matchSelectionToBlocks({ text: "candidates. recall", page: 1 }, [
      { ...blocks[0], order: 10 },
      { ...blocks[1], order: 11 },
    ])).toBeNull();
  });

  it("recovers by line geometry when extracted text is unavailable", () => {
    expect(matchSelectionToBlocks({ text: "ocr drift", page: 1, rects: [{ x: 0.11, y: 0.21, width: 0.16, height: 0.04 }] }, [
      { ...blocks[0], text: "different extraction", order: 10 },
      { ...blocks[1], text: "another paragraph", order: 11 },
    ])?.map((block) => block.block_id)).toEqual(["B-left"]);
  });

  it("does not recover when geometry has no credible overlap", () => {
    expect(matchSelectionToBlocks({ text: "ocr drift", page: 1, rects: [{ x: 0.02, y: 0.02, width: 0.04, height: 0.02 }] }, [
      { ...blocks[0], text: "different extraction", order: 10 },
      { ...blocks[1], text: "another paragraph", order: 11 },
    ])).toBeNull();
  });
});
