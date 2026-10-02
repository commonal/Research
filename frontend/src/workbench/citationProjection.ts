export type BlockLocator = {
  x: number;
  y: number;
  width: number;
  height: number;
};

export type SentCitationBlock = {
  block_id: string;
  label: string;
  page: number;
  bbox?: BlockLocator;
};

export type ProjectedCitation = {
  block_id: string;
  label: string;
  status: "resolved" | "unresolved";
  page?: number;
  bbox?: BlockLocator;
};

export function projectDraftCitations(text: string, sentBlocks: SentCitationBlock[]): ProjectedCitation[] {
  const sentById = new Map(sentBlocks.map((block) => [block.block_id, block]));
  const ids = [...text.matchAll(/\[(B\d+)\]/g)].map((match) => match[1]);
  return [...new Set(ids)].map((blockId) => {
    const block = sentById.get(blockId);
    if (!block) return { block_id: blockId, label: `[${blockId}]`, status: "unresolved" };
    return {
      block_id: blockId,
      label: `[${blockId}]`,
      status: "resolved",
      page: block.page,
      ...(block.bbox ? { bbox: block.bbox } : {}),
    };
  });
}
