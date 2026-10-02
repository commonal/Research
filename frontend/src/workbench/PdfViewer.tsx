import { useEffect, useRef, useState } from "react";
import type { PDFDocumentProxy, PDFPageProxy } from "pdfjs-dist";
import { selectPdfBlockAtPoint, type PdfSelectedBlock, type PdfTextRun } from "./pdfBlockSelection";

type Highlight = { x: number; y: number; width: number; height: number };
type PageSize = { width: number; height: number };
type SelectionPayload = { text: string; page: number; bbox?: Highlight; rects?: Highlight[]; pageSize?: PageSize };
type SelectionAction = "translate" | "explain";
type Props = { url: string; onSelection?: (selection: SelectionPayload) => void; onSelectionAction?: (action: SelectionAction, selection: SelectionPayload) => void; onTextSelected?: (selection: SelectionPayload) => void; highlight?: Highlight | null; highlightPage?: number; focusPage?: number; loadDocument?: (url: string) => Promise<PDFDocumentProxy> };

type SelectionFragment = { text: string; left: number; top: number; width: number; height: number };

/** Convert either a page-relative box or a canvas-pixel box to canvas pixels. */
export function highlightToCanvasPixels(value: Highlight, canvasWidth: number, canvasHeight: number): Highlight {
  const toPixels = (coordinate: number, size: number) => coordinate >= 0 && coordinate <= 1 ? coordinate * size : coordinate;
  return {
    x: toPixels(value.x, canvasWidth),
    y: toPixels(value.y, canvasHeight),
    width: toPixels(value.width, canvasWidth),
    height: toPixels(value.height, canvasHeight),
  };
}

type HighlightLine = Highlight & { centerY: number };

/**
 * Turn a coarse parser block box into per-line highlight rectangles using the
 * PDF.js text layer.  The parser box still bounds the result, while the text
 * runs provide the actual rendered geometry and remove empty whitespace.
 */
export function derivePdfHighlightRects(
  highlight: Highlight,
  runs: PdfTextRun[],
  canvasWidth: number,
  canvasHeight: number,
  pageNumber: number,
): Highlight[] {
  const base = highlightToCanvasPixels(highlight, canvasWidth, canvasHeight);
  if (!canvasWidth || !canvasHeight || !base.width || !base.height) return [base];
  const candidates = runs
    .filter((run) => run.page === pageNumber && run.width > 0 && run.height > 0)
    .filter((run) => {
      const overlapX = Math.min(base.x + base.width, run.x + run.width) - Math.max(base.x, run.x);
      const overlapY = Math.min(base.y + base.height, run.y + run.height) - Math.max(base.y, run.y);
      return overlapX >= Math.min(2, run.width * 0.2) && overlapY >= Math.min(2, run.height * 0.2);
    })
    .sort((left, right) => left.y - right.y || left.x - right.x);
  if (!candidates.length) return [base];

  const lines: HighlightLine[] = [];
  for (const run of candidates) {
    const centerY = run.y + run.height / 2;
    const tolerance = Math.max(2, run.height * 0.55);
    const line = lines.find((candidate) => Math.abs(candidate.centerY - centerY) <= tolerance);
    if (!line) {
      lines.push({ x: run.x, y: run.y, width: run.width, height: run.height, centerY });
      continue;
    }
    const right = Math.max(line.x + line.width, run.x + run.width);
    const bottom = Math.max(line.y + line.height, run.y + run.height);
    line.x = Math.min(line.x, run.x);
    line.y = Math.min(line.y, run.y);
    line.width = right - line.x;
    line.height = bottom - line.y;
    line.centerY = line.y + line.height / 2;
  }
  return lines.map(({ centerY: _centerY, ...line }) => {
    const x = Math.max(base.x, line.x);
    const y = Math.max(base.y, line.y);
    const right = Math.min(base.x + base.width, line.x + line.width);
    const bottom = Math.min(base.y + base.height, line.y + line.height);
    return { x, y, width: Math.max(0, right - x), height: Math.max(0, bottom - y) };
  }).filter((line) => line.width > 0 && line.height > 0);
}

const CJK_CHARACTER = /[\u3400-\u9fff\u3040-\u30ff\uff00-\uffef]$/;
const OPENING_PUNCTUATION = /^[([{\u3008\u300a\u300c\u300e\u3010]/;
const CLOSING_PUNCTUATION = /^[,.;:!?%)\\]}\u3001\u3002\uff0c\uff01\uff1f\uff1a\uff1b]/;

/**
 * PDF text layers position each text item independently, so the browser's
 * Selection#toString() often concatenates adjacent words without a space.
 * Rebuild the selection from the intersected text nodes and their geometry,
 * keeping the user's exact first/last fragments while restoring visual word
 * boundaries. This is deliberately local to the selected runs; it does not
 * fuzzy-match or expand the requested evidence.
 */
