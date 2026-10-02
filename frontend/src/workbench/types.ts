export type SessionLifecycle = "active" | "archived";
export type PaperPreparationStatus = "idle" | "fetching" | "parsing" | "ready" | "failed";
export type MessageRole = "user" | "assistant" | "system";
export type MessageGenerationStatus = "queued" | "completed" | "generating" | "failed";
export type ContextScope = "selection" | "section" | "full" | "none";
export type SelectionRect = { x: number; y: number; width: number; height: number };
export type SelectionPageSize = { width: number; height: number };

export type PaperContextRef = {
  paper_id: string;
  scope: ContextScope;
  block_id?: string;
  block_ids?: string[];
  section_path?: string[];
  label: string;
  text?: string;
  status: "attached" | "detached" | "unresolved";
  page?: number;
  bbox?: { x: number; y: number; width: number; height: number };
  rects?: SelectionRect[];
  page_size?: SelectionPageSize;
};

export type CitationRef = {
  block_id: string;
  paper_id?: string | null;
  label: string;
  status: "resolved" | "unresolved" | "detached";
  page?: number;
  bbox?: { x: number; y: number; width: number; height: number };
};

export type WorkbenchMessage = {
  message_id: string;
  role: MessageRole;
  text: string;
  generation_status: MessageGenerationStatus;
  contexts: PaperContextRef[];
  citations: CitationRef[];
  created_at: string;
};

export type TokenUsage = { input_tokens: number; output_tokens: number };

export type WorkbenchToolUse = { name: string; calls: number };

export type WorkbenchNoteEvent = {
  sequence_no: number;
  event_type: string;
  summary: string;
  stable_ids: Record<string, string>;
  counters: Record<string, number>;
};

export type WorkbenchExplorationEvent = {
  attempt_id?: string;
  sequence_no: number;
  event_type: string;
  summary: string;
  stable_ids: Record<string, string>;
  counters: Record<string, number>;
  occurred_at?: string | null;
};

export type WorkbenchExplorationProfile = "literature" | "assistant";

export type InteractionContextSelection = {
  paper_id: string;
  block_id: string;
  block_ids?: string[];
  text: string;
  page?: number | null;
  section_path?: string[];
  rects?: SelectionRect[];
  page_size?: SelectionPageSize;
};

export type InteractionContextPayload = {
  surface: string;
  canonical_paper_id?: string | null;
  research_question_id?: string | null;
  project_id?: string | null;
  selection?: InteractionContextSelection | null;
};

// Run-level status mirrors the current Attempt's status. Terminal attempt
// statuses (awaiting_user, retryable_failure, terminal_failure, abandoned)
// surface verbatim so the card never has to guess a failure category.
export type WorkbenchRunStatus =
  | "queued"
  | "running"
  | "completed"
  | "awaiting_user"
  | "awaiting_user_decision"
  | "budget_exhausted"
  | "cancelled"
  | "retryable_failure"
  | "terminal_failure"
  | "abandoned"
  | "failed";

export type WorkbenchExplorationRun = {
  run_id: string;
  current_attempt_id?: string;
  current_attempt?: WorkbenchAttempt | null;
  attempt_history?: WorkbenchAttempt[];
  budget?: { current: Record<string, number>; cumulative: Record<string, number> };
  failure?: { category: string; safe_message: string; recommended_action: string } | null;
  tool_details?: WorkbenchToolDetail[];
  question: string;
  attempt: number;
  status: WorkbenchRunStatus;
  phase: string;
  budget_used: { tool_calls: number; tool_limit: number; block_reads: number; block_limit: number };
  tools_used: WorkbenchToolUse[];
  token_usage: TokenUsage;
  web_search_usage?: WebSearchUsage;
  sources: { source_id: string; title: string; kind?: string; url?: string; relevance?: string }[];
  partial_result: string | null;
  events?: WorkbenchExplorationEvent[];
  profile?: WorkbenchExplorationProfile;
  created_at?: string;
  continuation_mode?: "persisted_results" | "checkpoint";
  continuation_strategy?: string | null;
  continuation_label?: string;
  application_budget_mode?: "bounded" | "experimental_unbounded";
  retrieval_plan?: "direct" | "web_lookup" | "paper_local" | "paper_evidence" | "research_exploration" | null;
};

