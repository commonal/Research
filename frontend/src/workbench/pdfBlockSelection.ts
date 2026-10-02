export type PdfRunKind = "text" | "formula" | "table";
export type PdfTextRun = {
  text: string;
  x: number;
  y: number;
  width: number;
  height: number;
  page: number;
  kind: PdfRunKind;
};
export type PdfPoint = { x: number; y: number; page: number; pageWidth: number };
export type PdfSelectedBlock = PdfTextRun & { runCount: number };

function contains(run: PdfTextRun, point: PdfPoint): boolean {
  return point.x >= run.x && point.x <= run.x + run.width
    && point.y >= run.y && point.y <= run.y + run.height;
}

function sameColumn(run: PdfTextRun, target: PdfTextRun, pageWidth: number): boolean {
  if (target.width >= pageWidth * 0.6 || run.width >= pageWidth * 0.6) return true;
  const middle = pageWidth / 2;
  const targetCenter = target.x + target.width / 2;
  const runCenter = run.x + run.width / 2;
  return (targetCenter < middle) === (runCenter < middle);
}

function merge(runs: PdfTextRun[], kind: PdfRunKind): PdfSelectedBlock {
  const x = Math.min(...runs.map((run) => run.x));
  const y = Math.min(...runs.map((run) => run.y));
  const right = Math.max(...runs.map((run) => run.x + run.width));
  const bottom = Math.max(...runs.map((run) => run.y + run.height));
  return {
    text: runs.map((run) => run.text.trim()).filter(Boolean).join(" "),
    x,
    y,
    width: right - x,
    height: bottom - y,
    page: runs[0].page,
    kind,
    runCount: runs.length,
  };
}

export function selectPdfBlockAtPoint(runs: PdfTextRun[], point: PdfPoint): PdfSelectedBlock | null {
  const pageRuns = runs.filter((run) => run.page === point.page && run.text.trim());
  const target = pageRuns.find((run) => contains(run, point));
  if (!target) return null;
  if (target.kind !== "text") return merge([target], target.kind);

  const columnRuns = pageRuns
    .filter((run) => run.kind === "text" && sameColumn(run, target, point.pageWidth))
    .sort((left, right) => left.y - right.y || left.x - right.x);
  const targetIndex = columnRuns.indexOf(target);
  let start = targetIndex;
  let end = targetIndex;
  while (start > 0) {
    const previous = columnRuns[start - 1];
    const current = columnRuns[start];
    const gap = current.y - (previous.y + previous.height);
    if (gap > Math.max(previous.height, current.height) * 0.75) break;
    start -= 1;
  }
  while (end < columnRuns.length - 1) {
    const current = columnRuns[end];
    const next = columnRuns[end + 1];
    const gap = next.y - (current.y + current.height);
    if (gap > Math.max(current.height, next.height) * 0.75) break;
    end += 1;
  }
  return merge(columnRuns.slice(start, end + 1), "text");
}