export function reconstructPdfSelectionText(range: Range, textLayer: HTMLElement, fallback = ""): string {
  const fragments: SelectionFragment[] = [];
  for (const element of Array.from(textLayer.querySelectorAll<HTMLElement>("[data-pdf-run]"))) {
    let intersects = false;
    try { intersects = range.intersectsNode(element); } catch { intersects = false; }
    if (!intersects) continue;
    const node = element.firstChild;
    const raw = node?.textContent ?? element.textContent ?? "";
    let start = 0;
    let end = raw.length;
    if (node && range.startContainer === node) start = Math.max(0, Math.min(raw.length, range.startOffset));
    if (node && range.endContainer === node) end = Math.max(0, Math.min(raw.length, range.endOffset));
    const text = raw.slice(Math.min(start, end), Math.max(start, end));
    const rect = element.getBoundingClientRect();
    if (text) fragments.push({ text, left: rect.left, top: rect.top, width: rect.width, height: rect.height });
  }
  if (!fragments.length) return fallback.replace(/\s+/g, " ").trim();
  fragments.sort((left, right) => {
    const lineTolerance = Math.max(2, Math.min(left.height, right.height) * 0.45);
    return Math.abs(left.top - right.top) <= lineTolerance ? left.left - right.left : left.top - right.top;
  });
  let result = "";
  let previous: SelectionFragment | null = null;
  for (const fragment of fragments) {
    if (previous && shouldSeparatePdfSelectionFragments(previous, fragment)) result += " ";
    result += fragment.text;
    previous = fragment;
  }
  return result.replace(/\s+/g, " ").trim() || fallback.replace(/\s+/g, " ").trim();
}

function shouldSeparatePdfSelectionFragments(previous: SelectionFragment, current: SelectionFragment): boolean {
  const previousEnd = previous.text.slice(-1);
  const currentStart = current.text.slice(0, 1);
  const lineBreak = Math.abs(previous.top - current.top) > Math.max(2, Math.min(previous.height, current.height) * 0.45);
  if (previousEnd === "-" && lineBreak) return false;
  if (CJK_CHARACTER.test(previousEnd) || CJK_CHARACTER.test(currentStart)) return false;
  if (CLOSING_PUNCTUATION.test(currentStart) || OPENING_PUNCTUATION.test(previousEnd)) return false;
  if (lineBreak) return true;
  const gap = current.left - (previous.left + previous.width);
  return gap > Math.max(1.5, Math.min(previous.height, current.height) * 0.12);
}

async function loadPdfDocument(url: string): Promise<PDFDocumentProxy> {
  const { getDocument, GlobalWorkerOptions } = await import("pdfjs-dist");
  GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/build/pdf.worker.mjs", import.meta.url).toString();
  return getDocument(url).promise;
}

type PageProps = {
  document: PDFDocumentProxy;
  pageNumber: number;
  highlight?: Highlight | null;
  onBlockSelected: (block: PdfSelectedBlock) => void;
  onTextSelected?: (selection: SelectionPayload) => void;
  onSelectionPayload: (selection: SelectionPayload) => void;
  selectionPayload?: SelectionPayload | null;
  selectedBlock?: PdfSelectedBlock | null;
  onSelection?: (selection: SelectionPayload) => void;
  onSelectionAction?: (action: SelectionAction, selection: SelectionPayload) => void;
  zoom: number;
};

