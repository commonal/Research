import type { CitationRef, KnowledgeWorkbenchStatus, WorkbenchClient, WorkbenchExplorationProfile, WorkbenchExplorationRun, WorkbenchMessage, WorkbenchNoteRun, WorkbenchPaper, WorkbenchSession, WorkbenchWorkspaceState, WorkspaceDecisionPoint, WorkspaceDocument } from "./types";

type ApiSession = { session_id: string; title: string; lifecycle: "active" | "archived"; paper_panel_open: boolean; active_paper_id: string | null; created_at: string; workspace_id?: string | null; workspace_title?: string | null };
type ApiPaper = { paper_id: string; title: string | null; source_url: string | null; pdf_url: string; pdf_status: string; parse_status: string; safe_error: string | null; note_status?: "idle" | "generating" | "awaiting_approval" | "published" | "failed"; note_url?: string | null; sample_block_id?: string | null; sample_section_path?: string[]; sample_page?: number | null };
export type ApiBlock = {
  block_id: string;
  text: string;
  section_path: string[];
  page: number | null;
  order?: number;
  bbox?: [number, number, number, number] | null;
  bbox_format?: "xyxy";
  bbox_space?: "page_points" | "normalized_1000";
  bbox_dimensions?: [number, number] | null;
};

export type HydratedCitationBlock = { paper_id: string; block: ApiBlock };

function citationBbox(block: Pick<ApiBlock, "bbox" | "bbox_space" | "bbox_dimensions">): CitationRef["bbox"] {
  const value = block.bbox;
  if (!Array.isArray(value) || value.length < 4) return undefined;
  const dimensions = block.bbox_dimensions;
  const width = dimensions?.[0] ?? (block.bbox_space === "normalized_1000" ? 1000 : 1);
  const height = dimensions?.[1] ?? (block.bbox_space === "normalized_1000" ? 1000 : 1);
  if (block.bbox_space === "normalized_1000" || dimensions) {
    return {
      x: value[0] / width,
      y: value[1] / height,
      width: Math.max(0, value[2] - value[0]) / width,
      height: Math.max(0, value[3] - value[1]) / height,
    };
  }
  return { x: value[0], y: value[1], width: Math.max(0, value[2] - value[0]), height: Math.max(0, value[3] - value[1]) };
}

function citationFromBlock(item: HydratedCitationBlock): CitationRef {
  const { block } = item;
  return {
    block_id: block.block_id,
    paper_id: item.paper_id,
    label: block.page ? `论文证据 · 第 ${block.page} 页` : "论文证据",
    status: "resolved",
    page: block.page ?? undefined,
    bbox: citationBbox(block),
  };
}

function citationTokenAliases(blockId: string): string[] {
  const aliases = [blockId];
  const normalized = blockId.match(/^normalized:[^:]+:(text|figure|formula|table):(.+)$/);
  if (normalized) {
    aliases.push(`${normalized[1]}:${normalized[2]}`);
    aliases.push(normalized[2]);
  }
  return aliases;
}

