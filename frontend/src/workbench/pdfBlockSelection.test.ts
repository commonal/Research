import { describe, expect, it } from "vitest";
import { selectPdfBlockAtPoint, type PdfTextRun } from "./pdfBlockSelection";

describe("selectPdfBlockAtPoint", () => {
  it("returns the surrounding paragraph in a normal single-column page", () => {
    const runs: PdfTextRun[] = [
      { text: "Memory retrieval is stateful.", x: 10, y: 20, width: 180, height: 12, page: 1, kind: "text" },
      { text: "Feedback improves later ranking.", x: 10, y: 35, width: 190, height: 12, page: 1, kind: "text" },
      { text: "A new paragraph starts here.", x: 10, y: 75, width: 180, height: 12, page: 1, kind: "text" },
    ];
    expect(selectPdfBlockAtPoint(runs, { x: 40, y: 38, page: 1, pageWidth: 220 })?.text)
      .toBe("Memory retrieval is stateful. Feedback improves later ranking.");
  });

  it("does not merge the neighboring column", () => {
    const runs: PdfTextRun[] = [
      { text: "Left one", x: 10, y: 20, width: 80, height: 12, page: 1, kind: "text" },
      { text: "Right one", x: 120, y: 20, width: 80, height: 12, page: 1, kind: "text" },
      { text: "Left two", x: 10, y: 35, width: 80, height: 12, page: 1, kind: "text" },
      { text: "Right two", x: 120, y: 35, width: 80, height: 12, page: 1, kind: "text" },
    ];
    expect(selectPdfBlockAtPoint(runs, { x: 150, y: 24, page: 1, pageWidth: 220 })?.text)
      .toBe("Right one Right two");
  });

  it("keeps formulas and tables as bounded blocks", () => {
    const runs: PdfTextRun[] = [
      { text: "Method prose", x: 10, y: 20, width: 80, height: 12, page: 1, kind: "text" },
      { text: "L = q · k", x: 10, y: 40, width: 80, height: 16, page: 1, kind: "formula" },
      { text: "Table 1  Accuracy  0.91", x: 10, y: 70, width: 180, height: 20, page: 1, kind: "table" },
    ];
    expect(selectPdfBlockAtPoint(runs, { x: 30, y: 45, page: 1, pageWidth: 220 })?.kind).toBe("formula");
    expect(selectPdfBlockAtPoint(runs, { x: 30, y: 78, page: 1, pageWidth: 220 })?.text).toBe("Table 1  Accuracy  0.91");
  });
});