function PdfPage({ document, pageNumber, highlight, onBlockSelected, onTextSelected, onSelectionPayload, selectionPayload, selectedBlock, onSelection, onSelectionAction, zoom }: PageProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textLayerRef = useRef<HTMLDivElement>(null);
  const [page, setPage] = useState<PDFPageProxy | null>(null);
  const [textRuns, setTextRuns] = useState<PdfTextRun[]>([]);
  const [displayScale, setDisplayScale] = useState(1);
  const [pageSize, setPageSize] = useState<PageSize | null>(null);

  useEffect(() => {
    let cancelled = false;
    void document.getPage(pageNumber).then((loadedPage) => { if (!cancelled) setPage(loadedPage); });
    return () => { cancelled = true; };
  }, [document, pageNumber]);

  useEffect(() => {
    if (!page || !canvasRef.current) return;
    let cancelled = false;
    const canvas = canvasRef.current;
    const viewport = page.getViewport({ scale: 1.15 * zoom });
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    setPageSize({ width: viewport.width / viewport.scale, height: viewport.height / viewport.scale });
    const context = canvas.getContext("2d");
    if (!context) return;
    void Promise.all([page.render({ canvas, canvasContext: context, viewport }).promise, page.getTextContent()]).then(([, textContent]) => {
      if (cancelled) return;
      setTextRuns(textContent.items.flatMap((item) => {
        if (!("str" in item) || !item.str.trim()) return [];
        const [, , , , x, y] = item.transform;
        const point = viewport.convertToViewportPoint(x, y);
        const rawHeight = "height" in item && typeof item.height === "number" ? item.height : 10;
        const rawWidth = "width" in item && typeof item.width === "number" ? item.width : 10;
        const height = Math.max(7, Math.abs(rawHeight) * viewport.scale);
        return [{ text: item.str, x: point[0], y: point[1] - height, width: Math.max(1, rawWidth * viewport.scale), height, page: pageNumber, kind: "text" as const }];
      }));
    });
    return () => { cancelled = true; };
  }, [page, pageNumber, zoom]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const updateScale = () => setDisplayScale(canvas.getBoundingClientRect().width / canvas.width || 1);
    updateScale();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(updateScale);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [page]);

  function captureSelection() {
    const nativeSelection = window.getSelection();
    const range = nativeSelection?.rangeCount ? nativeSelection.getRangeAt(0) : null;
    const selection = range && textLayerRef.current
      ? reconstructPdfSelectionText(range, textLayerRef.current, nativeSelection?.toString() ?? "")
      : nativeSelection?.toString().replace(/\s+/g, " ").trim();
    if (selection && nativeSelection) {
      const pageRect = canvasRef.current?.getBoundingClientRect();
      const canvas = canvasRef.current;
      if (!range || !pageRect || !canvas) return;
      const clientRects = Array.from(range.getClientRects()).filter((rect) => rect.width > 0 && rect.height > 0);
      const normalizedRects = clientRects.map((rect) => ({
        x: Math.max(0, (rect.left - pageRect.left) / displayScale) / canvas.width,
        y: Math.max(0, (rect.top - pageRect.top) / displayScale) / canvas.height,
        width: rect.width / displayScale / canvas.width,
        height: rect.height / displayScale / canvas.height,
      }));
      const rect = range.getBoundingClientRect();
      const block = { text: selection, x: Math.max(0, rect.left - pageRect.left) / displayScale, y: Math.max(0, rect.top - pageRect.top) / displayScale, width: rect.width / displayScale, height: rect.height / displayScale, page: pageNumber, kind: "text" as const, runCount: clientRects.length || 1 };
      const payload: SelectionPayload = { text: selection, page: pageNumber, bbox: { x: block.x / canvas.width, y: block.y / canvas.height, width: block.width / canvas.width, height: block.height / canvas.height }, rects: normalizedRects, pageSize: pageSize ?? { width: canvas.width / (1.15 * zoom), height: canvas.height / (1.15 * zoom) } };
      onTextSelected?.(payload);
      onBlockSelected(block);
      onSelectionPayload(payload);
    }
  }

  function selectRun(run: PdfTextRun) {
    const selected = selectPdfBlockAtPoint(textRuns, { x: run.x + run.width / 2, y: run.y + run.height / 2, page: pageNumber, pageWidth: canvasRef.current?.width ?? 1 });
    if (selected) {
      const payload = { text: selected.text, page: pageNumber, bbox: canvasRef.current ? { x: selected.x / canvasRef.current.width, y: selected.y / canvasRef.current.height, width: selected.width / canvasRef.current.width, height: selected.height / canvasRef.current.height } : undefined, pageSize: pageSize ?? undefined };
      onTextSelected?.(payload);
      onBlockSelected(selected);
      onSelectionPayload(payload);
    }
  }

  function currentSelectionPayload(): SelectionPayload | null {
    if (!selectedBlock || !canvasRef.current) return null;
    // The parent retains the exact browser selection, including all line rects.
    // The fallback is only used for a programmatic single-run click.
    if (selectionPayload?.page === pageNumber && selectionPayload.text === selectedBlock.text) return selectionPayload;
    return {
      text: selectedBlock.text,
      page: pageNumber,
      bbox: {
        x: selectedBlock.x / canvasRef.current.width,
        y: selectedBlock.y / canvasRef.current.height,
        width: selectedBlock.width / canvasRef.current.width,
        height: selectedBlock.height / canvasRef.current.height,
      },
    };
  }

  return (
    <div className="workbench-pdf-page" aria-label={`论文第 ${pageNumber} 页`} onMouseUp={captureSelection}>
      <canvas ref={canvasRef} aria-label={`论文第 ${pageNumber} 页画布`} />
      {selectedBlock?.page === pageNumber && <div className="workbench-selection-action" style={{ left: `${(selectedBlock.x + selectedBlock.width / 2) * displayScale}px`, top: `${Math.max(8, selectedBlock.y * displayScale - 46)}px` }}>
        <div className="workbench-selection-actions">
          <button type="button" className="secondary" onClick={() => { const selection = currentSelectionPayload(); if (selection) onSelectionAction?.("translate", selection); }}>翻译</button>
          <button type="button" className="secondary" onClick={() => { const selection = currentSelectionPayload(); if (selection) onSelectionAction?.("explain", selection); }}>解释</button>
          <button type="button" onClick={() => { const selection = currentSelectionPayload(); if (selection) onSelection?.(selection); }}>提问</button>
        </div>
      </div>}
      {highlight && (canvasRef.current ? derivePdfHighlightRects(highlight, textRuns, canvasRef.current.width, canvasRef.current.height, pageNumber) : [highlight]).map((rect, index) => {
        const canvas = canvasRef.current;
        if (!canvas) return null;
        return <i key={`${rect.x}-${rect.y}-${index}`} className="workbench-pdf-highlight" aria-label="引用定位高亮" style={{ left: `${rect.x / canvas.width * 100}%`, top: `${rect.y / canvas.height * 100}%`, width: `${rect.width / canvas.width * 100}%`, height: `${rect.height / canvas.height * 100}%` }} />;
      })}
      <div ref={textLayerRef} className="workbench-pdf-text-layer" aria-label={`论文第 ${pageNumber} 页文本层`}>
        {textRuns.map((run, index) => <span data-pdf-run key={`${index}-${run.x}-${run.y}`} onClick={(event) => { event.stopPropagation(); selectRun(run); }} style={{ left: run.x * displayScale, top: run.y * displayScale, width: run.width * displayScale, height: run.height * displayScale, fontSize: run.height * displayScale }}>{run.text}</span>)}
      </div>
    </div>
  );
}

