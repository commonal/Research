export type SelectionBox = { x: number; y: number; width: number; height: number };
export type SelectionBlock = { block_id: string; text: string; page: number | null; order?: number; bbox?: SelectionBox; section_path?: string[] };
export type SelectionInput = { text: string; page: number; bbox?: SelectionBox; rects?: SelectionBox[] };
export type ApiSelectionBlockGeometry = {
  bbox?: [number, number, number, number] | null;
  bbox_space?: "page_points" | "normalized_1000";
  bbox_dimensions?: [number, number] | null;
};

/**
 * Convert a parser-owned block bbox into the same page-relative coordinate
 * space used by the browser selection geometry.  The parser coordinate space
 * must be explicit; only the legacy page-points path falls back to the PDF
 * page dimensions supplied by PDF.js.
 */
export function normalizeBlockBbox(
  block: ApiSelectionBlockGeometry,
  pageSize?: { width: number; height: number },
): SelectionBox | undefined {
  const raw = block.bbox;
  if (!raw || raw.length < 4) return undefined;
  const dimensions = block.bbox_dimensions
    ?? (block.bbox_space === "normalized_1000" ? [1000, 1000] as [number, number] : undefined)
    ?? (pageSize ? [pageSize.width, pageSize.height] as [number, number] : undefined);
  if (!dimensions || dimensions[0] <= 0 || dimensions[1] <= 0) return undefined;
  const [x0, y0, x1, y1] = raw;
  return {
    x: Math.min(x0, x1) / dimensions[0],
    y: Math.min(y0, y1) / dimensions[1],
    width: Math.abs(x1 - x0) / dimensions[0],
    height: Math.abs(y1 - y0) / dimensions[1],
  };
}

const ligatures: Record<string, string> = { "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl" };