function extractCitationTokens(text: string): string[] {
  const pattern = /(?:normalized:[^:\s\[\]()[\]（）`]+:(?:text|figure|formula|table):[^\s\[\]()[\]（）`，、；;，,。！？!?]+|(?:text|figure|formula|table):[^\s\[\]()[\]（）`，、；;，,。！？!?]+|[0-9a-fA-F]{8,64})/g;
  return Array.from(text.matchAll(pattern), (match) => match[0].replace(/^`|`$/g, ""));
}

/**
 * Recover structured citation locators for historical messages whose text
 * still contains normalized/typed block markers but whose API citation list
 * is empty (older runs predate citation projection). Only blocks loaded from
 * the current session can be promoted; unknown markers remain absent.
 */
export function inferCitationsFromText(
  text: string,
  existing: CitationRef[],
  blocks: HydratedCitationBlock[],
): CitationRef[] {
  const exact = new Map<string, HydratedCitationBlock>();
  const aliases = new Map<string, HydratedCitationBlock | null>();
  for (const item of blocks) {
    if (item.block.page == null) continue;
    exact.set(item.block.block_id, item);
    for (const alias of citationTokenAliases(item.block.block_id)) {
      if (!aliases.has(alias)) {
        aliases.set(alias, item);
        continue;
      }
      const previous = aliases.get(alias);
      if (previous && previous.block.block_id !== item.block.block_id) aliases.set(alias, null);
    }
  }
  const resolve = (token: string): HydratedCitationBlock | undefined => {
    const exactMatch = exact.get(token);
    if (exactMatch) return exactMatch;
    const alias = aliases.get(token);
    return alias === null ? undefined : alias;
  };
  const result = [...existing];
  for (const token of extractCitationTokens(text)) {
    const item = resolve(token);
    if (!item) continue;
    const canonicalId = item.block.block_id;
    const existingIndex = result.findIndex((citation) => {
      if (citation.block_id === canonicalId) return true;
      const existingItem = resolve(citation.block_id);
      return existingItem?.block.block_id === canonicalId;
    });
    if (existingIndex >= 0) {
      if (result[existingIndex].status !== "resolved") result[existingIndex] = citationFromBlock(item);
      continue;
    }
    result.push(citationFromBlock(item));
  }
  return result;
}

export class WorkbenchApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly code?: string,
  ) {
    super(message);
    this.name = "WorkbenchApiError";
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const raw = (await response.text()).trim();
    let message = raw || `HTTP ${response.status}`;
    let code: string | undefined;
    try {
      const payload = JSON.parse(raw) as { detail?: unknown };
      const detail = payload?.detail;
      if (typeof detail === "string") message = detail;
      else if (detail && typeof detail === "object") {
        const value = detail as { message?: unknown; code?: unknown };
        if (typeof value.message === "string") message = value.message;
        if (typeof value.code === "string") code = value.code;
      }
    } catch {
      // Preserve plain-text backend errors as-is.
    }
    throw new WorkbenchApiError(message, response.status, code);
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}

type OptionalCollection<T> = { items: T[]; load_error?: string };

async function loadOptionalCollection<T>(url: string, label: string): Promise<OptionalCollection<T>> {
  try {
    return await request<{ items: T[] }>(url);
  } catch {
    return { items: [], load_error: `${label}加载失败，请刷新页面；如果仍然失败，请检查后端日志` };
  }
}

function paperFromApi(paper: ApiPaper): WorkbenchPaper {
  const fallback = paper.source_url?.match(/arxiv\.org\/(?:abs|pdf)\/([^/?#]+)/i)?.[1]
    ? `arXiv 论文（${paper.source_url.match(/arxiv\.org\/(?:abs|pdf)\/([^/?#]+)/i)?.[1]}）`
    : "未命名论文";
  return {
    paper_id: paper.paper_id,
    title: paper.title?.trim() || fallback,
    source_url: paper.source_url,
    pdf_url: paper.pdf_url,
    preparation_status: paper.parse_status === "ready" ? "ready" : paper.parse_status === "failed" ? "failed" : paper.parse_status === "parsing" ? "parsing" : "fetching",
    parse_error: paper.safe_error,
    note_status: paper.note_status ?? "idle",
    note_url: paper.note_url ?? null,
    sample_block_id: paper.sample_block_id,
    sample_section_path: paper.sample_section_path ?? [],
    sample_page: paper.sample_page,
    selection_blocks: [],
  };
}

function noteRunFromApi(run: any): WorkbenchNoteRun {
  return {
    note_run_id: String(run.note_run_id),
    paper_id: String(run.paper_id),
    triggering_session_id: String(run.triggering_session_id),
    status: run.status,
    attempt: Number(run.attempt ?? 1),
    previous_note_run_id: run.previous_note_run_id ?? null,
    receipt_path: run.receipt_path ?? null,
    draft_path: run.draft_path ?? null,
    knowledge_id: run.knowledge_id ?? null,
    safe_error: run.safe_error ?? null,
    stage: run.stage ?? run.status ?? "queued",
    created_at: run.created_at ?? null,
    draft_available: Boolean(run.draft_available ?? run.draft_path),
    draft_preview: typeof run.draft_preview === "string" ? run.draft_preview : null,
    can_publish: Boolean(run.can_publish ?? run.status === "awaiting_approval"),
    events: (run.events ?? []).map((event: any) => ({ sequence_no: event.sequence_no, event_type: event.event_type, summary: event.summary, stable_ids: event.stable_ids ?? {}, counters: event.counters ?? {} })),
    tool_uses: (run.tool_uses ?? []).map((item: any) => ({ name: item.operation, calls: item.calls })),
    token_usage: { input_tokens: run.token_usage?.input_tokens ?? 0, output_tokens: run.token_usage?.output_tokens ?? 0 },
    elapsed_ms: run.elapsed_ms ?? 0,
  };
}

/**
 * The history rail only needs session metadata. Hydrating every session here
 * makes entering the workbench fan out into hundreds of requests (papers,
 * messages, explorations, note runs and evidence blocks). Keep this projection
 * intentionally shallow; the active session is hydrated by getSession below.
 */
function sessionSummaryFromApi(session: ApiSession): WorkbenchSession {
  return {
    session_id: session.session_id,
    title: session.title?.trim() || "新会话",
    title_source: "user",
    lifecycle: session.lifecycle,
    workspace_id: session.workspace_id ?? null,
    workspace_title: session.workspace_title ?? null,
    papers: [],
    active_paper_id: session.active_paper_id ?? null,
    messages: [],
    exploration_runs: [],
    paper_panel_open: session.paper_panel_open,
    updated_at: session.created_at,
  };
}

function messageFromApi(item: any, hydratedBlocks: HydratedCitationBlock[] = []): WorkbenchMessage {
  const citations: CitationRef[] = (item.citations ?? []).map((citation: any) => {
    const box = citation.locator?.bbox;
    const locator = citation.locator ?? {};
    return { block_id: citation.block_id, paper_id: citation.paper_id, label: locator.page ? `论文证据 · 第 ${locator.page} 页` : "论文证据", status: citation.status, page: locator.page, bbox: citationBbox({ bbox: box, bbox_space: locator.bbox_space, bbox_dimensions: locator.bbox_dimensions }) };
  });
  return {
    message_id: item.message_id, role: item.role, text: item.text,
    generation_status: item.generation_status, created_at: item.created_at,
    contexts: (item.metadata?.context_chips ?? []).map((chip: any, index: number) => ({ paper_id: chip.paper_id, scope: item.scope, block_id: chip.block_id, block_ids: item.metadata?.selection_block_ids ?? undefined, section_path: chip.section_path, label: chip.page ? `已使用论文选区 · 第 ${chip.page} 页` : "已使用论文选区", status: "attached", page: chip.page, text: index === 0 ? item.metadata?.selected_text ?? undefined : undefined })),
    citations: inferCitationsFromText(item.text, citations, hydratedBlocks),
  };
}

function explorationFromApi(run: any): WorkbenchExplorationRun {
  const profile: WorkbenchExplorationRun["profile"] = run.config_snapshot?.profile === "assistant" ? "assistant" : "literature";
  const applicationBudgetMode = run.application_budget_mode
    ?? (run.budgets?.experimental_unbounded ? "experimental_unbounded" : "bounded");
  return { run_id: run.run_id, current_attempt_id: run.current_attempt_id, current_attempt: run.current_attempt, attempt_history: run.attempt_history ?? [], budget: run.budget, failure: run.failure, tool_details: run.tool_details ?? [], question: run.question_snapshot, attempt: run.current_attempt?.attempt_no ?? run.attempt, status: run.status, phase: run.phase ?? run.status, profile, created_at: run.created_at, continuation_mode: run.continuation_mode, continuation_label: run.continuation_label, application_budget_mode: applicationBudgetMode, retrieval_plan: run.retrieval_plan ?? run.config_snapshot?.resolved?.retrieval_plan ?? null, budget_used: { tool_calls: run.counters?.tool_calls ?? 0, tool_limit: run.budgets?.tool_calls ?? 16, block_reads: run.counters?.block_reads ?? 0, block_limit: run.budgets?.block_reads ?? 24 }, tools_used: (run.tools_used ?? []).map((item: any) => ({ name: item.tool, calls: item.calls })), token_usage: { input_tokens: run.token_usage?.input_tokens ?? 0, output_tokens: run.token_usage?.output_tokens ?? 0 }, web_search_usage: { calls: run.web_search_usage?.calls ?? 0, input_tokens: run.web_search_usage?.input_tokens ?? 0, output_tokens: run.web_search_usage?.output_tokens ?? 0, total_tokens: run.web_search_usage?.total_tokens ?? 0, cached_tokens: run.web_search_usage?.cached_tokens ?? 0, reasoning_tokens: run.web_search_usage?.reasoning_tokens ?? 0 }, sources: (run.sources ?? []).map((item: any) => ({ source_id: item.source_id, title: item.title, kind: item.kind, url: item.url, relevance: item.relevance })), partial_result: run.final_draft ?? null, events: (run.events ?? []).map((event: any) => ({ attempt_id: event.attempt_id, sequence_no: event.sequence_no, event_type: event.event_type, summary: event.summary, stable_ids: event.stable_ids ?? {}, counters: event.counters ?? {}, occurred_at: event.occurred_at ?? null })) };
}

function mutationKey(action: string, runId: string): string {
  return `${action}:${runId}:${globalThis.crypto?.randomUUID?.() ?? Date.now()}`;
}

export function createHttpWorkbenchClient(baseUrl = "/api"): WorkbenchClient {
  const sessionUrl = (id: string) => `${baseUrl}/workbench/sessions/${encodeURIComponent(id)}`;
  const hydrate = async (session: ApiSession): Promise<WorkbenchSession> => {
    const [papers, messages, explorations, notes] = await Promise.all([
      request<{ items: ApiPaper[] }>(`${sessionUrl(session.session_id)}/papers`),
      loadOptionalCollection<any>(`${sessionUrl(session.session_id)}/messages`, "会话消息"),
      loadOptionalCollection<any>(`${sessionUrl(session.session_id)}/explorations`, "探索历史"),
      loadOptionalCollection<any>(`${sessionUrl(session.session_id)}/note-runs`, "笔记运行"),
    ]);
    const projectedPapers = papers.items.map(paperFromApi);
    await Promise.all(projectedPapers.map(async (paper) => {
      if (paper.preparation_status !== "ready") return;
      const blocks = await request<{ items: ApiBlock[] }>(`${sessionUrl(session.session_id)}/papers/${paper.paper_id}/blocks`).catch(() => ({ items: [] }));
      paper.selection_blocks = blocks.items;
    }));
    const noteRuns = notes.items.map(noteRunFromApi);
    for (const run of noteRuns) {
      const paper = projectedPapers.find((item) => item.paper_id === run.paper_id);
      if (!paper) continue;
      paper.note_events = run.events;
      paper.note_tool_uses = run.tool_uses;
      paper.note_token_usage = run.token_usage;
      paper.note_elapsed_ms = run.elapsed_ms ?? 0;
      if (run.status === "published") { paper.note_status = "published"; paper.note_url = run.knowledge_id ?? null; paper.note_error = null; paper.note_stage = run.stage ?? "published"; }
      else if (run.status === "failed") { paper.note_status = "failed"; paper.note_error = run.safe_error ?? "正式笔记生成失败"; paper.note_stage = run.stage ?? "failed"; }
      else if (run.status === "awaiting_approval") { paper.note_status = "awaiting_approval"; paper.note_stage = run.stage ?? "awaiting_approval"; }
      else { paper.note_status = "generating"; paper.note_stage = run.stage ?? "generating_note"; }
    }
    const historyError = [messages.load_error, explorations.load_error, notes.load_error].filter(Boolean).join("；") || null;
    const hydratedBlocks = projectedPapers.flatMap((paper) => (paper.selection_blocks ?? []).map((block) => ({ paper_id: paper.paper_id, block })));
    return { ...session, title_source: "user", papers: projectedPapers, note_runs: noteRuns, active_paper_id: session.active_paper_id, messages: messages.items.map((item) => messageFromApi(item, hydratedBlocks)), exploration_runs: explorations.items.map(explorationFromApi), history_error: historyError, updated_at: session.created_at };
  };
  const reload = async (id: string) => hydrate(await request<ApiSession>(sessionUrl(id)));
  return {
    async listSessions(includeArchived = false) {
      const result = await request<{ items: ApiSession[] }>(`${baseUrl}/workbench/sessions${includeArchived ? "" : "?lifecycle=active"}`);
      // Do not hydrate the complete history on entry. A user normally opens
      // one session, so loading all details up front creates a request storm
      // and leaves the UI looking stuck while the backend serializes SQLite
      // reads. Full data is loaded lazily by getSession when a session opens.
      return result.items.map(sessionSummaryFromApi);
    },
    async createSession(workspaceId) { return hydrate(await request<ApiSession>(`${baseUrl}/workbench/sessions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(workspaceId ? { workspace_id: workspaceId } : {}) })); },
    async createQuestionSession(question) { return hydrate(await request<ApiSession>(`${baseUrl}/workbench/sessions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ research_question: question }) })); },
    async createKnowledgeSession(knowledgeId, workspaceId) {
      return hydrate(await request<ApiSession>(`${baseUrl}/knowledge/${encodeURIComponent(knowledgeId)}/workbench-session`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(workspaceId ? { workspace_id: workspaceId } : {}),
      }));
    },
    async getKnowledgePaperStatus(knowledgeId): Promise<KnowledgeWorkbenchStatus> {
      return request<KnowledgeWorkbenchStatus>(`${baseUrl}/knowledge/${encodeURIComponent(knowledgeId)}/workbench-status`);
    },
    async importKnowledgeSession(knowledgeId, workspaceId) {
      return hydrate(await request<ApiSession>(`${baseUrl}/knowledge/${encodeURIComponent(knowledgeId)}/workbench-import`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(workspaceId ? { workspace_id: workspaceId } : {}),
      }));
    },
    getSession: reload,
    async renameSession(id, title) { return hydrate(await request<ApiSession>(sessionUrl(id), { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title }) })); },
    async updateLayout(id, open, activePaperId) { return hydrate(await request<ApiSession>(sessionUrl(id), { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paper_panel_open: open, ...(activePaperId === undefined ? {} : { active_paper_id: activePaperId }) }) })); },
    async archiveSession(id) { return hydrate(await request<ApiSession>(`${sessionUrl(id)}/archive`, { method: "POST" })); },
    async deleteSession(id) { await request(`${sessionUrl(id)}`, { method: "DELETE" }); },
    async renameWorkspace(workspaceId, title) { return request<{ workspace_id: string; title: string }>(`${baseUrl}/workbench/workspace/${encodeURIComponent(workspaceId)}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title }) }); },
    async deleteWorkspace(workspaceId) { await request(`${baseUrl}/workbench/workspace/${encodeURIComponent(workspaceId)}`, { method: "DELETE" }); },
    async uploadPdf(id, file) { const paper = await request<ApiPaper>(`${sessionUrl(id)}/papers/upload`, { method: "POST", headers: { "Content-Type": "application/pdf" }, body: file }); await request(sessionUrl(id), { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paper_panel_open: true, active_paper_id: paper.paper_id }) }); return reload(id); },
    async addPaperUrl(id, url, title) { const paper = await request<ApiPaper>(`${sessionUrl(id)}/papers/url`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url, ...(title ? { title } : {}) }) }); await request(sessionUrl(id), { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paper_panel_open: true, active_paper_id: paper.paper_id }) }); return reload(id); },
    async retryPaper(id, paperId) { await request(`${sessionUrl(id)}/papers/${paperId}/retry`, { method: "POST" }); return reload(id); },
    async addFixtureMessage(id, text, context) { await request(`${sessionUrl(id)}/messages`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ query: text, scope: context?.scope ?? "none", ...(context?.paper_id ? { paper_id: context.paper_id } : {}), block_id: context?.block_id, block_ids: context?.block_ids ?? (context?.block_id ? [context.block_id] : []), selected_text: context?.text, selection_rects: context?.rects ?? [], selection_page_size: context?.page_size, section_path: context?.section_path ?? [] }) }); return reload(id); },
    async startFixtureExploration(id, question, profile, interactionContext) { await request(`${sessionUrl(id)}/messages`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ query: question, scope: "explore", ...(profile ? { profile } : {}), ...(interactionContext ? { interaction_context: interactionContext } : {}) }) }); return reload(id); },
    async sendTurn(id, text, options = {}) {
      const context = options.context;
      const interactionContext = options.interactionContext;
      const selectionAction = options.action === "translate" || options.action === "explain" || options.action === "ask_selection";
      const scope = context?.scope ?? (selectionAction ? "selection" : "none");
      await request(`${sessionUrl(id)}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query: text,
          scope,
          ...(options.action ? { action: options.action } : {}),
          ...(options.profile ? { profile: options.profile } : {}),
          ...(context?.paper_id ? { paper_id: context.paper_id } : {}),
          ...(context?.block_id ? { block_id: context.block_id } : {}),
          ...(context?.block_ids ? { block_ids: context.block_ids } : {}),
          ...(context?.text ? { selected_text: context.text } : {}),
          ...(context?.section_path ? { section_path: context.section_path } : {}),
          ...(interactionContext ? { interaction_context: interactionContext } : {}),
          client_request_id: globalThis.crypto?.randomUUID?.() ?? `turn-${Date.now()}`,
        }),
      });
      return reload(id);
    },
    async cancelFixtureExploration(id, runId) { const run = await request<any>(`${baseUrl}/workbench/explorations/${runId}`); await request(`${baseUrl}/workbench/explorations/${runId}/cancel`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ expected_attempt_id: run.current_attempt_id, idempotency_key: mutationKey("cancel", runId) }) }); return reload(id); },
    async continueExploration(id, runId) { const run = await request<any>(`${baseUrl}/workbench/explorations/${runId}`); await request(`${baseUrl}/workbench/explorations/${runId}/continue`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ expected_attempt_id: run.current_attempt_id, idempotency_key: mutationKey("continue", runId) }) }); return reload(id); },
    async startFixtureNoteRun(id, paperId) { await request(`${sessionUrl(id)}/papers/${paperId}/note-runs`, { method: "POST" }); return reload(id); },
    async publishNoteRun(noteRunId) { const run = await request<any>(`${baseUrl}/workbench/note-runs/${encodeURIComponent(noteRunId)}/publish`, { method: "POST" }); return reload(run.triggering_session_id); },
    async fetchWorkspace(id) {
      const response = await fetch(`${baseUrl}/workbench/workspaces/${encodeURIComponent(id)}`);
      if (response.status === 404) return null;
      if (!response.ok) throw new Error("无法读取工作区状态");
      return response.json() as Promise<WorkbenchWorkspaceState>;
    },
    async fetchWorkspaceDocuments(id) {
      const response = await fetch(`${baseUrl}/workbench/workspaces/${encodeURIComponent(id)}/documents`);
      if (!response.ok) throw new Error("无法读取研究文档");
      const payload = await response.json() as { items: WorkspaceDocument[] };
      return payload.items;
    },
    async setWorkspaceFocus(id, questionId) {
      const response = await fetch(`${baseUrl}/workbench/workspaces/${encodeURIComponent(id)}/focus`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question_id: questionId }),
      });
      if (!response.ok) throw new Error((await response.text()) || "无法设置研究焦点");
      return response.json() as Promise<WorkbenchWorkspaceState>;
    },
    async resolveWorkspaceDecision(id, decisionId, approved, decision, questionId) {
      const response = await fetch(`${baseUrl}/workbench/workspaces/${encodeURIComponent(id)}/decisions/${encodeURIComponent(decisionId)}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approved, decision, ...(questionId ? { question_id: questionId } : {}) }),
      });
      if (!response.ok) throw new Error("无法提交决策");
      return response.json() as Promise<{ decision: WorkspaceDecisionPoint }>;
    },
  };
}