export type WebSearchUsage = {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cached_tokens: number;
  reasoning_tokens: number;
};

export type WorkbenchToolDetail = {
  tool_call_id: string;
  tool_name: string;
  status: string;
  retryability: string;
  side_effect_state: string;
  error_code?: string | null;
  safe_message?: string | null;
  diagnostic_id?: string | null;
  recommended_action: string;
};

export type WorkbenchAttempt = {
  attempt_id: string;
  attempt_no: number;
  status: string;
  generation: number;
  budgets: Record<string, number>;
  application_budget_mode?: "bounded" | "experimental_unbounded";
  budget_used: Record<string, number>;
  failure?: { category: string; safe_message: string; recommended_action: string } | null;
  started_at?: string | null;
  finished_at?: string | null;
};

export type WorkbenchPaper = {
  paper_id: string;
  title: string;
  source_url: string | null;
  pdf_url: string;
  preparation_status: PaperPreparationStatus;
  parse_error: string | null;
  note_status: "idle" | "generating" | "awaiting_approval" | "published" | "failed";
  note_url: string | null;
  note_error?: string | null;
  note_stage?: string | null;
  note_events?: WorkbenchNoteEvent[];
  note_tool_uses?: WorkbenchToolUse[];
  note_token_usage?: TokenUsage;
  note_elapsed_ms?: number;
  sample_block_id?: string | null;
  sample_section_path?: string[];
  sample_page?: number | null;
  selection_blocks?: {
    block_id: string;
    text: string;
    section_path: string[];
    page: number | null;
    order?: number;
    bbox?: [number, number, number, number] | null;
    bbox_format?: "xyxy";
    bbox_space?: "page_points" | "normalized_1000";
    bbox_dimensions?: [number, number] | null;
  }[];
};

export type WorkbenchNoteRun = {
  note_run_id: string;
  paper_id: string;
  triggering_session_id: string;
  status: "queued" | "generating" | "awaiting_approval" | "published" | "failed";
  attempt: number;
  previous_note_run_id?: string | null;
  receipt_path?: string | null;
  draft_path?: string | null;
  knowledge_id?: string | null;
  safe_error?: string | null;
  stage: string;
  created_at?: string | null;
  draft_available?: boolean;
  draft_preview?: string | null;
  can_publish?: boolean;
  events: WorkbenchNoteEvent[];
  tool_uses?: WorkbenchToolUse[];
  token_usage?: TokenUsage;
  elapsed_ms?: number;
};

export type WorkbenchSession = {
  session_id: string;
  title: string;
  title_source: "default" | "message" | "paper" | "user";
  lifecycle: SessionLifecycle;
  workspace_id?: string | null;
  workspace_title?: string | null;
  papers: WorkbenchPaper[];
  note_runs?: WorkbenchNoteRun[];
  active_paper_id: string | null;
  messages: WorkbenchMessage[];
  exploration_runs?: WorkbenchExplorationRun[];
  history_error?: string | null;
  paper_panel_open: boolean;
  updated_at: string;
};

export type KnowledgeWorkbenchStatus = {
  knowledge_id: string;
  status: "available" | "preparing" | "failed" | "not_registered" | "unavailable";
  can_import: boolean;
  paper_id: string | null;
  title?: string | null;
  source_url?: string | null;
  pdf_status?: string | null;
  parse_status?: string | null;
};