export function normalizeSelectionText(value: string): string {
  return value
    .replace(/[ﬀﬁﬂﬃﬄ]/g, (char) => ligatures[char] ?? char)
    // Browser selections collapse line breaks to spaces before matching; also
    // accept the resulting "word- word" form from a PDF line wrap.
    .replace(/-\s+/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLocaleLowerCase();
}

function overlap(left: SelectionBox, right: SelectionBox): number {
  const x = Math.max(0, Math.min(left.x + left.width, right.x + right.width) - Math.max(left.x, right.x));
  const y = Math.max(0, Math.min(left.y + left.height, right.y + right.height) - Math.max(left.y, right.y));
  return x * y;
}

export function matchSelectionToBlock(selection: SelectionInput, blocks: SelectionBlock[]): SelectionBlock | null {
  const matched = matchSelectionToBlocks(selection, blocks);
  if (matched?.length === 1) return matched[0];

  // Keep the old geometry-only fallback for callers that have already narrowed
  // the candidate set to one page. Raw API bboxes are converted by the caller
  // before reaching this path; an unconverted bbox must never be compared here.
  if (matched?.length) return null;
  const query = normalizeSelectionText(selection.text);
  if (!query) return null;
  const samePage = blocks.filter((block) => block.page === selection.page);
  const textual = samePage
    .map((block) => ({ block, exact: normalizeSelectionText(block.text).includes(query) }))
    .filter((candidate) => candidate.exact)
    .map((candidate) => candidate.block);
  if (textual.length) {
    if (textual.length === 1 || !selection.bbox) return textual.length === 1 ? textual[0] : null;
    const ranked = textual
      .map((block) => ({ block, score: block.bbox ? overlap(selection.bbox!, block.bbox) : 0 }))
      .sort((left, right) => right.score - left.score);
    return ranked[0]?.score > 0 ? ranked[0].block : null;
  }
  if (!selection.bbox) return null;
  return samePage
    .filter((block): block is SelectionBlock & { bbox: SelectionBox } => Boolean(block.bbox))
    .map((block) => ({ block, score: overlap(selection.bbox!, block.bbox) }))
    .filter((candidate) => candidate.score >= selection.bbox!.width * selection.bbox!.height * 0.5)
    .sort((left, right) => right.score - left.score)[0]?.block ?? null;
}

/**
 * Resolve a browser selection to the smallest contiguous sequence of managed
 * blocks containing the exact selected text. A selection can cross PDF text
 * runs and therefore legitimately map to more than one evidence block.
 *
 * Fuzzy token overlap is intentionally not used: silently attaching a nearby
 * paragraph is worse than asking the user to shorten an ambiguous selection.
 */
export function matchSelectionToBlocks(selection: SelectionInput, blocks: SelectionBlock[]): SelectionBlock[] | null {
  const query = normalizeSelectionText(selection.text);
  if (!query) return null;
  const ordered = blocks
    .map((block, index) => ({ block, index }))
    .filter(({ block }) => block.page === selection.page)
    .sort((left, right) => (left.block.order ?? left.index) - (right.block.order ?? right.index));
  const candidates: SelectionBlock[][] = [];
  for (let start = 0; start < ordered.length; start += 1) {
    let joined = "";
    for (let end = start; end < ordered.length; end += 1) {
      joined = joined ? `${joined} ${normalizeSelectionText(ordered[end].block.text)}` : normalizeSelectionText(ordered[end].block.text);
      if (joined.includes(query)) candidates.push(ordered.slice(start, end + 1).map(({ block }) => block));
      // Once the accumulated text is longer than the query and no match was
      // found, adding another paragraph cannot produce a more precise match.
      if (joined.length > query.length && !joined.includes(query)) break;
      if (joined.includes(query)) break;
    }
  }
  if (!candidates.length) {
    const geometric = samePageGeometricCandidates(selection, ordered.map(({ block }) => block));
    return geometric.length ? geometric : null;
  }
  const shortest = Math.min(...candidates.map((candidate) => candidate.length));
  const best = candidates.filter((candidate) => candidate.length === shortest);
  if (best.length === 1) return best[0];

  // Duplicate text is common in headers, footers, and repeated figure
  // captions. Use the actual line rectangles only to disambiguate; otherwise
  // fail closed instead of attaching an arbitrary occurrence.
  if (selection.bbox || selection.rects?.length) {
    const ranked = best
      .map((candidate) => ({ candidate, score: candidateGeometryScore(selection, candidate) }))
      .sort((left, right) => right.score - left.score);
    if (ranked[0]?.score > 0 && ranked[0].score > (ranked[1]?.score ?? 0)) return ranked[0].candidate;
  }
  return null;
}

function samePageGeometricCandidates(selection: SelectionInput, blocks: SelectionBlock[]): SelectionBlock[] {
  const scored = blocks
    .filter((block): block is SelectionBlock & { bbox: SelectionBox } => Boolean(block.bbox))
    .map((block) => ({ block, score: blockGeometryScore(selection, block.bbox) }))
    .filter(({ score }) => score > 0)
    .sort((left, right) => (right.score - left.score) || ((left.block.order ?? 0) - (right.block.order ?? 0)));
  if (!scored.length) return [];
  const best = scored[0].score;
  // Keep adjacent blocks only when they materially overlap a selected line;
  // this enables recovery for OCR/text-layer drift without swallowing a whole
  // column or page.
  return scored.filter(({ score }) => score >= Math.max(best * 0.2, 0.00001)).map(({ block }) => block);
}

function candidateGeometryScore(selection: SelectionInput, candidate: SelectionBlock[]): number {
  return candidate.reduce((total, block) => total + (block.bbox ? blockGeometryScore(selection, block.bbox) : 0), 0);
}

function blockGeometryScore(selection: SelectionInput, block: SelectionBox): number {
  if (selection.rects?.length) return selection.rects.reduce((total, rect) => total + overlap(rect, block), 0);
  return selection.bbox ? overlap(selection.bbox, block) : 0;
}
