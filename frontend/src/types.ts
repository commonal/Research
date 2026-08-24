export type KnowledgeItem = {
  knowledge_id: string;
  knowledge_version: string;
  title: string;
  domain: string;
  evidence_level: string;
  source_url: string;
  provenance_status?: "complete" | "legacy_missing_provenance";
};

export type KnowledgeDetail = {
  knowledge_id: string;
  knowledge_version: string;
  title: string;
  domain: string;
  evidence_level: string;
  source_urls: string[];
  markdown: string;
  provenance_status: "complete" | "legacy_missing_provenance";
  anchors: EvidenceAnchor[];
  reading_mode?: "deep_reading" | "legacy";
  evidence_model?: "section_anchors" | "legacy_v2" | "legacy";
  reading_sections?: ReadingSectionEvidence[];
};

export type ReadingSectionEvidence = {
  section_name: "summary" | "problem" | "research_question" | "core_idea" | "method" | "workflow" | "experiments" | "experiment_design" | "result_interpretation" | "limitations" | "reproduction";
  anchor_ids: string[];
  visuals?: ReadingVisualEvidence[];
};

export type ReadingVisualEvidence = {
  block_id: string;
  kind: "formula" | "table" | "figure";
  role: string;
  explanation: string;
  asset_path?: string | null;
};

export type ReviewIssue = {
  code: string;
  severity: string;
  claim_id?: string | null;
  anchor_id?: string | null;
};

export type ReviewDraftSummary = {
  draft_id: string;
  knowledge_id: string;
  knowledge_version: string;
  title: string;
  domain: string;
  status: "needs_review" | "rejected" | "published" | "expired";
  source_urls: string[];
  quality_issues: ReviewIssue[];
  evidence_boundary: string;
  created_at: string;
  updated_at: string;
};

export type ReviewDraft = ReviewDraftSummary & {
  source_id: string;
  markdown: string;
  content_sha256: string;
};

export type EvidenceAnchor = {
  anchor_id: string;
  source_url: string;
  section?: string | null;
  page_start?: number | null;
  page_end?: number | null;
  figure_or_table?: string | null;
  block_kind?: "text" | "formula" | "table" | "figure" | "caption" | null;
  parse_status?: "available" | "degraded" | "unparsed" | null;
  locator_completeness?: "exact" | "partial" | "section_only" | "missing" | null;
  bbox?: number[] | null;
  evidence_excerpt: string;
  excerpt_sha256: string;
};

export type Citation = {
  knowledge_id: string;
  knowledge_version: string;
  anchor_id?: string | null;
  source_url: string;
  claim_id?: string | null;
  claim_type?: "source_fact" | "agent_inference" | "reading_question" | null;
  source_anchors?: EvidenceAnchor[];
};

export type ChatResponse =
  | { status: "completed"; thread_id: string; answer: string; citations: Citation[] }
  | { status: "needs_confirmation"; thread_id: string; interrupt: { message: string; reason?: string } };

export type ProductionRunStatus = "queued" | "running" | "completed" | "partial_failed" | "failed";
export type ProductionRunTrigger = "initial" | "manual" | "scheduled";

export type ResearchTopic = {
  topic_id: string;
  name: string;
  query: string;
  domain: string;
  created_at: string;
  enabled: boolean;
  daily_limit: number;
  last_successful_discovery_at: string | null;
};

export type ProductionRun = {
  run_id: string;
  topic_id: string;
  status: ProductionRunStatus;
  limit: number;
  candidate_count: number;
  published_count: number;
  failed_count: number;
  error_code: string | null;
  error_summary: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  trigger: ProductionRunTrigger;
  window_start: string | null;
  window_end: string | null;
  scheduled_for: string | null;
};

export type TopicWithLatestRun = {
  topic: ResearchTopic;
  latest_run: ProductionRun | null;
};

export type CreateTopicResponse = {
  topic: ResearchTopic;
  run: ProductionRun;
};

export type SchedulerStatus = {
  enabled: boolean;
  timezone: string;
  daily_time: string;
  next_run_at: string | null;
};