export interface WorkbenchClient {
  listSessions(includeArchived?: boolean): Promise<WorkbenchSession[]>;
  createSession(workspaceId?: string | null): Promise<WorkbenchSession>;
  createQuestionSession?(question: string): Promise<WorkbenchSession>;
  /** Open a published note's paper, reusing an active paper session when one exists. */
  createKnowledgeSession?(knowledgeId: string, workspaceId?: string | null): Promise<WorkbenchSession>;
  /** Read whether a published note is registered, preparing, or importable. */
  getKnowledgePaperStatus?(knowledgeId: string): Promise<KnowledgeWorkbenchStatus>;
  /** Import a note's source through the shared paper pipeline and open it. */
  importKnowledgeSession?(knowledgeId: string, workspaceId?: string | null): Promise<WorkbenchSession>;
  getSession(sessionId: string): Promise<WorkbenchSession>;
  renameSession(sessionId: string, title: string): Promise<WorkbenchSession>;
  updateLayout(sessionId: string, paperPanelOpen: boolean, activePaperId?: string | null): Promise<WorkbenchSession>;
  archiveSession(sessionId: string): Promise<WorkbenchSession>;
  deleteSession(sessionId: string): Promise<void>;
  renameWorkspace(workspaceId: string, title: string): Promise<{ workspace_id: string; title: string }>;
  deleteWorkspace(workspaceId: string): Promise<void>;
  addFixturePaper?(sessionId: string, paperId: string): Promise<WorkbenchSession>;
  addFixtureMessage?(sessionId: string, text: string, context?: PaperContextRef): Promise<WorkbenchSession>;
  startFixtureNoteRun?(sessionId: string, paperId: string): Promise<WorkbenchSession>;
  publishNoteRun?(noteRunId: string): Promise<WorkbenchSession>;
  attachPrototypePaper?(sessionId: string, paper: { title: string; pdf_url: string; source_url?: string | null }): Promise<WorkbenchSession>;
  uploadPdf?(sessionId: string, file: File): Promise<WorkbenchSession>;
  addPaperUrl?(sessionId: string, url: string, title?: string | null): Promise<WorkbenchSession>;
  retryPaper?(sessionId: string, paperId: string): Promise<WorkbenchSession>;
  startFixtureExploration?(sessionId: string, question: string, profile?: WorkbenchExplorationProfile, interactionContext?: InteractionContextPayload): Promise<WorkbenchSession>;
  cancelFixtureExploration?(sessionId: string, runId: string): Promise<WorkbenchSession>;
  continueExploration?(sessionId: string, runId: string): Promise<WorkbenchSession>;
  fetchWorkspace?(sessionId: string): Promise<WorkbenchWorkspaceState | null>;
  fetchWorkspaceDocuments?(sessionId: string): Promise<WorkspaceDocument[]>;
  setWorkspaceFocus?(sessionId: string, questionId: string): Promise<WorkbenchWorkspaceState>;
  resolveWorkspaceDecision?(sessionId: string, decisionId: string, approved: boolean, decision?: string, questionId?: string): Promise<{ decision: WorkspaceDecisionPoint }>;
  sendTurn?(sessionId: string, text: string, options?: {
    action?: "translate" | "explain" | "ask_selection" | "paper" | "web" | "web_search" | "research" | "research_run" | "basic";
    context?: PaperContextRef;
    interactionContext?: InteractionContextPayload;
    profile?: WorkbenchExplorationProfile;
  }): Promise<WorkbenchSession>;
}

export interface WorkbenchChatClient {
  listMessages(sessionId: string): Promise<WorkbenchMessage[]>;
  sendFixedMessage(
    sessionId: string,
    query: string,
    context?: PaperContextRef,
  ): Promise<WorkbenchMessage>;
}

export type WorkspaceStatus =
  | "created"
  | "initial_research"
  | "waiting_for_user_action"
  | "investigating"
  | "archived";

export type WorkspaceSubquestion = {
  question_id: string;
  text: string;
  status: "suggested" | "open" | "resolved" | "deprioritized";
  answer?: string | null;
  researchability?: "candidate" | "boundary" | "unknown";
};

