import type {
  ChatResponse,
  CreateTopicResponse,
  KnowledgeDetail,
  KnowledgeItem,
  ProductionRun,
  ResearchTopic,
  SchedulerStatus,
  ReviewDraft,
  ReviewDraftSummary,
  TopicWithLatestRun,
} from "./types";

const baseUrl = import.meta.env.VITE_API_BASE_URL ?? "/api";

export async function fetchKnowledge(): Promise<KnowledgeItem[]> {
  const response = await fetch(`${baseUrl}/knowledge?limit=30`);
  if (!response.ok) throw new Error("知识时间线暂不可用");
  const payload = (await response.json()) as { items: KnowledgeItem[] };
  return payload.items;
}

export class KnowledgeNotFoundError extends Error {}

export function knowledgeDetailUrl(knowledgeId: string): string {
  return `${baseUrl}/knowledge/${encodeURIComponent(knowledgeId)}`;
}

export async function fetchKnowledgeDetail(
  knowledgeId: string,
  signal?: AbortSignal,
): Promise<KnowledgeDetail> {
  const response = await fetch(knowledgeDetailUrl(knowledgeId), { signal });
  if (response.status === 404) throw new KnowledgeNotFoundError("知识不存在或不可用");
  if (!response.ok) throw new Error("知识详情暂不可用");
  return response.json() as Promise<KnowledgeDetail>;
}

export async function fetchReviewDrafts(knowledgeId: string, signal?: AbortSignal): Promise<ReviewDraftSummary[]> {
  const response = await fetch(`${baseUrl}/knowledge/${encodeURIComponent(knowledgeId)}/review-drafts`, { signal });
  if (!response.ok) throw new Error("待审核精读暂不可用");
  const payload = (await response.json()) as { items: ReviewDraftSummary[] };
  return payload.items;
}

export async function fetchReviewDraft(knowledgeId: string, draftId: string, signal?: AbortSignal): Promise<ReviewDraft> {
  const response = await fetch(
    `${baseUrl}/knowledge/${encodeURIComponent(knowledgeId)}/review-drafts/${encodeURIComponent(draftId)}`,
    { signal },
  );
  if (response.status === 404) throw new Error("待审核精读不存在");
  if (!response.ok) throw new Error("待审核精读暂不可用");
  return response.json() as Promise<ReviewDraft>;
}

async function reviewDraftAction(knowledgeId: string, draftId: string, action: "approve" | "reject"): Promise<ReviewDraft> {
  const response = await fetch(
    `${baseUrl}/knowledge/${encodeURIComponent(knowledgeId)}/review-drafts/${encodeURIComponent(draftId)}/${action}`,
    { method: "POST" },
  );
  if (response.status === 409) {
    const payload = (await response.json()) as { detail?: { reason?: string; message?: string } };
    throw new Error(payload.detail?.reason ?? payload.detail?.message ?? "待审核状态已变化");
  }
  if (!response.ok) throw new Error("待审核操作暂不可用");
  const payload = (await response.json()) as { draft: ReviewDraft } | ReviewDraft;
  return "draft" in payload ? payload.draft : payload;
}

export function approveReviewDraft(knowledgeId: string, draftId: string): Promise<ReviewDraft> {
  return reviewDraftAction(knowledgeId, draftId, "approve");
}

export function rejectReviewDraft(knowledgeId: string, draftId: string): Promise<ReviewDraft> {
  return reviewDraftAction(knowledgeId, draftId, "reject");
}

export async function startChat(query: string, knowledgeId?: string): Promise<ChatResponse> {
  const response = await fetch(`${baseUrl}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, knowledge_ids: knowledgeId ? [knowledgeId] : [] }),
  });
  if (!response.ok) throw new Error("问答服务暂不可用");
  return response.json() as Promise<ChatResponse>;
}

export async function resumeChat(threadId: string, approved: boolean): Promise<ChatResponse> {
  const response = await fetch(`${baseUrl}/chat/${threadId}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approved }),
  });
  if (!response.ok) throw new Error("无法恢复本次问答");
  return response.json() as Promise<ChatResponse>;
}

export class ActiveRunConflictError extends Error {
  constructor(public readonly runId: string) {
    super("该方向已有抓取任务正在运行");
    this.name = "ActiveRunConflictError";
  }
}

export async function fetchResearchTopics(signal?: AbortSignal): Promise<TopicWithLatestRun[]> {
  const response = await fetch(`${baseUrl}/research-topics`, { signal });
  if (!response.ok) throw new Error("研究方向暂不可用");
  const payload = (await response.json()) as { items: TopicWithLatestRun[] };
  return payload.items;
}

export async function fetchSchedulerStatus(signal?: AbortSignal): Promise<SchedulerStatus> {
  const response = await fetch(`${baseUrl}/scheduler`, { signal });
  if (!response.ok) throw new Error("自动更新状态暂不可用");
  return response.json() as Promise<SchedulerStatus>;
}

export async function updateResearchTopic(
  topicId: string,
  settings: { enabled?: boolean; daily_limit?: number },
): Promise<ResearchTopic> {
  const response = await fetch(`${baseUrl}/research-topics/${encodeURIComponent(topicId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
  if (response.status === 404) throw new Error("研究方向不存在");
  if (response.status === 422) throw new Error("自动更新设置无效");
  if (!response.ok) throw new Error("无法更新研究方向");
  return response.json() as Promise<ResearchTopic>;
}

export async function createResearchTopic(name: string, query: string): Promise<CreateTopicResponse> {
  const response = await fetch(`${baseUrl}/research-topics`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, query }),
  });
  if (response.status === 422) throw new Error("请填写有效的方向名称和检索词");
  if (!response.ok) throw new Error("无法保存研究方向");
  return response.json() as Promise<CreateTopicResponse>;
}

export async function retryResearchTopic(topicId: string): Promise<ProductionRun> {
  const response = await fetch(`${baseUrl}/research-topics/${encodeURIComponent(topicId)}/runs`, {
    method: "POST",
  });
  if (response.status === 409) {
    const payload = (await response.json()) as { detail?: { run_id?: string } };
    if (payload.detail?.run_id) throw new ActiveRunConflictError(payload.detail.run_id);
  }
  if (response.status === 404) throw new Error("研究方向不存在");
  if (!response.ok) throw new Error("无法重新抓取该方向");
  return response.json() as Promise<ProductionRun>;
}

export async function fetchProductionRun(runId: string, signal?: AbortSignal): Promise<ProductionRun> {
  const response = await fetch(`${baseUrl}/production-runs/${encodeURIComponent(runId)}`, { signal });
  if (response.status === 404) throw new Error("生产任务不存在");
  if (!response.ok) throw new Error("无法读取生产进度");
  return response.json() as Promise<ProductionRun>;
}