export function PdfViewer({ url, onSelection, onSelectionAction, onTextSelected, highlight, highlightPage = 1, focusPage, loadDocument = loadPdfDocument }: Props) {
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null);
  const viewerRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "failed">("loading");
  const [error, setError] = useState<string | null>(null);
  const [selectedBlock, setSelectedBlock] = useState<PdfSelectedBlock | null>(null);
  const [selectionPayload, setSelectionPayload] = useState<SelectionPayload | null>(null);
  const [zoom, setZoom] = useState(1);

  useEffect(() => {
    let cancelled = false;
    let loadedDocument: PDFDocumentProxy | null = null;
    setStatus("loading");
    setError(null);
    setDocument(null);
    setZoom(1);
    setSelectedBlock(null);
    setSelectionPayload(null);
    void loadDocument(url).then((loaded) => {
      loadedDocument = loaded;
      if (!cancelled) { setDocument(loaded); setStatus("ready"); }
    }).catch((reason: unknown) => {
      if (!cancelled) { setStatus("failed"); setError(reason instanceof Error ? reason.message : "PDF 加载失败"); }
    });
    return () => { cancelled = true; void loadedDocument?.destroy(); };
  }, [loadDocument, url]);

  useEffect(() => {
    if (!document || !focusPage) return;
    viewerRef.current?.querySelector<HTMLElement>(`[aria-label="论文第 ${focusPage} 页"]`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [document, focusPage]);

  return (
    <div ref={viewerRef} className="workbench-pdf-viewer" aria-label="PDF 预览">
      {document && <div className="workbench-pdf-controls" aria-label="论文缩放控制">
        <button type="button" onClick={() => setZoom((value) => Math.max(.75, Number((value - .25).toFixed(2))))} aria-label="缩小论文">−</button>
        <span>{Math.round(zoom * 100)}%</span>
        <button type="button" onClick={() => setZoom((value) => Math.min(2.5, Number((value + .25).toFixed(2))))} aria-label="放大论文">＋</button>
        <button type="button" onClick={() => setZoom(1)} aria-label="重置论文缩放">重置</button>
      </div>}
      {status === "loading" && <p role="status">正在加载 PDF…</p>}
      {status === "failed" && <p role="alert">PDF 暂不可读：{error ?? "未知错误"}</p>}
      {document && Array.from({ length: document.numPages }, (_, index) => index + 1).map((pageNumber) => <PdfPage key={pageNumber} document={document} pageNumber={pageNumber} zoom={zoom} highlight={pageNumber === highlightPage ? highlight : null} onBlockSelected={setSelectedBlock} onTextSelected={onTextSelected} onSelectionPayload={setSelectionPayload} selectionPayload={selectionPayload} selectedBlock={selectedBlock} onSelection={onSelection} onSelectionAction={onSelectionAction} />)}
    </div>
  );
}