export type WorkspaceResearchMapNode = {
  node_id: string;
  label?: string;
  related_question_ids: string[];
  evidence_ids: string[];
};

export type WorkspaceEvidence = {
  evidence_id: string;
  source_id: string;
  block_ids: string[];
  supports_question_ids: string[];
  evidence_role: string;
  claim?: string;
  research_interpretation?: string;
  confidence?: string;
};

export type WorkspaceMethodMapEntry = {
  method_id: string;
  name: string;
  mechanism?: string;
  assumptions?: string;
  evidence_ids: string[];
  limitations?: string;
};

export type WorkspaceMethodMap = {
  map_id: string;
  status: "draft" | "approved" | "rejected";
  entries: WorkspaceMethodMapEntry[];
};

export type WorkspaceCandidateHypothesis = {
  hypothesis_id: string;
  text: string;
  question_id?: string | null;
  evidence_ids: string[];
  rationale?: string;
  falsifiers: string[];
  status: "candidate" | "selected" | "rejected";
};

export type WorkspaceCritique = {
  critique_id: string;
  hypothesis_id: string;
  strengths: string[];
  risks: string[];
  alternatives: string[];
  evidence_ids: string[];
  confidence?: string;
  status: "draft" | "approved" | "rejected";
};

export type WorkspaceExperimentPlan = {
  plan_id: string;
  hypothesis_id?: string | null;
  status: "draft" | "approved" | "rejected";
  intervention?: string;
  baselines: string[];
  datasets: string[];
  metrics: string[];
  ablations: string[];
  expected_outcomes?: string;
  decision_criteria?: string;
  resource_estimate?: string;
  risks: string[];
};

export type WorkspaceResearchIteration = {
  iteration_id: string;
  sequence: number;
  title: string;
  status: "in_progress" | "waiting_for_user" | "completed" | "abandoned";
  focus_question_id?: string | null;
  run_id?: string | null;
  summary?: string;
  evidence_ids: string[];
  candidate_question_ids: string[];
  decision?: string;
  next_step?: string;
};

export type WorkspaceResearchPlan = {
  stage: "not_started" | "method_mapping" | "hypothesis_review" | "experiment_planning" | "ready";
  /** Maturity of the user-facing artifact; distinct from the internal plan stage. */
  artifact_stage?: "overview" | "stage_note" | "report_draft" | "report";
  artifact?: {
    stage: "overview" | "stage_note" | "report_draft" | "report";
    label: string;
    description: string;
    can_generate_report: boolean;
    is_final: boolean;
  };
  iterations: WorkspaceResearchIteration[];
  method_map?: WorkspaceMethodMap | null;
  hypotheses: WorkspaceCandidateHypothesis[];
  critiques: WorkspaceCritique[];
  experiment_plans: WorkspaceExperimentPlan[];
};

export type WorkspaceDecisionPoint = {
  decision_id: string;
  kind: "research_direction" | "patch_approval";
  prompt?: string;
  status: "pending" | "approved" | "rejected";
  created_at?: string | null;
  decision?: string | null;
  candidate_question_ids?: string[];
};

export type WorkbenchWorkspaceState = {
  workspace_id: string;
  research_question: string;
  research_intent?: string | null;
  active_focus_id?: string | null;
  anchor_paper_id: string;
  status: WorkspaceStatus;
  workspace_revision: number;
  research_map: WorkspaceResearchMapNode[];
  subquestions: WorkspaceSubquestion[];
  evidence: WorkspaceEvidence[];
  research_plan?: WorkspaceResearchPlan;
  decision_points?: WorkspaceDecisionPoint[];
};

export type WorkspaceDocument = {
  document_id: "research-brief" | "current-progress" | "evidence-index" | "research-plan" | string;
  title: string;
  filename: string;
  markdown: string;
  path?: string;
  updated_at?: string;
};
