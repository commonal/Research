import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PDFDocumentProxy } from "pdfjs-dist";
import { derivePdfHighlightRects, PdfViewer, reconstructPdfSelectionText } from "./PdfViewer";
import type { PdfTextRun } from "./pdfBlockSelection";

describe("PdfViewer", () => {
  afterEach(() => cleanup());

  it("renders every page so the complete paper can be read by scrolling", async () => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({} as CanvasRenderingContext2D);
    const page = {
      getViewport: () => ({ width: 600, height: 800, scale: 1, convertToViewportPoint: (x: number, y: number) => [x, y] }),
      render: () => ({ promise: Promise.resolve() }),
      getTextContent: () => Promise.resolve({ items: [] }),
    };
    const document = {
      numPages: 3,
      getPage: vi.fn().mockResolvedValue(page),
      destroy: vi.fn().mockResolvedValue(undefined),
    } as unknown as PDFDocumentProxy;

    render(<PdfViewer url="blob:paper" loadDocument={() => Promise.resolve(document)} />);

    await waitFor(() => expect(document.getPage).toHaveBeenCalledTimes(3));
    expect(screen.getAllByLabelText(/^论文第 \d+ 页$/)).toHaveLength(3);
  });

  it("restores spaces between adjacent PDF text runs", () => {
    const layer = document.createElement("div");
    const first = document.createElement("span");
    const second = document.createElement("span");
    first.dataset.pdfRun = "";
    second.dataset.pdfRun = "";
    first.textContent = "remarkable";
    second.textContent = "performance";
    vi.spyOn(first, "getBoundingClientRect").mockReturnValue({ left: 10, top: 10, width: 80, height: 12 } as DOMRect);
    vi.spyOn(second, "getBoundingClientRect").mockReturnValue({ left: 96, top: 10, width: 80, height: 12 } as DOMRect);
    layer.append(first, second);
    document.body.append(layer);
    const range = document.createRange();
    range.setStart(first.firstChild!, 0);
    range.setEnd(second.firstChild!, second.textContent.length);

    expect(reconstructPdfSelectionText(range, layer)).toBe("remarkable performance");
    layer.remove();
  });

  it("maps normalized-1000 citation boxes to the rendered page", () => {
    expect(derivePdfHighlightRects(
      { x: 0.519, y: 0.816, width: 0.404, height: 0.061 },
      [],
      703,
      910,
      1,
    )).toEqual([{ x: 364.857, y: 742.56, width: 284.012, height: 55.51 }]);
  });

  it("tightens a coarse block box into separate text-line highlights", () => {
    const runs: PdfTextRun[] = [
      { text: "We are among", x: 382, y: 743, width: 120, height: 11, page: 1, kind: "text" },
      { text: "the first", x: 382, y: 757, width: 80, height: 11, page: 1, kind: "text" },
      { text: "unrelated footer", x: 56, y: 804, width: 90, height: 9, page: 1, kind: "text" },
    ];
    expect(derivePdfHighlightRects(
      { x: 0.519, y: 0.816, width: 0.404, height: 0.061 },
      runs,
      703,
      910,
      1,
    )).toEqual([
      { x: 382, y: 743, width: 120, height: 11 },
      { x: 382, y: 757, width: 80, height: 11 },
    ]);
  });
});
