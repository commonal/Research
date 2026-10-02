import type {
  CitationRef,
  PaperContextRef,
  WorkbenchChatClient,
  WorkbenchMessage,
} from "./types";

type ApiChip = {
  paper_id: string;
  block_id: string;
  section_path: string[];
  page: number | null;
  truncated: boolean;
};

type ApiCitation = {
  block_id: string;
  status: "resolved" | "unresolved" | "detached";
  locator: { page: number | null; bbox: number[] | null; section_path: string[] } | null;
};

type ApiMessage = {
  message_id: string;
  role: "user" | "assistant" | "system";
  text: string;
  generation_status: "queued" | "completed" | "generating" | "failed";
  created_at: string;
  metadata: { scope: PaperContextRef["scope"]; context_chips?: ApiChip[] };
  citations: ApiCitation[];
};

function bbox(value: number[] | null | undefined) {
  if (!value || value.length !== 4) return undefined;
  return { x: value[0], y: value[1], width: value[2] - value[0], height: value[3] - value[1] };
}

function messageFromApi(message: ApiMessage): WorkbenchMessage {
  const contexts: PaperContextRef[] = (message.metadata.context_chips ?? []).map((chip) => ({
    paper_id: chip.paper_id,
    scope: message.metadata.scope,
    block_id: chip.block_id,
    section_path: chip.section_path,
    label: chip.section_path.join(" / ") || chip.block_id,
    status: "attached",
    page: chip.page ?? undefined,
  }));
  const citations: CitationRef[] = message.citations.map((citation) => ({
    block_id: citation.block_id,
    label: citation.block_id,
    status: citation.status,
    page: citation.locator?.page ?? undefined,
    bbox: bbox(citation.locator?.bbox),
  }));
  return { ...message, contexts, citations };
}

export function createHttpWorkbenchChatClient(baseUrl = "/api"): WorkbenchChatClient {
  const endpoint = (sessionId: string) =>
    `${baseUrl}/workbench/sessions/${encodeURIComponent(sessionId)}/messages`;

  return {
    async listMessages(sessionId) {
      const response = await fetch(endpoint(sessionId));
      if (!response.ok) throw new Error("无法读取会话消息");
      const payload = (await response.json()) as { items: ApiMessage[] };
      return payload.items.map(messageFromApi);
    },
    async sendFixedMessage(sessionId, query, context) {
      const response = await fetch(endpoint(sessionId), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query,
          scope: context?.scope ?? "none",
          paper_id: context?.paper_id,
          block_id: context?.block_id,
          section_path: context?.section_path ?? [],
        }),
      });
      if (!response.ok) throw new Error("固定范围问答暂不可用");
      return messageFromApi((await response.json()) as ApiMessage);
    },
  };
}
