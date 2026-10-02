import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import type { CitationRef, InteractionContextPayload, PaperContextRef, SelectionPageSize, SelectionRect, WorkbenchClient, WorkbenchExplorationEvent, WorkbenchExplorationRun, WorkbenchMessage, WorkbenchNoteRun, WorkbenchSession, WorkbenchWorkspaceState, WorkspaceDecisionPoint, WorkspaceDocument } from "./types";
import { PdfViewer } from "./PdfViewer";
import { matchSelectionToBlocks, normalizeBlockBbox } from "./selectionMatching";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";

type Props = {
  client: WorkbenchClient;
  onOpenKnowledge?: (knowledgeId: string) => void;
  /** Session created by a cross-surface entry point such as a paper note. */
  initialSessionId?: string | null;
};

function cleanAssistantMessage(text: string): string {
  return text
    // This was an internal grounding marker, not useful answer content.
    .replace(/来源：?\s*\[论文证据\]\s*/g, "")
    .replace(/^\s*来源：?\s*$/gm, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function displayUserMessage(text: string): string {
  if (text.startsWith("请将当前选区翻译") || text.startsWith("请翻译当前选区")) return "翻译当前选区";
  if (text.startsWith("请解释当前选区")) return "解释当前选区";
  return text;
}

function removeUnresolvedCitationTokens(text: string, citations: CitationRef[]): string {
  return citations
    .filter((citation) => citation.status !== "resolved")
    .reduce((value, citation) => value.split(`[${citation.block_id}]`).join(""), text);
}

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message.trim() ? reason.message : fallback;
}

// Deduplicate citation anchors ([text:..] / [figure:..]) into compact superscript
// markers (`1`, `2`, …) and return the mapping (blockId → index) so a marker can
// be clicked to jump to the cited block in the paper.
export function tokenizeCitations(text: string): { markdown: string; refs: { blockId: string; label: string }[] } {
  const refs: { blockId: string; label: string }[] = [];
  const seen = new Map<string, number>();
  // A citation token is either a typed block anchor (text:/figure:, or the
  // assistant's full normalized:<paper>:text:<block> id) or a bare hex id the
  // model wrote by abbreviating one. Everything else inside brackets ([16],
  // [et al.], [论文证据]…) is ordinary text and stays untouched. Models also
  // merge several ids into one bracket pair ("[a, b]"), so each bracket is
  // split on separators and every valid part becomes its own marker.
  const isPrefixed = (part: string) => /^(?:text|figure|formula|table):[^\s\]]+$/.test(part);
  const isNormalized = (part: string) => /^normalized:[^:\s\]]+:(?:text|figure|formula|table):[^\s\]]+$/.test(part);
  const isBareHex = (part: string) => /^[0-9a-fA-F]{8,64}$/.test(part);
  const isCitationToken = (part: string) => isPrefixed(part) || isNormalized(part) || isBareHex(part);
  const markerFor = (part: string): string => {
    let idx = seen.get(part);
    if (idx === undefined) {
      idx = refs.length + 1;
      seen.set(part, idx);
      refs.push({ blockId: part, label: String(idx) });
    }
    return `[${idx}](#cite-${idx})`;
  };
  const citationGroupPattern = /[\[［(（]([^\[\]［］()（）]+)[\]］)）]/g;
  let markdown = text.replace(citationGroupPattern, (bracket: string, inner: string) => {
    const parts = inner.split(/[\s,，;；、]+/).map((part) => part.trim()).filter(Boolean);
    if (!parts.length) return bracket;
    const tokens = parts.map((part) => part.replace(/^`|`$/g, ""));
    const validTokens = tokens.filter(isCitationToken);
    if (validTokens.length !== parts.length) return bracket; // mixed real text -> leave the bracket as-is
    return validTokens.map(markerFor).join("");
  });
  // Some providers emit a single managed id without a citation wrapper, or
  // put Markdown backticks around it inside a prose sentence. Convert only
  // exact managed-id shapes; ordinary words and bibliography numbers remain
  // untouched.
  const standaloneManaged = /(?<![A-Za-z0-9_])`?((?:normalized:[^:\s\[\]\(\)（）,，、;；]+:(?:text|figure|formula|table):[^\s\[\]\(\)（）,，、;；`]+|(?:text|figure|formula|table):[^\s\[\]\(\)（）,，、;；`]+))`?(?![A-Za-z0-9_])/g;
  markdown = markdown.replace(standaloneManaged, (_match, token: string) => markerFor(token));
  return { markdown, refs };
}

/** The draft ref count is a unique citation-location count, not persisted evidence rows. */
export function citationSummaryLabel(count: number): string {
  return `引用定位 ${count} 处`;
}

/**
 * Remove the model's optional citation-audit appendix from user-facing draft
 * prose.  Stable ids are already preserved in the structured citation refs
 * returned by tokenizeCitations; exposing this appendix only leaks an
 * implementation detail and makes the answer much harder to read.
 */
export function stripCitationAuditAppendix(text: string): string {
  let cleaned = text;
  cleaned = cleaned.replace(
    /<details\b[^>]*>\s*<summary>\s*引用映射[\s\S]*?(?:<\/details>|$)/i,
    "",
  );
  // A provider may emit the same appendix as a Markdown heading or plain
  // text when it does not preserve HTML.  It is always an end-of-draft
  // section, so remove only the trailing section rather than matching prose
  // that happens to mention the words "引用映射".
  cleaned = cleaned.replace(
    /(?:^|\n)\s*(?:#{1,6}\s*)?引用映射(?:\s*[（(][^\n]*[）)])?[\s\S]*$/i,
    "",
  );
  return cleaned.replace(/\n{3,}/g, "\n\n").trim();
}

type DraftSection = { heading: string; body: string };
type OrganizedDraft = {
  conclusion: DraftSection[];
  evidence: DraftSection[];
  uncertainty: DraftSection[];
  next: DraftSection[];
  analysis: DraftSection[];
  internal: DraftSection[];
  intro: string;
  structured: boolean;
};

/**
 * Project the model's long exploration draft into a small user-facing result.
 * The model is still free to write rich Markdown, but the main conversation
 * should foreground the answer and keep audit/protocol material secondary.
 * This is intentionally a presentation-only transform: persisted evidence and
 * the original draft remain unchanged.
 */
export function organizeExplorationDraft(markdown: string): OrganizedDraft {
  const lines = markdown.replace(/\r\n/g, "\n").split("\n");
  const sections: DraftSection[] = [];
  let introLines: string[] = [];
  let current: DraftSection | null = null;
  const headingPattern = /^\s{0,3}#{1,4}\s+(.+?)\s*#*\s*$/;
  for (const line of lines) {
    const match = line.match(headingPattern);
    if (match) {
      if (current) sections.push({ ...current, body: current.body.trim() });
      current = { heading: match[1].trim(), body: "" };
      continue;
    }
    if (current) current.body += `${line}\n`;
    else introLines.push(line);
  }
  if (current) sections.push({ ...current, body: current.body.trim() });
  const intro = introLines.join("\n").trim();
  const cleanHeading = (heading: string) => heading
    .replace(/^\s*(?:第\s*)?\d+[.、)]\s*/, "")
    .replace(/^\s*[一二三四五六七八九十]+[、.]\s*/, "")
    .trim();
  const classify = (heading: string): keyof Pick<OrganizedDraft, "conclusion" | "evidence" | "uncertainty" | "next" | "analysis" | "internal"> => {
    const value = cleanHeading(heading).toLowerCase();
    if (/引用映射|stable id|完整清单|待精读候选|工具调用|运行过程|审计/.test(value)) return "internal";
    if (/不能|无法|待核验|开放问题|限制|不足|未能|未验证|证据边界|证据范围|不能证明|机制外推|外推|假设|推断/.test(value)) return "uncertainty";
    if (/结论|核心回答|结论先行|一句话|总结/.test(value)) return "conclusion";
    if (/证据|事实|已确认|能确认|论文关于|当前上下文|论文已|正文|依据/.test(value)) return "evidence";
    if (/下一步|验证|实验|研究方案|可继续|建议|分析计划|预期产出|如何检验|执行方案/.test(value)) return "next";
    return "analysis";
  };
  const result: OrganizedDraft = {
    conclusion: [], evidence: [], uncertainty: [], next: [], analysis: [], internal: [], intro,
    structured: sections.length >= 2 && sections.some((section) => classify(section.heading) !== "analysis"),
  };
  for (const section of sections) {
    if (!section.body) continue;
    result[classify(section.heading)].push(section);
  }
  if (result.structured && intro) result.conclusion.unshift({ heading: "阶段结论", body: intro });
  if (!result.conclusion.length && sections.length === 1) result.conclusion.push(sections[0]);
  return result;
}

function explorationEventLabel(eventType: string): string {
  const labels: Record<string, string> = {
    run_started: "开始",
    continuation_started: "继续",
    phase_changed: "阶段",
    tool_started: "调用",
    tool_completed: "完成",
    source_discovered: "发现",
    context_read: "读取",
    file_written: "写入",
    plan_updated: "计划",
    budget_updated: "预算",
    awaiting_decision: "等待确认",
    operation_started: "开始操作",
    operation_completed: "完成操作",
    final_draft: "草稿",
    run_cancelled: "停止",
    run_failed: "失败",
  };
  return labels[eventType] ?? eventType;
}

const TERMINAL_EXPLORATION_STATES = [
  "completed", "awaiting_user", "failed", "cancelled", "budget_exhausted",
  "retryable_failure", "terminal_failure", "abandoned", "awaiting_user_decision",
];

// One card per run_id: the conversation merges the session's exploration runs
// by stable run identity so a continuation never renders a second card.
function dedupeRunsByRunId(runs: WorkbenchExplorationRun[]): WorkbenchExplorationRun[] {
  const byRunId = new Map<string, WorkbenchExplorationRun>();
  for (const run of runs) byRunId.set(run.run_id, run);
  return [...byRunId.values()];
}

function attemptStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    queued: "排队中",
    running: "运行中",
    completed: "已完成",
    awaiting_user: "等待用户输入",
    awaiting_user_decision: "等待你确认",
    budget_exhausted: "预算耗尽",
    cancelled: "已停止",
    retryable_failure: "可重试失败",
    terminal_failure: "失败",
    abandoned: "执行中断（可继续）",
    failed: "失败",
  };
  return labels[status] ?? status;
}

// The research-assistant agent no longer needs the selection folded into the
    // question string: the Scope Resolver receives the live interaction context
// (active paper + selection) and injects the evidence as a privileged leading
// system message at run time, so the stored question (and its echo) stays clean.
function buildInteractionContext(session: WorkbenchSession, selection: PaperContextRef | null, workspace?: WorkbenchWorkspaceState | null): InteractionContextPayload {
  const activePaper = session?.papers?.find((paper) => paper.paper_id === session.active_paper_id) ?? null;
  const surface = activePaper || selection ? "paper_reader" : "global_chat";
  return {
    surface,
    canonical_paper_id: activePaper?.paper_id ?? null,
    research_question_id: workspace?.active_focus_id ?? null,
    project_id: workspace?.workspace_id ?? session.workspace_id ?? null,
    selection: selection?.block_id
      ? {
          paper_id: selection.paper_id,
          block_id: selection.block_id,
          block_ids: selection.block_ids ?? [selection.block_id],
          text: selection.text ?? "",
          page: selection.page ?? null,
          section_path: selection.section_path ?? [],
          rects: selection.rects ?? [],
          page_size: selection.page_size,
        }
      : null,
  };
}

// Exploration reads as part of the conversation: the research question echoes
// as a user bubble and the run itself streams as one assistant bubble.
export function continuationActionLabel(run: Pick<WorkbenchExplorationRun, "status" | "continuation_label">): string {
  return run.status === "cancelled"
    ? "重新开始探索"
    : (run.continuation_label || "基于已有结果继续执行");
}

export function retrievalPlanLabel(plan: WorkbenchExplorationRun["retrieval_plan"]): string | null {
  const labels: Record<string, string> = {
    direct: "直接回答",
    web_lookup: "网页查询",
    paper_local: "当前论文",
    paper_evidence: "论文证据",
    research_exploration: "论文调研",
  };
  return labels[plan ?? ""] ?? null;
}

type TracePhase = {
  key: "understand" | "plan" | "retrieve" | "read" | "synthesize";
  label: string;
  description: string;
  state: "pending" | "active" | "completed" | "waiting" | "failed";
};

const TRACE_PHASE_DEFINITIONS: Array<Pick<TracePhase, "key" | "label" | "description">> = [
  { key: "understand", label: "理解问题", description: "确认问题范围与上下文" },
  { key: "plan", label: "制定计划", description: "选择检索路径与证据策略" },
  { key: "retrieve", label: "检索来源", description: "查找相关论文或网页来源" },
  { key: "read", label: "读取证据", description: "读取论文区块并核对定位" },
  { key: "synthesize", label: "整理结论", description: "汇总证据并生成阶段结果" },
];

function tracePhaseIndex(event: WorkbenchExplorationEvent): number {
  const type = event.event_type;
  const summary = event.summary.toLowerCase();
  const tool = (event.stable_ids.tool_name ?? "").toLowerCase();
  if (type === "run_started" || type === "continuation_started") return 0;
  if (type === "source_discovered" || tool.includes("search") || /检索|搜索|来源|候选/.test(summary)) return 2;
  if (type === "context_read" || tool.includes("read_managed") || tool.includes("paper_metadata") || /读取|证据块|正文|论文材料/.test(summary)) return 3;
  if (type === "final_draft" || type === "file_written" || type === "plan_updated" || /总结|草稿|结论|写入|整理/.test(summary)) return 4;
  if (type === "awaiting_decision" || /等待.*确认|研究需要用户/.test(summary)) return 4;
  if (type === "phase_changed") {
    if (/规划|计划|问题|范围/.test(summary)) return 1;
    if (/检索|搜索|来源/.test(summary)) return 2;
    if (/读取|证据|论文/.test(summary)) return 3;
    if (/总结|草稿|结论|写入/.test(summary)) return 4;
  }
  return 1;
}

function tracePhases(run: WorkbenchExplorationRun): TracePhase[] {
  const events = run.events ?? [];
  const maxIndex = events.reduce((max, event) => Math.max(max, tracePhaseIndex(event)), -1);
  const live = !TERMINAL_EXPLORATION_STATES.includes(run.status);
  const waiting = run.status === "awaiting_user" || run.status === "awaiting_user_decision";
  const failed = ["failed", "retryable_failure", "terminal_failure", "abandoned", "budget_exhausted", "cancelled"].includes(run.status);
  const activeIndex = waiting || failed ? Math.max(maxIndex, 0) : Math.max(maxIndex, 0);
  return TRACE_PHASE_DEFINITIONS.map((definition, index) => {
    let state: TracePhase["state"] = "pending";
    if (index < maxIndex || run.status === "completed") state = "completed";
    else if (index === activeIndex && waiting) state = "waiting";
    else if (index === activeIndex && failed) state = "failed";
    else if (index === activeIndex && live) state = "active";
    return { ...definition, state };
  });
}

function traceToolLabel(toolName: string): string {
  const labels: Record<string, string> = {
    search_web: "网页搜索",
    search_arxiv: "arXiv 检索",
    search_sources: "受管来源检索",
    read_paper_metadata: "读取论文元数据",
    read_managed_blocks: "读取证据块",
    update_subquestions: "更新子问题",
    update_research_map: "更新研究地图",
    add_evidence: "写入证据",
    write_file: "写入研究文档",
  };
  return labels[toolName] ?? toolName;
}

function traceToolStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    succeeded: "成功",
    rejected: "参数被拒绝",
    retryable_failure: "可重试失败",
    effect_unknown: "结果未知",
    cancelled: "已取消",
    failed: "失败",
  };
  return labels[status] ?? status;
}

function traceEventDescription(event: WorkbenchExplorationEvent): string {
  const toolName = event.stable_ids.tool_name;
  if (toolName) {
    const tool = traceToolLabel(toolName);
    if (event.event_type === "tool_started") return `${tool} · 已接收调用`;
    if (event.event_type === "tool_completed") {
      const failed = /失败|错误|拒绝|停止|跳过|上限/.test(event.summary);
      return `${tool} · ${failed ? "未完成" : "已返回结果"}`;
    }
    return tool;
  }
  if (event.event_type === "source_discovered" && event.stable_ids.title) {
    const title = event.stable_ids.title.trim();
    return `发现来源：${title.length > 48 ? `${title.slice(0, 48)}…` : title}`;
  }
  if (event.event_type === "context_read") return "读取证据块";
  return event.summary;
}

function formatTraceDuration(run: WorkbenchExplorationRun): string | null {
  const attempt = run.current_attempt;
  const started = attempt?.started_at ?? run.created_at;
  if (!started) return null;
  const end = attempt?.finished_at ?? (TERMINAL_EXPLORATION_STATES.includes(run.status) ? undefined : new Date().toISOString());
  const startMs = Date.parse(started);
  const endMs = end ? Date.parse(end) : Number.NaN;
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs < startMs) return null;
  const seconds = Math.max(0, Math.round((endMs - startMs) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} 分 ${seconds % 60} 秒`;
}

function ExplorationBubble({
  run,
  onCancel,
  onContinue,
  onAddCandidate,
  onOpenBlock,
  pendingDecisions = [],
  candidateQuestions = [],
  onResolveDecision,
}: {
  run: WorkbenchExplorationRun;
  onCancel: (runId: string) => void;
  onContinue: (runId: string) => void;
  onAddCandidate: (source: { title: string; url?: string }) => void;
  onOpenBlock?: (blockId: string) => void;
  pendingDecisions?: WorkspaceDecisionPoint[];
  candidateQuestions?: WorkbenchWorkspaceState["subquestions"];
  onResolveDecision?: (decisionId: string, approved: boolean, questionId?: string) => void;
}) {
  const live = !TERMINAL_EXPLORATION_STATES.includes(run.status);
  const events = run.events ?? [];
  const cleanedDraft = run.partial_result ? stripCitationAuditAppendix(run.partial_result) : "";
  const draft = cleanedDraft ? tokenizeCitations(cleanedDraft) : null;
  const CitationLink = (props: { href?: string; children?: ReactNode }) => {
    const href = String(props.href ?? "").replace(/^#/, "");
    const match = href.match(/^cite-(\d+)$/);
    if (match) {
      const ref = draft?.refs?.[Number(match[1]) - 1];
      return (
        <span className="workbench-draft-cite" title={ref?.blockId ?? ""} onClick={() => ref && onOpenBlock?.(ref.blockId)}>
          {ref?.label}
        </span>
      );
    }
    return <a href={props.href}>{props.children}</a>;
  };
  const organizedDraft = draft ? organizeExplorationDraft(draft.markdown) : null;
  const renderMarkdown = (content: string) => (
    <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={{ a: CitationLink }}>
      {content}
    </ReactMarkdown>
  );
  const renderDraftGroup = (label: string, sections: DraftSection[], className: string) => {
    if (!sections.length) return null;
    return (
      <section className={`workbench-draft-section ${className}`} aria-label={label}>
        <h3>{label}</h3>
        <div className="workbench-draft-section-content">
          {sections.map((section, index) => (
            <div className="workbench-draft-subsection" key={`${section.heading}-${index}`}>
              {sections.length > 1 && <h4>{section.heading}</h4>}
              {renderMarkdown(section.body)}
            </div>
          ))}
        </div>
      </section>
    );
  };
  const renderOrganizedDraft = () => {
    if (!organizedDraft) return null;
    if (!organizedDraft.structured) return renderMarkdown(draft?.markdown ?? "");
    // Internal audit blocks (stable-id maps and candidate IDs) are deliberately
    // omitted from the readable answer. Candidates are surfaced by the
    // dedicated "相关论文" overview section instead.
    const analysis = organizedDraft.analysis;
    return (
      <div className="workbench-exploration-result" aria-label="研究阶段结果">
        {renderDraftGroup("阶段结论", organizedDraft.conclusion, "workbench-draft-conclusion")}
        {renderDraftGroup("当前证据", organizedDraft.evidence, "workbench-draft-evidence")}
        {renderDraftGroup("尚不能确定", organizedDraft.uncertainty, "workbench-draft-uncertainty")}
        {renderDraftGroup("下一步", organizedDraft.next, "workbench-draft-next")}
        {analysis.length > 0 && (
          <details className="workbench-draft-analysis">
            <summary>展开完整分析</summary>
            <div className="workbench-draft-analysis-body">
              {analysis.map((section, index) => (
                <div className="workbench-draft-subsection" key={`${section.heading}-${index}`}>
                  <h4>{section.heading}</h4>
                  {renderMarkdown(section.body)}
                </div>
              ))}
            </div>
          </details>
        )}
      </div>
    );
  };
  const visibleTools = run.tools_used.filter((tool) => tool.name !== "read_run_status");
  const summaryBits = [
    run.application_budget_mode === "experimental_unbounded"
      ? "测试模式 · 应用预算不设上限（受时间/模型限制）"
      : `${run.budget_used.tool_calls}/${run.budget_used.tool_limit} 工具`,
    ...(run.sources.length ? [`${run.sources.length} 条外部来源`] : []),
    `${run.token_usage.input_tokens} 入 / ${run.token_usage.output_tokens} 出 token`,
    ...((run.web_search_usage?.calls ?? 0) > 0
      ? [`网页搜索 ${run.web_search_usage?.calls ?? 0} 次 · ${run.web_search_usage?.total_tokens ?? 0} token`]
      : []),
  ];
  const statusLabel = attemptStatusLabel(run.status);
  const attemptHistory = run.attempt_history ?? [];
  const failedTools = (run.tool_details ?? []).filter((tool) => tool.status !== "succeeded");
  // A continuation is a NEW attempt under the same run based on persisted
  // results — never a claimed lossless checkpoint resume.
  const isContinuation = run.attempt > 1;
  const continuationState = run.continuation_label || "基于已有结果继续执行";
  const retrievalLabel = retrievalPlanLabel(run.retrieval_plan);

  const attemptHistoryRow = attemptHistory.length > 0 && (
    <details className="workbench-attempt-history" aria-label="Attempt 历史">
      <summary>Attempt 历史（{attemptHistory.length}）</summary>
      <ul>
        {attemptHistory.map((attempt) => (
          <li key={attempt.attempt_id} className={`workbench-attempt-item ${attempt.status}`}>
            <span className="workbench-attempt-item-title">Attempt {attempt.attempt_no}</span>
            <span>{attemptStatusLabel(attempt.status)}</span>
            <span>{attempt.budget_used?.tool_calls ?? 0}/{attempt.budgets?.tool_calls ?? 16} 工具</span>
            {attempt.failure?.safe_message && <em className="workbench-attempt-item-failure">{attempt.failure.safe_message}</em>}
          </li>
        ))}
      </ul>
    </details>
  );

  const toolFailureDetails = failedTools.length > 0 && (
    <details className="workbench-tool-failures" aria-label="工具失败详情">
      <summary>工具失败（{failedTools.length}）</summary>
      <ul>
        {failedTools.map((tool) => (
          <li key={tool.tool_call_id}>
            <strong>{tool.tool_name}</strong>
            <span>类别：{tool.error_code || tool.status}</span>
            <span>可重试性：{tool.retryability}</span>
            <span>副作用：{tool.side_effect_state}</span>
            {tool.safe_message && <span>{tool.safe_message}</span>}
            {tool.diagnostic_id && <code>诊断 ID：{tool.diagnostic_id}</code>}
            <span>建议动作：{tool.recommended_action}</span>
          </li>
        ))}
      </ul>
    </details>
  );

  const stream = (
    <div className="workbench-exploration-events" aria-label="探索事件流">
      {events.length ? (
        events.map((event) => (
          <p className="workbench-exploration-event" key={`${event.attempt_id ?? run.current_attempt_id ?? "legacy"}-${event.sequence_no}`}>
            <span className="workbench-exploration-event-type">{explorationEventLabel(event.event_type)}</span>
            <span>{event.summary}</span>
            {event.stable_ids.tool_name && <code>{event.stable_ids.tool_name}</code>}
            {event.stable_ids.title && <em>{event.stable_ids.title}</em>}
          </p>
        ))
      ) : (
        <p className="workbench-exploration-event muted">{run.status === "queued" ? "探索已排队，等待执行…" : "探索已启动，等待事件流…"}</p>
      )}
    </div>
  );

  const sourcesRow = run.sources.length > 0 ? (
    <div className="workbench-exploration-sources">
      外部来源：
      {run.sources.map((source, index) => (
        <span key={`${source.source_id}-${index}`} className="workbench-source-item">
          {index > 0 && "、"}
          {source.title}
          {source.relevance ? ` · 相关度 ${source.relevance}` : ""}
          {source.kind === "external_candidate" && source.url && (
            <button className="workbench-add-candidate" type="button" onClick={() => onAddCandidate(source)}>＋添加</button>
          )}
        </span>
      ))}
    </div>
  ) : null;

  const decisionPrompt = pendingDecisions[0];
  const researchSuggestion = decisionPrompt && onResolveDecision ? (
    <section className="workbench-research-suggestion" aria-label="研究建议">
      <div className="workbench-research-suggestion-heading">
        <span className="workbench-research-suggestion-label">研究建议</span>
        <span className="workbench-research-suggestion-hint">可选，不影响当前阅读</span>
      </div>
      <p>{decisionPrompt.prompt || "已形成一个可继续深入的研究方向。"}</p>
      {(decisionPrompt.candidate_question_ids ?? []).length > 0 && (
        <div className="workbench-research-suggestion-options" aria-label="候选研究线索">
          {(decisionPrompt.candidate_question_ids ?? []).map((questionId) => {
            const question = candidateQuestions.find((item) => item.question_id === questionId);
            if (!question) return null;
            return (
              <button className="workbench-focus-option" type="button" key={questionId} onClick={() => onResolveDecision(decisionPrompt.decision_id, true, questionId)}>
                <span>设为当前焦点</span>
                <strong>{question.text}</strong>
              </button>
            );
          })}
        </div>
      )}
      <div className="workbench-research-suggestion-actions">
        <button className="workbench-secondary" type="button" onClick={() => onResolveDecision(decisionPrompt.decision_id, true, decisionPrompt.candidate_question_ids?.[0])}>设为第一个方向</button>
        <button className="workbench-secondary" type="button" onClick={() => onResolveDecision(decisionPrompt.decision_id, false)}>忽略</button>
      </div>
    </section>
  ) : null;

  return (
    <>
      <article className="workbench-message user" aria-label={`探索问题：${run.question}`}>
        <span>你</span>
        <p>{run.question}</p>
      </article>
      <article className={`workbench-message assistant workbench-exploration-bubble ${run.status}`} aria-label={`探索运行：${run.question}`}>
        <span>{run.profile === "assistant" ? "研究助理" : "文献探索"} · Research Pulse</span>
        {live ? (
          <>
            <div className="workbench-exploration-progress" role="status">
              <span className="workbench-exploration-progress-dot" aria-hidden="true" />
              <span>{run.phase || "正在处理"}</span>
            </div>
            <div className="workbench-exploration-meta">
              <span className="workbench-exploration-status" role="status">{statusLabel} · {run.phase}</span>
              {retrievalLabel && <span className="workbench-retrieval-plan">回答模式：{retrievalLabel}</span>}
              <span>{summaryBits.join(" · ")}</span>
              {isContinuation && <span className="workbench-continuation-state">{continuationState}</span>}
              <button onClick={() => onCancel(run.run_id)}>停止探索</button>
            </div>
            <details className="workbench-exploration-details">
              <summary>查看运行详情</summary>
              {stream}
              {attemptHistoryRow}
              {toolFailureDetails}
            </details>
          </>
        ) : (
          <details className="workbench-exploration-collapsed">
            <summary>
              <span className="workbench-exploration-status">{statusLabel}</span>
              <span className="workbench-exploration-summary-bits">{summaryBits.join(" · ")}</span>
              <span className="workbench-exploration-summary-toggle">查看过程</span>
            </summary>
            <div className="workbench-exploration-detail">
              <div className="workbench-exploration-meta">
                <span>Attempt {run.attempt}</span>
                {retrievalLabel && <span className="workbench-retrieval-plan">回答模式：{retrievalLabel}</span>}
                {isContinuation && <span className="workbench-continuation-state">{continuationState}</span>}
                {run.phase && <span>{run.phase}</span>}
                {visibleTools.length > 0 && <span>{visibleTools.map((tool) => `${tool.name}×${tool.calls}`).join("、")}</span>}
              </div>
              {stream}
              {sourcesRow}
              {attemptHistoryRow}
              {toolFailureDetails}
            </div>
          </details>
        )}
        {draft && (
          <blockquote className="workbench-exploration-draft" aria-label={organizedDraft?.structured ? "研究阶段结果" : "探索草稿"}>
            <div className="workbench-exploration-draft-header">
              <span className="workbench-exploration-draft-tag">{organizedDraft?.structured ? "阶段结果 · 待核验" : "初步结论 · 待核验"}</span>
              {draft.refs.length > 0 && <span className="workbench-exploration-draft-evidence">{citationSummaryLabel(draft.refs.length)}</span>}
            </div>
            <div className="workbench-exploration-draft-body">
              {renderOrganizedDraft()}
            </div>
          </blockquote>
        )}
        {researchSuggestion}
        {run.failure?.safe_message && (
          <p className="workbench-error" role="alert">运行未生成可显示结果：{run.failure.safe_message}</p>
        )}
        {(["cancelled", "retryable_failure", "abandoned"].includes(run.status)
          || run.failure?.recommended_action === "continue") && (
          <div className="workbench-exploration-meta">
            <button onClick={() => onContinue(run.run_id)}>{continuationActionLabel(run)}</button>
          </div>
        )}
      </article>
    </>
  );
}

type ConversationItem =
  | { kind: "message"; message: WorkbenchMessage }
  | { kind: "exploration"; run: WorkbenchExplorationRun }
  | { kind: "note"; run: WorkbenchNoteRun };

function noteRunStageLabel(stage: string, status: WorkbenchNoteRun["status"]): string {
  if (status === "queued") return "排队中";
  if (status === "generating") {
    if (stage === "reading_material") return "读取论文材料";
    if (stage === "generating_note") return "生成并校验笔记";
    return "正在处理";
  }
  if (status === "awaiting_approval") return "草稿待确认";
  if (status === "published") return "已发布";
  return "生成失败";
}

function NoteRunBubble({
  run,
  paperTitle,
  onPublish,
  onRetry,
  onOpenKnowledge,
}: {
  run: WorkbenchNoteRun;
  paperTitle: string;
  onPublish: (runId: string) => void;
  onRetry: (paperId: string) => void;
  onOpenKnowledge?: (knowledgeId: string) => void;
}) {
  const live = run.status === "queued" || run.status === "generating";
  const statusLabel = noteRunStageLabel(run.stage, run.status);
  const preparationStep = run.status === "queued" || run.stage === "reading_material"
    ? "active"
    : "done";
  const generationStep = run.status === "generating" && run.stage === "generating_note"
    ? "active"
    : ["awaiting_approval", "published"].includes(run.status)
      ? "done"
      : run.status === "failed"
        ? "failed"
        : "";
  const publicationStep = run.status === "awaiting_approval"
    ? "active"
    : run.status === "published"
      ? "done"
      : "";
  return (
    <article className={`workbench-message assistant workbench-note-run-bubble ${run.status}`} aria-label={`论文笔记运行：${paperTitle}`}>
      <span>论文笔记 · Research Pulse</span>
      <strong className="workbench-note-run-title">{paperTitle}</strong>
      <div className="workbench-note-run-progress" role="status" aria-live="polite">
        <i aria-hidden="true" />
        <span>{statusLabel}</span>
      </div>
      <ol className="workbench-note-run-steps" aria-label="论文笔记生成步骤">
        <li className={preparationStep}>准备论文材料</li>
        <li className={generationStep}>生成结构化笔记</li>
        <li className={publicationStep}>等待发布确认</li>
      </ol>
      {run.status === "awaiting_approval" && (
        <div className="workbench-note-run-approval">
          <p>{run.safe_error ? `上次发布未完成：${run.safe_error}` : "草稿已完成，尚未进入知识库。确认后才会发布。"}</p>
          {run.draft_preview && (
            <details className="workbench-note-draft-preview">
              <summary>预览论文笔记草稿</summary>
              <div className="workbench-note-draft-preview-body">
                <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]}>
                  {run.draft_preview}
                </ReactMarkdown>
              </div>
            </details>
          )}
          <button type="button" onClick={() => onPublish(run.note_run_id)}>确认发布</button>
        </div>
      )}
      {run.status === "published" && (
        <div className="workbench-note-run-approval">
          <p>论文笔记已发布，可从知识库继续阅读。</p>
          {run.knowledge_id && onOpenKnowledge && <button type="button" onClick={() => onOpenKnowledge(run.knowledge_id!)}>打开论文笔记</button>}
        </div>
      )}
      {run.status === "failed" && (
        <div className="workbench-note-run-approval">
          <p role="alert">{run.safe_error || "论文笔记生成失败"}</p>
          <button type="button" onClick={() => onRetry(run.paper_id)}>重新生成</button>
        </div>
      )}
      {run.events.length > 0 && (
        <details className="workbench-note-run-events" open={live}>
          <summary>查看生成过程</summary>
          <ul>{run.events.map((event) => <li key={event.sequence_no}>{event.summary}</li>)}</ul>
          {run.tool_uses?.length ? <small>调用：{run.tool_uses.map((tool) => `${tool.name}×${tool.calls}`).join("、")}</small> : null}
        </details>
      )}
    </article>
  );
}

type PendingEvidenceAction = {
  action: "translate" | "explain";
  prompt: string;
};

type PendingSelectionQuestion = {
  text: string;
};

type AnswerMode = "auto" | "basic" | "web_search" | "paper" | "research_run";

const ANSWER_MODE_OPTIONS: Array<{ value: AnswerMode; label: string; hint: string }> = [
  { value: "auto", label: "自动选择", hint: "由研究助理判断回答路径" },
  { value: "basic", label: "普通回答", hint: "使用常识和当前对话直接回答" },
  { value: "web_search", label: "网页搜索", hint: "检索网页上的最新信息" },
  { value: "paper", label: "当前论文", hint: "只基于当前论文证据回答" },
  { value: "research_run", label: "深度研究", hint: "拆解问题并形成研究产物" },
];

function answerModeLabel(mode: AnswerMode): string {
  return ANSWER_MODE_OPTIONS.find((option) => option.value === mode)?.label ?? "自动选择";
}

function WorkspaceDocumentView({
  document,
  documents,
  workspace,
  onBack,
  onSelect,
  onSetFocus,
}: {
  document: WorkspaceDocument;
  documents: WorkspaceDocument[];
  workspace: WorkbenchWorkspaceState | null;
  onBack: () => void;
  onSelect: (documentId: string) => void;
  onSetFocus: (questionId: string) => void;
}) {
  const candidates = workspace?.subquestions.filter((question) => question.researchability === "candidate" && question.status !== "deprioritized") ?? [];
  return (
    <section className="workbench-document-view" aria-label={`研究文档：${document.title}`}>
      <header className="workbench-document-toolbar">
        <button type="button" className="workbench-document-back" onClick={onBack}>← 返回会话</button>
        <div className="workbench-document-file-label">
          <span className="workbench-kicker">Workspace 文档</span>
          <strong>{document.title}</strong>
          <small>{document.filename}</small>
        </div>
      </header>
      <nav className="workbench-document-tabs" aria-label="研究文档列表">
        {documents.map((item) => (
          <button
            type="button"
            key={item.document_id}
            className={item.document_id === document.document_id ? "active" : ""}
            onClick={() => onSelect(item.document_id)}
          >
            {item.title}
          </button>
        ))}
      </nav>
      {document.document_id === "current-progress" && candidates.length > 0 && (
        <section className="workbench-document-focus-list" aria-label="候选研究线索">
          <div>
            <strong>候选研究线索</strong>
            <span>选择一个方向作为当前研究焦点，再回到会话开始核查。</span>
          </div>
          {candidates.map((question) => (
            <button
              type="button"
              key={question.question_id}
              className={question.question_id === workspace?.active_focus_id ? "active" : ""}
              aria-pressed={question.question_id === workspace?.active_focus_id}
              onClick={() => onSetFocus(question.question_id)}
            >
              <span>{question.question_id === workspace?.active_focus_id ? "当前焦点" : "设为当前焦点"}</span>
              <strong>{question.text}</strong>
            </button>
          ))}
        </section>
      )}
      <article className="workbench-document-content">
        <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]}>
          {document.markdown}
        </ReactMarkdown>
      </article>
    </section>
  );
}

const WORKSPACE_STATUS_LABELS: Record<WorkbenchWorkspaceState["status"], string> = {
  created: "刚开始",
  initial_research: "初步研究中",
  waiting_for_user_action: "等待确认",
  investigating: "深入研究中",
  archived: "已归档",
};

function OverviewDisclosure({
  label,
  count,
  openByDefault = false,
  children,
}: {
  label: string;
  count?: string;
  openByDefault?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(openByDefault);

  // A newly-created decision point is actionable. Open the candidate list for
  // that transition, while preserving any manual collapse afterwards.
  useEffect(() => {
    if (openByDefault) setOpen(true);
  }, [openByDefault]);

  return (
    <details className="workbench-overview-disclosure" role="region" aria-label={label} open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>
        <span>{label}</span>
        {count && <small>{count}</small>}
      </summary>
      <div className="workbench-overview-disclosure-body">{children}</div>
    </details>
  );
}

function WorkspaceOverviewRail({
  workspace,
  documents,
  papers,
  explorationRuns,
  onOpenKnowledge,
  onOpenDocument,
  onOpenPaper,
  onAddPaperCandidate,
  onSetFocus,
  onContinueFocus,
  onReload,
  focusBusy = false,
}: {
  workspace?: WorkbenchWorkspaceState | null;
  documents: WorkspaceDocument[];
  papers: WorkbenchSession["papers"];
  explorationRuns: WorkbenchExplorationRun[];
  onOpenKnowledge?: (knowledgeId: string) => void;
  onOpenDocument: (documentId: string) => void;
  onOpenPaper: (paperId: string) => void;
  onAddPaperCandidate?: (candidate: { title: string; url?: string }) => void;
  onSetFocus: (questionId: string) => void;
  onContinueFocus: (questionId: string) => void;
  onReload?: () => void;
  focusBusy?: boolean;
}) {
  if (!workspace) {
    return (
      <aside className="workbench-overview-rail workbench-overview-unavailable" aria-label="研究概览" role="region">
        <header className="workbench-overview-head">
          <div><span className="workbench-kicker">Research workspace</span><h3>研究概览</h3><p className="workbench-overview-subtitle">研究设定会一直留在这里，不随会话滚动消失。</p></div>
        </header>
        <div className="workbench-overview-placeholder">
          <strong>正在加载研究设定</strong>
          <p>研究议题、当前焦点和证据将在这里显示。</p>
          <button type="button" className="workbench-overview-open" onClick={() => onReload?.()}>重新加载</button>
        </div>
      </aside>
    );
  }
  const activeQuestion = workspace.subquestions.find((question) => question.question_id === workspace.active_focus_id);
  const candidates = workspace.subquestions.filter((question) => question.researchability === "candidate" && question.status !== "deprioritized");
  const pendingDirectionDecision = workspace.decision_points?.find((point) => (
    point.kind === "research_direction" && point.status === "pending"
  ));
  const workspaceStatusLabel = workspace.status === "waiting_for_user_action"
    ? pendingDirectionDecision
      ? "等待你确认方向"
      : activeQuestion
        ? "等待你继续"
        : "等待确认"
    : WORKSPACE_STATUS_LABELS[workspace.status] ?? workspace.status;
  const artifactStageLabels: Record<string, string> = {
    overview: "研究概览",
    stage_note: "阶段性研究笔记",
    report_draft: "研究报告草稿",
    report: "研究报告",
  };
  const mapNodes = workspace.research_map.slice(0, 4);
  const researchIterations = [...(workspace.research_plan?.iterations ?? [])]
    .sort((left, right) => left.sequence - right.sequence);
  const artifactStage = workspace.research_plan?.artifact_stage
    || (researchIterations.length > 0 || workspace.evidence.length > 0 ? "stage_note" : "overview");
  const iterationStatusLabel: Record<string, string> = {
    in_progress: "进行中",
    waiting_for_user: "等待你确认",
    completed: "已完成",
    abandoned: "已放弃",
  };
  const visibleDocuments = documents.filter((document) => ["research-brief", "current-progress", "evidence-index", "research-plan"].includes(document.document_id));
  const publishedPaperNotes = papers.filter((paper) => paper.note_status === "published" && !!paper.note_url);
  const knowledgeAssetCount = publishedPaperNotes.length + visibleDocuments.length;
  const sourceKey = (value: string | null | undefined) => (value ?? "")
    .trim()
    .toLowerCase()
    .replace(/\/$/, "")
    .replace(/^https?:\/\/(?:www\.)?arxiv\.org\/(?:abs|pdf)\//, "arxiv:")
    .replace(/\.pdf$/, "");
  const relatedPapers = Array.from(
    explorationRuns
      .flatMap((run) => run.sources)
      .filter((source) => source.kind === "external_candidate" && (source.title || source.url))
      .reduce((unique, source) => {
        const key = sourceKey(source.url) || source.source_id;
        if (!unique.has(key)) unique.set(key, source);
        return unique;
      }, new Map<string, WorkbenchExplorationRun["sources"][number]>()),
  ).map(([, source]) => {
    const attachedPaper = papers.find((paper) => sourceKey(paper.source_url || paper.pdf_url) === sourceKey(source.url));
    return { source, attachedPaper };
  });
  const pendingRelatedCount = relatedPapers.filter(({ attachedPaper }) => !attachedPaper).length;

  return (
    <aside className="workbench-overview-rail" aria-label="研究概览" role="region">
      <header className="workbench-overview-head">
        <div>
          <span className="workbench-kicker">Research workspace</span>
          <h3>研究概览</h3>
          <p className="workbench-overview-subtitle">研究设定会一直留在这里，不随会话滚动消失。</p>
        </div>
        <button type="button" className="workbench-overview-open" onClick={() => onOpenDocument("current-progress")}>
          打开进展
        </button>
      </header>

      <section className="workbench-overview-section" aria-label="研究议题">
        <h4>研究议题</h4>
        <p className="workbench-overview-intent">{workspace.research_intent || workspace.research_question || "尚未确定研究议题"}</p>
        {workspace.research_question && workspace.research_question !== workspace.research_intent && (
          <p className="workbench-overview-question">当前问题：{workspace.research_question}</p>
        )}
      </section>

      <section className="workbench-overview-section" aria-label="当前研究焦点">
        <h4>当前研究焦点</h4>
        {activeQuestion ? (
          <div className="workbench-overview-focus-card">
            <span className="workbench-overview-focus-state">{pendingDirectionDecision ? "已选择，待确认" : "已选定"}</span>
            <strong>{activeQuestion.text}</strong>
            <button
              type="button"
              className="workbench-overview-next-step"
              disabled={focusBusy}
              onClick={() => onContinueFocus(activeQuestion.question_id)}
            >
              {focusBusy ? "正在确认方向…" : pendingDirectionDecision ? "确认方向并继续" : "开始下一步研究"}
            </button>
          </div>
        ) : (
          <p className="workbench-overview-empty">尚未选择具体焦点。可以从下方候选线索中选一个。</p>
        )}
      </section>

      <section className="workbench-overview-section" aria-label="研究状态">
        <h4>研究状态</h4>
        <div className="workbench-overview-status-row">
          <span>阶段</span>
          <strong>{workspaceStatusLabel}</strong>
        </div>
        <div className="workbench-overview-status-row">
          <span>当前产物</span>
          <strong>{artifactStageLabels[artifactStage] ?? "研究概览"}</strong>
        </div>
        <p className="workbench-overview-section-hint">
          {artifactStage === "report"
            ? "研究报告已完成并确认。"
            : artifactStage === "report_draft"
              ? "报告草稿正在等待证据边界检查。"
              : "先积累多轮证据；当前内容是阶段性结果，不是最终报告。"}
        </p>
        <div className="workbench-overview-stats" aria-label="研究概览统计">
          <span><strong>{workspace.research_map.length}</strong> 个地图节点</span>
          <span><strong>{workspace.subquestions.length}</strong> 个子问题</span>
          <span><strong>{workspace.evidence.length}</strong> 条证据</span>
          <span><strong>{workspace.workspace_revision}</strong> 次更新</span>
        </div>
      </section>

      {researchIterations.length > 0 && (
        <OverviewDisclosure
          label="研究迭代"
          count={`${researchIterations.length} 轮`}
          openByDefault
        >
          <p className="workbench-overview-section-hint">每一轮都基于上一轮的证据和未解决问题推进，不会覆盖历史结论。</p>
          <div className="workbench-overview-iterations">
            {researchIterations.slice(-4).reverse().map((iteration) => (
              <article className="workbench-overview-iteration" key={iteration.iteration_id}>
                <div className="workbench-overview-iteration-heading">
                  <strong>第 {iteration.sequence} 轮 · {iteration.title}</strong>
                  <span>{iterationStatusLabel[iteration.status] ?? iteration.status}</span>
                </div>
                {iteration.summary && <p>{iteration.summary}</p>}
                {iteration.decision && <small>决策：{iteration.decision}</small>}
                {iteration.next_step && <small>下一步：{iteration.next_step}</small>}
              </article>
            ))}
          </div>
          {researchIterations.length > 4 && (
            <p className="workbench-overview-section-hint">已折叠更早的 {researchIterations.length - 4} 轮，可打开“当前研究进展”查看完整记录。</p>
          )}
        </OverviewDisclosure>
      )}

      <OverviewDisclosure label="研究结构" count={`${workspace.research_map.length} 个节点`}>
        {mapNodes.length ? (
          <div className="workbench-overview-map-list">
            {mapNodes.map((node) => (
              <div className="workbench-overview-map-item" key={node.node_id}>
                <strong>{node.label || node.node_id}</strong>
                <span>{node.related_question_ids.length} 个相关问题 · {node.evidence_ids.length} 条证据</span>
              </div>
            ))}
          </div>
        ) : <p className="workbench-overview-empty">研究地图还在形成中。</p>}
      </OverviewDisclosure>

      {relatedPapers.length > 0 && (
        <OverviewDisclosure label="相关论文" count={pendingRelatedCount > 0 ? `${pendingRelatedCount} 待核验` : "均已纳入"}>
          <p className="workbench-overview-section-hint">检索得到的候选论文先放在这里，精读并形成证据后才会关联研究地图。</p>
          <div className="workbench-overview-related-papers">
            {relatedPapers.slice(0, 6).map(({ source, attachedPaper }) => (
              <article className="workbench-overview-related-paper" key={source.source_id || source.url || source.title}>
                <div className="workbench-overview-related-paper-main">
                  <span className={`workbench-overview-paper-state ${attachedPaper ? "attached" : "pending"}`}>
                    {attachedPaper
                      ? attachedPaper.preparation_status === "ready" ? "已纳入 · 可精读" : attachedPaper.preparation_status === "failed" ? "已纳入 · 解析失败" : "已纳入 · 准备中"
                      : "待核验"}
                  </span>
                  <strong>{source.title || "未命名候选论文"}</strong>
                  <span>{source.relevance ? `相关度 ${source.relevance}` : "外部候选来源"}</span>
                </div>
                {attachedPaper ? (
                  <button type="button" className="workbench-overview-related-paper-action" onClick={() => onOpenPaper(attachedPaper.paper_id)}>打开</button>
                ) : (
                  <button
                    type="button"
                    className="workbench-overview-related-paper-action"
                    disabled={!source.url || !onAddPaperCandidate}
                    onClick={() => source.url && onAddPaperCandidate?.({ title: source.title, url: source.url })}
                  >
                    添加
                  </button>
                )}
              </article>
            ))}
          </div>
          {relatedPapers.length > 6 && <p className="workbench-overview-section-hint">还有 {relatedPapers.length - 6} 篇候选论文，打开研究进展查看全部。</p>}
        </OverviewDisclosure>
      )}

      {knowledgeAssetCount > 0 && (
        <OverviewDisclosure label="知识资产" count={`${knowledgeAssetCount} 项`} openByDefault>
          <p className="workbench-overview-section-hint">论文笔记保留论文级知识；研究概览、进展和计划是工作区级产物，二者通过同一篇论文的证据关联。</p>
          {publishedPaperNotes.length > 0 && (
            <div className="workbench-overview-assets" aria-label="论文笔记">
              <span className="workbench-overview-assets-label">论文笔记</span>
              {publishedPaperNotes.map((paper) => (
                <article className="workbench-overview-asset" key={`note-${paper.paper_id}`}>
                  <div>
                    <strong>{paper.title || "未命名论文"}</strong>
                    <span>已发布 · 与当前论文上下文绑定</span>
                  </div>
                  <button
                    type="button"
                    className="workbench-overview-asset-action"
                    disabled={!onOpenKnowledge || !paper.note_url}
                    onClick={() => paper.note_url && onOpenKnowledge?.(paper.note_url)}
                  >
                    打开笔记
                  </button>
                </article>
              ))}
            </div>
          )}
          {visibleDocuments.length > 0 && (
            <div className="workbench-overview-assets" aria-label="研究产物">
              <span className="workbench-overview-assets-label">研究产物</span>
              {visibleDocuments.map((document) => (
                <button type="button" className="workbench-overview-asset-link" key={`asset-${document.document_id}`} onClick={() => onOpenDocument(document.document_id)}>
                  <span>{document.title}</span>
                  <small>{document.filename}</small>
                </button>
              ))}
            </div>
          )}
        </OverviewDisclosure>
      )}

      {candidates.length > 0 && (
        <OverviewDisclosure
          label="可继续探索"
          count={`${candidates.length} 个问题`}
          openByDefault={!activeQuestion || workspace.status === "waiting_for_user_action"}
        >
          <div className="workbench-overview-candidates">
            {candidates.slice(0, 3).map((question) => (
              <button
                type="button"
                key={question.question_id}
                className={question.question_id === workspace.active_focus_id ? "active" : ""}
                disabled={focusBusy}
                onClick={() => onSetFocus(question.question_id)}
              >
                <span>{question.question_id === workspace.active_focus_id ? "当前焦点" : "选择"}</span>
                <strong>{question.text}</strong>
              </button>
            ))}
          </div>
        </OverviewDisclosure>
      )}

      <OverviewDisclosure label="当前论文" count={`${papers.length} 篇`}>
        {papers.length ? papers.map((paper) => (
          <button type="button" className="workbench-overview-paper" key={paper.paper_id} onClick={() => onOpenPaper(paper.paper_id)}>
            <strong>{paper.title || "未命名论文"}</strong>
            <span>{paper.preparation_status === "ready" ? "证据已准备" : paper.preparation_status === "failed" ? "解析失败" : "正在准备证据"}</span>
          </button>
        )) : <p className="workbench-overview-empty">当前会话还没有绑定论文。</p>}
      </OverviewDisclosure>

      {visibleDocuments.length > 0 && (
        <OverviewDisclosure label="研究文档" count={`${visibleDocuments.length} 份`}>
          <nav className="workbench-overview-documents" aria-label="研究文档">
            {visibleDocuments.map((document) => (
              <button type="button" key={document.document_id} onClick={() => onOpenDocument(document.document_id)}>
                {document.title}
              </button>
            ))}
          </nav>
        </OverviewDisclosure>
      )}
    </aside>
  );
}

function WorkbenchRunTrace({ runs }: { runs: WorkbenchExplorationRun[] }) {
  const visibleRuns = dedupeRunsByRunId(runs)
    .sort((left, right) => Date.parse(right.created_at ?? "") - Date.parse(left.created_at ?? ""))
    .slice(0, 4);
  return (
    <section className="workbench-public-trace" aria-label="公开运行轨迹">
      <header className="workbench-public-trace-heading">
        <div>
          <span className="workbench-kicker">Research progress</span>
          <strong>研究进度</strong>
        </div>
        <span>{visibleRuns.length} 个运行</span>
      </header>
      {visibleRuns.length === 0 ? (
        <p className="workbench-public-trace-empty">发送需要检索或精读的问题后，研究进度会显示在这里。</p>
      ) : (
        <div className="workbench-public-trace-list">
          {visibleRuns.map((run) => {
            const events = run.events ?? [];
            const phases = tracePhases(run);
            const duration = formatTraceDuration(run);
            const route = retrievalPlanLabel(run.retrieval_plan) ?? (run.profile === "literature" ? "文献探索" : "研究助理");
            const evidenceCount = Math.max(
              run.budget_used.block_reads,
              events.filter((event) => event.event_type === "context_read").length,
            );
            const toolDetails = run.tool_details ?? [];
            const toolCount = Math.max(
              run.budget_used.tool_calls,
              events.filter((event) => event.event_type === "tool_started").length,
            );
            const latestEvent = events[events.length - 1];
            const isLive = !TERMINAL_EXPLORATION_STATES.includes(run.status);
            return (
              <article className={`workbench-public-trace-run ${run.status}`} key={run.run_id}>
                <header className="workbench-public-trace-run-header">
                  <div className="workbench-public-trace-run-title">
                    <div>
                      <span className={`workbench-trace-status ${run.status}`}>{attemptStatusLabel(run.status)}</span>
                      <strong>{route}</strong>
                    </div>
                    <p title={run.question}>{run.question}</p>
                  </div>
                  {duration && <time dateTime={run.current_attempt?.finished_at ?? run.current_attempt?.started_at ?? run.created_at}>{duration}</time>}
                </header>
                <div className="workbench-public-trace-stats" aria-label="运行统计">
                  <span><strong>{run.sources.length}</strong>来源</span>
                  <span><strong>{evidenceCount}</strong>证据块</span>
                  <span><strong>{toolCount}/{run.budget_used.tool_limit}</strong>工具</span>
                </div>
                <ol className="workbench-public-trace-phases" aria-label="研究阶段">
                  {phases.map((phase) => (
                    <li key={phase.key} className={phase.state}>
                      <span className="workbench-public-trace-phase-marker" aria-hidden="true">
                        {phase.state === "completed" ? "✓" : phase.state === "active" ? "●" : phase.state === "waiting" ? "!" : phase.state === "failed" ? "×" : ""}
                      </span>
                      <span className="workbench-public-trace-phase-copy">
                        <strong>{phase.label}</strong>
                        <small>{phase.state === "active" && run.phase ? run.phase : phase.description}</small>
                      </span>
                    </li>
                  ))}
                </ol>
                <div className="workbench-public-trace-evidence" aria-label="证据流">
                  <span>问题</span><i aria-hidden="true">→</i><span>来源 {run.sources.length}</span><i aria-hidden="true">→</i><span>证据 {evidenceCount}</span><i aria-hidden="true">→</i><span className={run.partial_result ? "ready" : ""}>结论 {run.partial_result ? "已生成" : "待生成"}</span>
                </div>
                {latestEvent && <p className="workbench-public-trace-latest">最近更新：{latestEvent.summary}</p>}
                {run.failure?.safe_message && <p className="workbench-public-trace-failure" role="alert">{run.failure.safe_message}</p>}
                <details className="workbench-public-trace-details">
                  <summary>技术细节 · {toolCount} 次工具调用{isLive ? " · 执行中" : ""}</summary>
                  {events.length > 0 && (
                    <ol className="workbench-public-trace-event-list" aria-label="事件明细">
                      {events.slice(-10).map((event) => (
                        <li key={`${event.attempt_id ?? run.run_id}-${event.sequence_no}`}>
                          <span className="workbench-public-trace-event-time">{event.occurred_at ? new Date(event.occurred_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : `#${event.sequence_no}`}</span>
                          <span className="workbench-public-trace-event-type">{explorationEventLabel(event.event_type)}</span>
                          <span>{traceEventDescription(event)}</span>
                        </li>
                      ))}
                    </ol>
                  )}
                  {toolDetails.length > 0 && (
                    <ul className="workbench-public-trace-tool-details" aria-label="工具结果">
                      {toolDetails.map((tool) => (
                        <li key={tool.tool_call_id} className={tool.status !== "succeeded" ? "failed" : ""}>
                          <div><strong>{traceToolLabel(tool.tool_name)}</strong><span>{traceToolStatusLabel(tool.status)}</span></div>
                          {tool.safe_message && <small>{tool.safe_message}</small>}
                          {tool.status !== "succeeded" && <small>建议：{tool.recommended_action}</small>}
                        </li>
                      ))}
                    </ul>
                  )}
                  <div className="workbench-public-trace-usage">
                    <span>Attempt {run.attempt}</span>
                    <span>Token {run.token_usage.input_tokens} 入 / {run.token_usage.output_tokens} 出</span>
                    {run.web_search_usage?.calls ? <span>网页搜索 {run.web_search_usage.calls} 次</span> : null}
                  </div>
                  {(run.attempt_history ?? []).length > 1 && (
                    <div className="workbench-public-trace-attempt-chain" aria-label="Attempt 历史">
                      {(run.attempt_history ?? []).map((attempt, index) => (
                        <span key={attempt.attempt_id} className={attempt.status}>
                          {index > 0 && <i aria-hidden="true">→</i>}
                          Attempt {attempt.attempt_no} · {attemptStatusLabel(attempt.status)}
                        </span>
                      ))}
                    </div>
                  )}
                </details>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

export function WorkbenchApp({ client, onOpenKnowledge, initialSessionId }: Props) {
  const [sessions, setSessions] = useState<WorkbenchSession[]>([]);
  const [current, setCurrent] = useState<WorkbenchSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [sessionRename, setSessionRename] = useState<{ sessionId: string; initial: string } | null>(null);
  const [sessionContextMenu, setSessionContextMenu] = useState<{ sessionId: string; x: number; y: number } | null>(null);
  const [query, setQuery] = useState("");
  const [selectionContext, setSelectionContext] = useState<PaperContextRef | null>(null);
  const [pendingSelectionText, setPendingSelectionText] = useState<string | null>(null);
  const [pendingSelectionPage, setPendingSelectionPage] = useState(1);
  const [pendingSelectionRects, setPendingSelectionRects] = useState<SelectionRect[]>([]);
  const [pendingSelectionPageSize, setPendingSelectionPageSize] = useState<SelectionPageSize | undefined>();
  const [chatError, setChatError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ message: string; dangerLabel: string; onConfirm: () => void } | null>(null);
  const [workspaceRename, setWorkspaceRename] = useState<{ workspaceId: string; initial: string } | null>(null);
  const [chatBusy, setChatBusy] = useState(false);
  const [pendingEvidenceAction, setPendingEvidenceAction] = useState<PendingEvidenceAction | null>(null);
  const [pendingSelectionQuestion, setPendingSelectionQuestion] = useState<PendingSelectionQuestion | null>(null);
  // Set only by the explicit “开始下一步研究” CTA.  Keeping this separate
  // from the natural-language prompt prevents an open PDF from downgrading a
  // research turn to the paper-answer route.
  const [pendingTurnAction, setPendingTurnAction] = useState<"research_run" | null>(null);
  const paperFileInputRef = useRef<HTMLInputElement>(null);
  const [citationHighlight, setCitationHighlight] = useState<CitationRef["bbox"] | null>(null);
  const [citationHighlightPage, setCitationHighlightPage] = useState(1);
  const [citationPaperId, setCitationPaperId] = useState<string | null>(null);
  const [paperInputError, setPaperInputError] = useState<string | null>(null);
  const [paperUrl, setPaperUrl] = useState("");
  const [contextScope, setContextScope] = useState<"none" | "selection" | "section" | "full">("none");
  const [workspace, setWorkspace] = useState<WorkbenchWorkspaceState | null>(null);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [workspaceDocuments, setWorkspaceDocuments] = useState<WorkspaceDocument[]>([]);
  const [activeWorkspaceDocument, setActiveWorkspaceDocument] = useState<string | null>(null);
  const [focusBusy, setFocusBusy] = useState(false);
  const [leftRailTab, setLeftRailTab] = useState<"workspace" | "trace">("workspace");
  const [answerMode, setAnswerMode] = useState<AnswerMode>("auto");
  const [railWidth, setRailWidth] = useState<number | null>(null);
  const [overviewWidth, setOverviewWidth] = useState<number | null>(null);
  const railRef = useRef<HTMLElement | null>(null);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const startResize = (
    event: React.MouseEvent,
    setWidth: (width: number) => void = setRailWidth,
    defaultWidth = 480,
    minWidth = 320,
    maxWidth = 1200,
  ) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = railRef.current?.getBoundingClientRect().width ?? defaultWidth;
    const move = (moveEvent: MouseEvent) => {
      // Dragging left widens the right rail (and shrinks the conversation);
      // clamp so the rail can take most of the pane but never vanish.
      setWidth(Math.round(Math.min(Math.max(minWidth, startWidth + (startX - moveEvent.clientX)), maxWidth)));
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };
  const readerPaper = current?.paper_panel_open ? current.papers.find((paper) => paper.paper_id === current.active_paper_id) ?? null : null;

  // Conversation timeline: chat messages and exploration runs interleave by
  // creation time, so an exploration reads as part of the dialogue instead of
  // a separate card stack below the chat.
  const conversation: ConversationItem[] = useMemo(() => {
    const merged: { at: number; item: ConversationItem }[] = [];
    for (const message of current?.messages ?? []) {
      merged.push({ at: Date.parse(message.created_at) || 0, item: { kind: "message", message } });
    }
    for (const run of dedupeRunsByRunId(current?.exploration_runs ?? [])) {
      merged.push({ at: Date.parse(run.created_at ?? current?.updated_at ?? "") || 0, item: { kind: "exploration", run } });
    }
    for (const run of current?.note_runs ?? []) {
      merged.push({ at: Date.parse(run.created_at ?? current?.updated_at ?? "") || 0, item: { kind: "note", run } });
    }
    return merged.sort((left, right) => left.at - right.at).map((entry) => entry.item);
  }, [current]);
  const latestExplorationRunId = [...conversation].reverse().find((item) => item.kind === "exploration")?.run.run_id;
  const pendingWorkspaceDecisions = workspace?.decision_points?.filter((point) => point.status === "pending") ?? [];

  // Workspace 1:N Session tree: group the flat session list by resource
  // workspace, so threads under one research project collapse together.
  const workspaceGroups = useMemo(() => {
    const groups = new Map<string, WorkbenchSession[]>();
    for (const session of sessions) {
      const key = session.workspace_id ?? "ungrouped";
      const bucket = groups.get(key) ?? [];
      bucket.push(session);
      groups.set(key, bucket);
    }
    return Array.from(groups.entries()).map(([workspaceId, items]) => {
      const sorted = items.sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at));
      return {
        workspaceId,
        title: workspaceId === "ungrouped" ? "未分组" : (sorted[0]?.workspace_title ?? sorted[0]?.title ?? "研究项目"),
        count: sorted.length,
        items: sorted,
      };
    });
  }, [sessions]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const records = await client.listSessions();
        if (cancelled) return;
        const ordered = [...records].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at));
        setSessions(ordered);
        const preferredId = initialSessionId ?? ordered[0]?.session_id;
        if (preferredId) {
          // listSessions deliberately returns shallow metadata. Hydrate only
          // the session that will be displayed so entering the workbench is
          // fast even when the history contains many sessions. An explicit
          // hand-off id still wins over the recency default and may not yet be
          // present in the list response, hence the direct getSession call.
          const listed = ordered.find((item) => item.session_id === preferredId);
          const selected = await client.getSession(preferredId);
          if (cancelled) return;
          // Some clients surface a non-fatal history warning in the list
          // projection. Preserve it when the detailed response omits it.
          setCurrent({ ...selected, history_error: selected.history_error ?? listed?.history_error ?? null });
        }
      } catch (reason) {
        if (!cancelled) setChatError(errorMessage(reason, "会话加载失败，请检查后端是否已启动"));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [client, initialSessionId]);

  // Upload/parse is asynchronous on the API. Keep the current paper projection
  // fresh so the evidence-block gate does not remain stuck after parsing ends.
  useEffect(() => {
    const sessionId = current?.session_id;
    const paper = current?.papers.find((item) => item.paper_id === current.active_paper_id);
    if (!sessionId || !paper || paper.preparation_status === "ready" || paper.preparation_status === "failed") return;
    const timer = window.setInterval(() => {
      void client.getSession(sessionId).then((updated) => {
        const nextPaper = updated.papers.find((item) => item.paper_id === updated.active_paper_id);
        if (nextPaper?.preparation_status === "ready" || nextPaper?.preparation_status === "failed") applyUpdatedSession(updated);
      }).catch(() => undefined);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [client, current?.session_id, current?.active_paper_id, current?.papers]);

  // Exploration runs execute in the background after the 202 response. While a
  // run is queued/running, poll the session so the exploration bubble streams
  // the persisted event flow (phases, tool calls, discovered sources) live,
  // and still recovers the full history after a refresh (events are durable).
  useEffect(() => {
    const sessionId = current?.session_id;
    const hasLiveRun = (current?.exploration_runs ?? []).some((run) => run.status === "queued" || run.status === "running");
    if (!sessionId || !hasLiveRun) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void client.getSession(sessionId).then((updated) => {
        if (!cancelled) applyUpdatedSession(updated);
      }).catch(() => undefined);
      // A durable research run advances the workspace in the worker, not in
      // the initial POST response. Poll the workspace alongside the session so
      // the right rail reflects investigating/awaiting states instead of
      // remaining on the pre-submit snapshot.
      if (client.fetchWorkspace) {
        void client.fetchWorkspace(sessionId)
          .then((updatedWorkspace) => {
            if (!cancelled) setWorkspace(updatedWorkspace);
          })
          .catch(() => undefined);
      }
    }, 1500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [client, current]);

  // Paper answers are acknowledged before the provider finishes. Poll only
  // while a persisted assistant message is queued/generating; terminal
  // messages stop the timer so idle sessions never create background traffic.
  const hasPendingChatMessage = (current?.messages ?? []).some((message) => (
    message.role === "assistant" &&
    (message.generation_status === "queued" || message.generation_status === "generating")
  ));
  useEffect(() => {
    const sessionId = current?.session_id;
    if (!sessionId || !hasPendingChatMessage) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void client.getSession(sessionId).then((updated) => {
        if (!cancelled) applyUpdatedSession(updated);
      }).catch(() => undefined);
    }, 700);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [client, current?.session_id, hasPendingChatMessage]);

  // NoteRun generation is a durable background task as well.  Poll its
  // persisted state until it reaches a draft, published, or failed terminal
  // state so a slow first response cannot leave the UI stuck on a spinner.
  const hasPendingNoteRun = (current?.note_runs ?? []).some((run) => run.status === "queued" || run.status === "generating");
  useEffect(() => {
    const sessionId = current?.session_id;
    if (!sessionId || !hasPendingNoteRun) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void client.getSession(sessionId).then((updated) => {
        if (!cancelled) applyUpdatedSession(updated);
      }).catch(() => undefined);
    }, 700);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [client, current?.session_id, hasPendingNoteRun]);

  async function createSession(workspaceId?: string | null) {
    try {
      const created = await client.createSession(workspaceId);
      setSessions((records) => [created, ...records]);
      setCurrent(created);
      setChatError(null);
    } catch (reason) {
      setChatError(errorMessage(reason, "新建会话失败，请检查后端是否已启动"));
    }
  }

  async function openSession(sessionId: string) {
    try {
      setCurrent(await client.getSession(sessionId));
      setChatError(null);
    } catch (reason) {
      setChatError(errorMessage(reason, "打开会话失败，请刷新后重试"));
    }
  }

  async function addDemoPaper() {
    if (!current || !client.addFixturePaper) return;
    const updated = await client.addFixturePaper(current.session_id, "paper-demo");
    setCurrent(updated);
    setSessions((records) => records.map((record) => record.session_id === updated.session_id ? updated : record));
  }

  function applyUpdatedSession(updated: WorkbenchSession) {
    setCurrent(updated);
    setSessions((records) => records.map((record) => record.session_id === updated.session_id ? updated : record));
  }

  async function attachLocalPdf(file: File | undefined) {
    if (!current || !file || (!client.uploadPdf && !client.attachPrototypePaper)) return;
    if (file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) {
      setPaperInputError("请选择 PDF 文件");
      return;
    }
    setPaperInputError(null);
    try {
      applyUpdatedSession(client.uploadPdf
        ? await client.uploadPdf(current.session_id, file)
        : await client.attachPrototypePaper!(current.session_id, { title: file.name, pdf_url: URL.createObjectURL(file) }));
    } catch (reason) {
      setPaperInputError(reason instanceof Error ? reason.message : "论文添加失败");
    }
  }

  /* URL import is intentionally not exposed in the V1 workbench UI. */
  async function attachPdfUrl(event: FormEvent) {
    event.preventDefault();
    if (!current || (!client.addPaperUrl && !client.attachPrototypePaper)) return;
    let parsed: URL;
    try {
      parsed = new URL(paperUrl);
      if (!(["http:", "https:"].includes(parsed.protocol))) throw new Error();
    } catch {
      setPaperInputError("请输入有效的 HTTP(S) PDF 地址");
      return;
    }
    setPaperInputError(null);
    const title = decodeURIComponent(parsed.pathname.split("/").filter(Boolean).at(-1) ?? parsed.hostname);
    try {
      applyUpdatedSession(client.addPaperUrl
        ? await client.addPaperUrl(current.session_id, parsed.toString())
        : await client.attachPrototypePaper!(current.session_id, { title: title.toLowerCase().endsWith(".pdf") ? title : `来自 ${parsed.hostname} 的论文`, pdf_url: parsed.toString(), source_url: parsed.toString() }));
      setPaperUrl("");
    } catch (reason) {
      setPaperInputError(reason instanceof Error ? reason.message : "论文添加失败");
    }
  }

  async function renameSessionById(sessionId: string, title: string) {
    try {
      const updated = await client.renameSession(sessionId, title);
      setCurrent((record) => record?.session_id === updated.session_id ? updated : record);
      setSessions((records) => records.map((record) => record.session_id === updated.session_id ? updated : record));
      setSessionRename(null);
    } catch (reason) {
      setChatError(reason instanceof Error ? reason.message : "重命名会话失败");
    }
  }

  async function togglePaper(open: boolean) {
    if (!current) return;
    const hasActivePaper = current.papers.some((paper) => paper.paper_id === current.active_paper_id);
    const defaultPaperId = open && !hasActivePaper ? current.papers[0]?.paper_id : undefined;
    const updated = await client.updateLayout(current.session_id, open, defaultPaperId);
    setCurrent(updated);
    setSessions((records) => records.map((record) => record.session_id === updated.session_id ? updated : record));
  }

  async function openPaper(paperId: string) {
    if (!current) return;
    const updated = await client.updateLayout(current.session_id, true, paperId);
    setCurrent(updated);
    setSessions((records) => records.map((record) => record.session_id === updated.session_id ? updated : record));
  }

  async function deleteSession(sessionId: string) {
    const target = sessions.find((session) => session.session_id === sessionId);
    setConfirm({
      message: `确定删除会话「${target?.title ?? sessionId}」吗？删除后无法恢复。`,
      dangerLabel: "删除会话",
      onConfirm: () => {
        setConfirm(null);
        void doDeleteSession(sessionId);
      },
    });
  }

  async function doDeleteSession(sessionId: string) {
    try {
      await client.deleteSession(sessionId);
      const remaining = sessions.filter((session) => session.session_id !== sessionId);
      setSessions(remaining);
      if (current?.session_id === sessionId) setCurrent(remaining[0] ?? null);
    } catch (reason) {
      setChatError(reason instanceof Error ? reason.message : "删除会话失败");
    }
  }

  async function renameWorkspace(workspaceId: string, initial: string) {
    setWorkspaceRename({ workspaceId, initial });
  }

  async function doRenameWorkspace(workspaceId: string, title: string) {
    try {
      await client.renameWorkspace(workspaceId, title);
      setSessions((records) => records.map((session) => (
        session.workspace_id === workspaceId ? { ...session, workspace_title: title } : session
      )));
      if (current?.workspace_id === workspaceId) setCurrent({ ...current, workspace_title: title });
    } catch (reason) {
      setChatError(reason instanceof Error ? reason.message : "重命名工作区失败");
    } finally {
      setWorkspaceRename(null);
    }
  }

  function deleteWorkspace(workspaceId: string, title: string) {
    setConfirm({
      message: `确定删除工作区「${title}」及其全部会话吗？删除后无法恢复。`,
      dangerLabel: "删除工作区",
      onConfirm: () => {
        setConfirm(null);
        void doDeleteWorkspace(workspaceId);
      },
    });
  }

  async function doDeleteWorkspace(workspaceId: string) {
    try {
      await client.deleteWorkspace(workspaceId);
      const remaining = sessions.filter((session) => session.workspace_id !== workspaceId);
      setSessions(remaining);
      if (current?.workspace_id === workspaceId) setCurrent(remaining[0] ?? null);
    } catch (reason) {
      setChatError(reason instanceof Error ? reason.message : "删除工作区失败");
    }
  }

  async function submitQuestion(event: FormEvent) {
    event.preventDefault();
    if (!current || !query.trim()) return;
    const effectiveScope = selectionContext ? "selection" : current.papers.some((paper) => paper.preparation_status === "ready") ? "full" : "none";
    if (effectiveScope === "selection" && !selectionContext) {
      setChatError("请先在 PDF 中选择文字");
      return;
    }
    // The composer launches the research-assistant agent with the live
    // interaction context (active paper + selection) so the Scope Resolver can
    // pin the evidence scope and inject it as a leading system message at run
    // time. The stored question stays clean (no inlined context).
    const agentQuestion = query.trim();
    const interactionContext = buildInteractionContext(current, selectionContext, workspace);
    const isResearchContinuation = Boolean(
      workspace?.active_focus_id
      && /^请围绕当前研究焦点继续深入研究[:：]/.test(agentQuestion)
    );
    // An explicit research continuation wins over a stale PDF selection.  A
    // selection chip can remain visible while the user is accepting a new
    // research focus, and letting it win would silently route this turn back
    // to the paper-answer path.
    const explicitAction = pendingTurnAction
      ?? (isResearchContinuation
        ? "research_run"
        : answerMode !== "auto"
          ? answerMode
          : selectionContext ? "ask_selection" : undefined);
    setChatError(null);
    setChatBusy(true);
    if (selectionContext) setPendingSelectionQuestion({ text: agentQuestion });
    try {
      if (client.sendTurn) {
        applyUpdatedSession(await client.sendTurn(current.session_id, agentQuestion, {
          action: explicitAction,
          context: selectionContext ?? undefined,
          interactionContext,
          profile: "assistant",
        }));
      } else if (client.startFixtureExploration) {
        applyUpdatedSession(await client.startFixtureExploration(current.session_id, agentQuestion, "assistant", interactionContext));
      }
      await refreshWorkspace();
      setQuery("");
      setPendingTurnAction(null);
      setSelectionContext(null);
    } catch (reason) {
      setChatError(reason instanceof Error ? reason.message : "研究助理请求失败，请检查后端是否已启动");
    } finally {
      setPendingSelectionQuestion(null);
      setChatBusy(false);
    }
  }

  async function runEvidenceAction(action: "translate" | "explain", context = selectionContext) {
    if (!current || !context) return;
    const prompt = action === "translate"
      ? "请将当前选区翻译成自然、完整的中文。仅保留必要的专业术语、专有名词和缩写英文，不要整句保留英文；按原文段落分段。输出格式：## 中文翻译\n（分段翻译）\n\n## 关键术语\n用一句话说明最重要的术语。不要输出‘来源：[论文证据]’、block_id 或其他内部证据标记。"
      : "请解释当前选区，使用中文并按段落组织：先用一两句话概括含义，再用要点说明关键术语、方法或公式。必要时保留专业术语英文，不要整段复述英文原文。不要输出‘来源：[论文证据]’、block_id 或其他内部证据标记。"
    setChatError(null);
    setChatBusy(true);
    setPendingEvidenceAction({ action, prompt });
    try {
      if (client.sendTurn) {
        applyUpdatedSession(await client.sendTurn(current.session_id, prompt, {
          action,
          context,
          interactionContext: buildInteractionContext(current, context, workspace),
        }));
      } else if (client.addFixtureMessage) {
        applyUpdatedSession(await client.addFixtureMessage(current.session_id, prompt, context));
      }
      await refreshWorkspace();
    } catch (reason) {
      setChatError(reason instanceof Error ? reason.message : "证据操作失败，请检查后端是否已启动");
    } finally {
      setPendingEvidenceAction(null);
      setChatBusy(false);
    }
  }

  async function cancelExploration(runId: string) {
    if (!current || !client.cancelFixtureExploration) return;
    try {
      applyUpdatedSession(await client.cancelFixtureExploration(current.session_id, runId));
    } catch (reason) {
      setChatError(errorMessage(reason, "停止探索失败，请刷新后重试"));
    }
  }

  async function continueExploration(runId: string) {
    if (!current || !client.continueExploration) return;
    try {
      applyUpdatedSession(await client.continueExploration(current.session_id, runId));
    } catch (reason) {
      setChatError(errorMessage(reason, "继续探索失败，请刷新后重试"));
    }
  }

  async function refreshWorkspace() {
    if (!current?.session_id || !client.fetchWorkspace) return;
    try {
      const nextWorkspace = await client.fetchWorkspace(current.session_id);
      setWorkspace(nextWorkspace);
      if (nextWorkspace && client.fetchWorkspaceDocuments) {
        const documents = await client.fetchWorkspaceDocuments(current.session_id);
        setWorkspaceDocuments(documents);
      } else {
        setWorkspaceDocuments([]);
      }
      setWorkspaceError(null);
    } catch (reason) {
      setWorkspaceError(reason instanceof Error ? reason.message : "工作区状态暂不可用");
    }
  }

  useEffect(() => {
    setActiveWorkspaceDocument(null);
    setWorkspaceDocuments([]);
    void refreshWorkspace();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current?.session_id]);

  useEffect(() => {
    if (!sessionContextMenu) return;
    const closeMenu = () => setSessionContextMenu(null);
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") closeMenu(); };
    window.addEventListener("click", closeMenu);
    window.addEventListener("contextmenu", closeMenu);
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      window.removeEventListener("click", closeMenu);
      window.removeEventListener("contextmenu", closeMenu);
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [sessionContextMenu]);

  function openWorkspaceDocument(documentId = "current-progress") {
    if (!workspaceDocuments.length && current?.session_id && client.fetchWorkspaceDocuments) {
      void client.fetchWorkspaceDocuments(current.session_id)
        .then((documents) => {
          setWorkspaceDocuments(documents);
          setActiveWorkspaceDocument(documentId);
        })
        .catch((reason) => setWorkspaceError(errorMessage(reason, "研究文档暂不可用")));
      return;
    }
    setActiveWorkspaceDocument(documentId);
  }

  async function setWorkspaceFocus(questionId: string): Promise<boolean> {
    if (!current?.session_id || !client.setWorkspaceFocus) return false;
    setFocusBusy(true);
    try {
      const nextWorkspace = await client.setWorkspaceFocus(current.session_id, questionId);
      setWorkspace(nextWorkspace);
      const pendingDirection = (nextWorkspace.decision_points ?? workspace?.decision_points ?? []).find((point) => (
        point.kind === "research_direction" &&
        point.status === "pending" &&
        (!(point.candidate_question_ids ?? []).length || point.candidate_question_ids?.includes(questionId))
      ));
      if (pendingDirection && client.resolveWorkspaceDecision) {
        const resolved = await resolveWorkspaceDecision(pendingDirection.decision_id, true, questionId);
        if (!resolved) return false;
      } else {
        await refreshWorkspace();
      }
      setWorkspaceError(null);
      return true;
    } catch (reason) {
      setWorkspaceError(errorMessage(reason, "无法设置当前研究焦点"));
      return false;
    } finally {
      setFocusBusy(false);
    }
  }

  async function resolveWorkspaceDecision(decisionId: string, approved: boolean, questionId?: string): Promise<boolean> {
    if (!current?.session_id || !client.resolveWorkspaceDecision) return false;
    setWorkspaceError(null);
    try {
      await client.resolveWorkspaceDecision(current.session_id, decisionId, approved, undefined, questionId);
      await refreshWorkspace();
      return true;
    } catch (reason) {
      setWorkspaceError(reason instanceof Error ? reason.message : "无法提交决策");
      return false;
    }
  }

  async function prepareNextResearch(questionId: string) {
    const question = workspace?.subquestions.find((item) => item.question_id === questionId);
    if (!question) return;
    if (!await setWorkspaceFocus(questionId)) return;
    setPendingTurnAction("research_run");
    setQuery(`请围绕当前研究焦点继续深入研究：${question.text}`);
    window.setTimeout(() => composerRef.current?.focus(), 0);
  }

  async function addExternalCandidate(source: { title: string; url?: string }) {
    if (!current || !source.url || !client.addPaperUrl) return;
    setChatError(null);
    try {
      applyUpdatedSession(await client.addPaperUrl(current.session_id, source.url, source.title));
    } catch (reason) {
      setChatError(errorMessage(reason, "候选论文添加失败，请检查后端与网络"));
    }
  }

  function resolvePdfSelection(text: string, page = 1, bbox?: PaperContextRef["bbox"], rects?: SelectionRect[], pageSize?: SelectionPageSize): PaperContextRef | null {
    if (!current?.active_paper_id) { setChatError("当前会话还没有活动论文"); return null; }
    const paper = current.papers.find((item) => item.paper_id === current.active_paper_id);
    if (!paper?.selection_blocks?.length) { setChatError("论文证据块尚未准备好，请等待解析完成后重试"); return null; }
    const matchableBlocks = paper.selection_blocks.map((block) => ({ ...block, bbox: normalizeBlockBbox(block, pageSize) }));
    const selected = matchSelectionToBlocks({ text, page, bbox, rects }, matchableBlocks);
    if (!selected?.length) { setChatError("证据块已加载，但这段文字与解析结果未对齐；请缩短选区，尽量只选一个完整句子"); return null; }
    const primary = selected[0];
    const sectionPath = primary.section_path ?? [];
    return {
      paper_id: current.active_paper_id,
      scope: "selection",
      block_id: primary.block_id,
      block_ids: selected.map((block) => block.block_id),
      section_path: sectionPath,
      label: `${sectionPath.join(" / ") || "论文正文"} · 第 ${primary.page ?? page} 页 · ${selected.length} 个证据块`,
      text,
      status: "attached",
      page: primary.page ?? page,
      bbox,
      rects,
      page_size: pageSize,
    };
  }

  function attachPdfSelection(context: PaperContextRef) {
    setSelectionContext(context);
    setContextScope("selection");
    setPendingSelectionText(null);
    setPendingSelectionRects([]);
    setPendingSelectionPageSize(undefined);
  }

  function acceptPdfSelection(text: string, page = 1, bbox?: PaperContextRef["bbox"], rects?: SelectionRect[], pageSize?: SelectionPageSize) {
    const context = resolvePdfSelection(text, page, bbox, rects, pageSize);
    if (context) attachPdfSelection(context);
  }

  async function handlePdfSelectionAction(action: "translate" | "explain", selection: { text: string; page: number; bbox?: PaperContextRef["bbox"]; rects?: SelectionRect[]; pageSize?: SelectionPageSize }) {
    if (chatBusy) return;
    const context = resolvePdfSelection(selection.text, selection.page, selection.bbox, selection.rects, selection.pageSize);
    if (!context) return;
    attachPdfSelection(context);
    await runEvidenceAction(action, context);
  }

  async function openCitation(citation: CitationRef) {
    if (citation.status !== "resolved" || !citation.page) return;
    setCitationHighlight(citation.bbox ?? (selectionContext?.block_id === citation.block_id ? selectionContext.bbox ?? null : null));
    setCitationHighlightPage(citation.page);
    setCitationPaperId(citation.paper_id ?? null);
    if (citation.paper_id && citation.paper_id !== current?.active_paper_id) {
      applyUpdatedSession(await client.updateLayout(current?.session_id ?? "", true, citation.paper_id));
    } else await togglePaper(true);
  }

  async function startNoteRun(requestedPaperId?: string) {
    if (!current || !client.startFixtureNoteRun) return;
    const paperId = requestedPaperId ?? current.active_paper_id;
    if (!paperId) return;
    const sourcePaper = current.papers.find((paper) => paper.paper_id === paperId);
    if (!sourcePaper || sourcePaper.preparation_status !== "ready") {
      setChatError("论文证据还在准备中，解析完成后才能生成笔记");
      return;
    }
    const generating: WorkbenchSession = {
      ...current,
      papers: current.papers.map((paper) => paper.paper_id === paperId ? { ...paper, note_status: "generating" } : paper),
    };
    setCurrent(generating);
    setSessions((records) => records.map((record) => record.session_id === generating.session_id ? generating : record));
    try {
      const updated = await client.startFixtureNoteRun(current.session_id, paperId);
      setCurrent(updated);
      setSessions((records) => records.map((record) => record.session_id === updated.session_id ? updated : record));
    } catch (reason) {
      setChatError(errorMessage(reason, "笔记生成启动失败，请稍后重试"));
      // The optimistic local status must never survive a rejected request.
      try {
        applyUpdatedSession(await client.getSession(current.session_id));
      } catch {
        setCurrent((record) => record ? {
          ...record,
          papers: record.papers.map((paper) => paper.paper_id === paperId ? { ...paper, note_status: "idle", note_error: null } : paper),
        } : record);
      }
    }
  }

  async function publishNoteRun(noteRunId: string) {
    if (!current || !client.publishNoteRun) return;
    setChatError(null);
    try {
      applyUpdatedSession(await client.publishNoteRun(noteRunId));
    } catch (reason) {
      setChatError(errorMessage(reason, "论文笔记发布失败，请稍后重试"));
    }
  }

  function renderConversationItem(item: ConversationItem): ReactNode {
    if (item.kind === "message") {
      const assistantMarkdown = item.message.role === "assistant" && item.message.generation_status === "completed" && item.message.text.trim()
        ? tokenizeCitations(removeUnresolvedCitationTokens(cleanAssistantMessage(item.message.text), item.message.citations))
        : null;
      const MessageCitationLink = (props: { href?: string; children?: ReactNode }) => {
        const href = String(props.href ?? "").replace(/^#/, "");
        const match = href.match(/^cite-(\d+)$/);
        const ref = match && assistantMarkdown ? assistantMarkdown.refs[Number(match[1]) - 1] : undefined;
        const citation = ref ? item.message.citations.find((candidate) => candidate.block_id === ref.blockId && candidate.status === "resolved") : undefined;
        if (!match) return <a href={props.href}>{props.children}</a>;
        if (!citation) return <span className="workbench-inline-citation workbench-inline-citation-unresolved" title="引用无法定位">{ref?.label ?? "?"}</span>;
        return <a href={props.href} className="workbench-inline-citation" title="跳转到论文原文" onClick={(event) => { event.preventDefault(); void openCitation(citation); }}>{ref?.label ?? "?"}</a>;
      };
      return (
        <article className={`workbench-message ${item.message.role} ${item.message.generation_status !== "completed" ? "pending" : ""}`} key={item.message.message_id}>
          <span>{item.message.role === "user" ? "你" : "Research Pulse"}</span>
          {assistantMarkdown ? <div className="workbench-message-body"><ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={{ a: MessageCitationLink }}>{assistantMarkdown.markdown}</ReactMarkdown></div> : <p>{item.message.role === "user"
            ? displayUserMessage(item.message.text)
            : item.message.generation_status === "queued"
              ? "回答已排队…"
              : item.message.generation_status === "generating"
                ? "正在生成回答…"
                : item.message.generation_status === "failed"
                  ? "回答未完成，请重试。"
                  : item.message.text}</p>}
        </article>
      );
    }
    if (item.kind === "note") {
      const paperTitle = current?.papers.find((paper) => paper.paper_id === item.run.paper_id)?.title ?? "当前论文";
      return (
        <NoteRunBubble
          key={item.run.note_run_id}
          run={item.run}
          paperTitle={paperTitle}
          onPublish={(runId) => void publishNoteRun(runId)}
          onRetry={(paperId) => void startNoteRun(paperId)}
          onOpenKnowledge={onOpenKnowledge}
        />
      );
    }
    return (
      <ExplorationBubble
        key={item.run.run_id}
        run={item.run}
        onCancel={(runId) => void cancelExploration(runId)}
        onContinue={(runId) => void continueExploration(runId)}
        onAddCandidate={(source) => void addExternalCandidate(source)}
        pendingDecisions={item.run.run_id === latestExplorationRunId ? pendingWorkspaceDecisions : []}
        candidateQuestions={workspace?.subquestions.filter((question) => question.researchability === "candidate") ?? []}
        onResolveDecision={(decisionId, approved, questionId) => void resolveWorkspaceDecision(decisionId, approved, questionId)}
        onOpenBlock={(blockId) => {
          const paper = current?.papers.find((candidate) => candidate.paper_id === current.active_paper_id);
          const block = paper?.selection_blocks?.find((candidate) =>
            candidate.block_id === blockId || candidate.block_id.endsWith(blockId) || candidate.block_id.endsWith(`:${blockId}`)
          );
          const page = block?.page ?? undefined;
          if (page != null) {
            openCitation({ block_id: blockId, paper_id: current?.active_paper_id ?? null, label: blockId, status: "resolved", page });
          }
        }}
      />
    );
  }

  if (loading) return <main className="workbench-shell"><p>正在加载会话…</p></main>;

  return (
    <main className={`workbench-shell ${current?.paper_panel_open ? "workbench-split" : ""}`}>
      <header className="workbench-topbar">
        <div className="workbench-brand"><span className="workbench-brand-mark">R</span><span>Research Pulse</span></div>
        <nav aria-label="一级空间" className="workbench-space-nav">
          <button className="workbench-space-active">工作台</button>
        </nav>
      </header>
      <div className="workbench-body">
        <aside className="workbench-history" aria-label="历史会话">
          <div className="workbench-history-head">
            <div><span className="workbench-kicker">Research sessions</span><h1>历史会话</h1></div>
            <button className="workbench-new-session" onClick={() => void createSession()} aria-label="新建会话">+</button>
          </div>
          <div className="workbench-history-tabs" role="tablist" aria-label="工作台左侧内容">
            <button type="button" role="tab" aria-selected={leftRailTab === "workspace"} className={leftRailTab === "workspace" ? "active" : ""} onClick={() => setLeftRailTab("workspace")}>工作区</button>
            <button type="button" role="tab" aria-selected={leftRailTab === "trace"} className={leftRailTab === "trace" ? "active" : ""} onClick={() => setLeftRailTab("trace")}>运行轨迹</button>
          </div>
          {leftRailTab === "workspace" ? (
            <div className="workbench-session-list">
              {workspaceGroups.map((group) => (
                <details key={group.workspaceId} className="workbench-workspace-group" open>
                  <summary className="workbench-workspace-head">
                    <span className="workbench-chevron" aria-hidden="true">▸</span>
                    <span className="workbench-workspace-title">{group.title}</span>
                    <span className="workbench-workspace-actions">
                      <button type="button" className="workbench-ws-btn" aria-label={`在 ${group.title} 中新建会话`} title="新建会话" onClick={(event) => { event.preventDefault(); event.stopPropagation(); void createSession(group.workspaceId); }}>＋</button>
                      <button type="button" className="workbench-ws-btn" aria-label={`重命名工作区：${group.title}`} title="重命名工作区" onClick={(event) => { event.preventDefault(); event.stopPropagation(); void renameWorkspace(group.workspaceId, group.title); }}>✎</button>
                      <button type="button" className="workbench-ws-btn workbench-ws-danger" aria-label={`删除工作区：${group.title}`} title="删除工作区" onClick={(event) => { event.preventDefault(); event.stopPropagation(); deleteWorkspace(group.workspaceId, group.title); }}>✕</button>
                    </span>
                  </summary>
                  <div className="workbench-workspace-sessions">
                    {group.items.map((session) => (
                      <div
                        key={session.session_id}
                        className={`workbench-session-item ${session.session_id === current?.session_id ? "active" : ""}`}
                        onContextMenu={(event) => {
                          event.preventDefault();
                          event.stopPropagation();
                          setSessionContextMenu({
                            sessionId: session.session_id,
                            x: Math.min(event.clientX, window.innerWidth - 170),
                            y: Math.min(event.clientY, window.innerHeight - 96),
                          });
                        }}
                      >
                        <button className="workbench-session-open" aria-label={`打开会话：${session.title}`} onClick={() => void openSession(session.session_id)}>
                          <strong>{session.title}</strong>
                        </button>
                      </div>
                    ))}
                  </div>
                </details>
              ))}
              {!sessions.length && <p className="workbench-empty-history">创建一个会话，开始一次论文研究。</p>}
            </div>
          ) : <WorkbenchRunTrace runs={current?.exploration_runs ?? []} />}
        </aside>

        {!current ? (
          <section className="workbench-empty-state" aria-label="空工作台">
            <span className="workbench-kicker">Paper context workspace</span>
            <h2>从一个研究议题开始</h2>
            <p>新建会话后，可以在对话中逐步形成具体问题，并按需打开论文和研究文档。</p>
            {chatError && <p className="workbench-error" role="alert">{chatError}</p>}
            <button className="workbench-primary" onClick={() => void createSession()}>新建会话</button>
          </section>
        ) : (
          <section
            className={`workbench-session-view ${current.paper_panel_open ? "with-paper" : !activeWorkspaceDocument ? "with-overview" : ""}`}
            aria-label="当前会话"
            style={(railWidth != null || overviewWidth != null) ? ({
              ...(railWidth != null ? { "--rail-width": `${railWidth}px` } : {}),
              ...(overviewWidth != null ? { "--overview-width": `${overviewWidth}px` } : {}),
            } as React.CSSProperties) : undefined}
          >
            <div className="workbench-chat-pane">
              <div className="workbench-chat-scroll" role="region" aria-label="会话内容">
              <div className="workbench-session-heading">
                <span className="workbench-kicker">Session · {current.session_id}</span>
                <div className="workbench-title-row"><h2>{current.title}</h2></div>
                <div className="workbench-session-subline">
                  <p>持续记录研究议题；论文和研究文档按需打开。</p>
                  {workspaceError && <p className="workbench-error" role="alert">{workspaceError}</p>}
                </div>
              </div>
              {activeWorkspaceDocument ? (
                (() => {
                  const document = workspaceDocuments.find((item) => item.document_id === activeWorkspaceDocument);
                  return document ? (
                    <WorkspaceDocumentView
                      document={document}
                      documents={workspaceDocuments}
                      workspace={workspace}
                      onBack={() => setActiveWorkspaceDocument(null)}
                      onSelect={setActiveWorkspaceDocument}
                      onSetFocus={(questionId) => void setWorkspaceFocus(questionId)}
                    />
                  ) : <p className="workbench-muted">研究文档正在准备…</p>;
                })()
              ) : <>
              {!current.papers.length && (
                <div className="workbench-no-paper" role="status" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); void attachLocalPdf(event.dataTransfer.files?.[0]); }}>
                  <strong>当前没有论文上下文</strong>
                  <span>上传自己的 PDF；每个会话在 V1 中添加一篇。</span>
                  <div className="workbench-paper-inputs">
                    <label className="workbench-secondary workbench-file-label">上传本地 PDF<input aria-label="上传本地 PDF" type="file" accept="application/pdf,.pdf" onChange={(event) => void attachLocalPdf(event.target.files?.[0])} /></label>
                    {client.addFixturePaper && <button className="workbench-demo-link" onClick={() => void addDemoPaper()}>添加演示论文</button>}
                  </div>
                  {paperInputError && <span className="workbench-input-error" role="alert">{paperInputError}</span>}
                </div>
              )}
              {current.papers.length > 0 && !current.paper_panel_open && (
                <button className="workbench-paper-reopen" onClick={() => void togglePaper(true)} aria-label="打开论文">打开论文上下文 →</button>
              )}
              <div className="workbench-message-list" aria-label="会话消息">
                {current.history_error && <p className="workbench-error" role="alert" aria-label="会话历史加载失败">{current.history_error}</p>}
                {workspace && workspaceDocuments.length > 0 && (
                  workspace.workspace_revision > 1
                  || workspace.research_map.length > 0
                  || workspace.subquestions.length > 0
                  || workspace.evidence.length > 0
                  || Boolean(workspace.active_focus_id)
                ) && (
                  <section className="workbench-artifact-card" aria-label="研究进展已更新">
                    <div>
                      <span className="workbench-kicker">研究工作区</span>
                      <strong>研究进展已更新</strong>
                      <p>研究议题、当前焦点和证据已整理成文档，需要时再打开查看。</p>
                    </div>
                    <button type="button" onClick={() => openWorkspaceDocument("current-progress")}>打开研究进展</button>
                  </section>
                )}
                {conversation.map((item) => renderConversationItem(item))}
                {pendingSelectionQuestion && <>
                  <article className="workbench-message user pending" aria-label="选区问题已发送">
                    <span>你</span>
                    <p>{pendingSelectionQuestion.text}</p>
                  </article>
                  <article className="workbench-message assistant pending" aria-live="polite" aria-label="选区问题处理中">
                    <span>Research Pulse</span>
                    <p>正在回答当前选区…</p>
                  </article>
                </>}
                {pendingEvidenceAction && <>
                  <article className="workbench-message user pending" aria-label="选区请求已发送">
                    <span>你</span>
                    <p>{displayUserMessage(pendingEvidenceAction.prompt)}</p>
                  </article>
                  <article className="workbench-message assistant pending" aria-live="polite" aria-label="选区请求处理中">
                    <span>Research Pulse</span>
                    <p>{pendingEvidenceAction.action === "translate" ? "正在翻译当前选区…" : "正在解释当前选区…"}</p>
                  </article>
                </>}
              </div>
              </>}
              </div>
              <div className="workbench-chat-footer">
                {selectionContext && <div className="workbench-context-chip active" aria-label="当前选区上下文">
                  <span>当前选区：{selectionContext.label}</span>
                  <button type="button" onClick={() => setSelectionContext(null)} aria-label="移除当前选区">×</button>
                </div>}
                <form className="workbench-composer" aria-label="对话输入区" onSubmit={(event) => void submitQuestion(event)}>
                  <button type="button" className="workbench-add-paper" aria-label="添加论文" title="添加论文" onClick={() => paperFileInputRef.current?.click()}>＋</button>
                  <input ref={paperFileInputRef} aria-label="添加论文文件" type="file" accept="application/pdf,.pdf" hidden onChange={(event) => void attachLocalPdf(event.target.files?.[0])} />
                  <span className="workbench-context-mode" aria-label="自动上下文">自动上下文</span>
                  <label className="workbench-answer-mode">
                    <span>回答方式</span>
                    <select aria-label="回答方式" value={answerMode} onChange={(event) => setAnswerMode(event.target.value as AnswerMode)}>
                      {ANSWER_MODE_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                    </select>
                  </label>
                  <select aria-label="上下文范围" value={contextScope} onChange={(event) => setContextScope(event.target.value as typeof contextScope)} hidden>
                    <option value="none">无论文</option><option value="selection">选区</option><option value="section">章节</option><option value="full">全文</option>
                  </select>
                  <textarea
                    ref={composerRef}
                    value={query}
                    onChange={(event) => {
                      const nextQuery = event.target.value;
                      setQuery(nextQuery);
                      // The CTA arms a single research turn.  If the user
                      // replaces the generated prompt entirely, do not let
                      // the stale action change the meaning of that new text.
                      if (pendingTurnAction && !/^请围绕当前研究焦点继续深入研究[:：]/.test(nextQuery)) {
                        setPendingTurnAction(null);
                      }
                    }}
                    placeholder={workspace?.active_focus_id ? "围绕当前研究焦点提问…" : "向研究助理提问…"}
                    rows={3}
                  />
                  <button className="workbench-secondary" disabled={!query.trim() || chatBusy}>{chatBusy ? "研究中…" : "发送"}</button>
                </form>
                {chatError && <p className="workbench-error" role="alert">{chatError}</p>}
              </div>
            </div>

            {current.paper_panel_open && <div className="workbench-resizer" role="separator" aria-orientation="vertical" aria-label="调整右侧面板宽度" onMouseDown={startResize} />}

            {current.paper_panel_open && current.papers.length > 0 && (
              <aside ref={railRef} className="workbench-paper-pane" aria-label="论文上下文" role="region">
                <header>
                  <div><h3>{readerPaper?.title ?? "论文上下文"}</h3></div>
                  <button className="workbench-close-paper" onClick={() => void togglePaper(false)} aria-label="关闭论文">×</button>
                </header>
                {!readerPaper && <p className="workbench-paper-hint">点击一篇论文，打开阅读区域。</p>}
                {readerPaper && <>
                {client.addFixturePaper && <button className="workbench-select-demo" onClick={() => {
                  if (client.addFixturePaper) {
                    const paper = readerPaper;
                    if (!paper.sample_block_id) return;
                    const blockText = paper.selection_blocks?.find((block) => block.block_id === paper.sample_block_id)?.text ?? "";
                    setSelectionContext({ paper_id: paper.paper_id, scope: "selection", block_id: paper.sample_block_id, block_ids: [paper.sample_block_id], section_path: paper.sample_section_path ?? [], label: `[${paper.sample_block_id}] 论文摘要选区`, text: blockText, status: "attached", page: paper.sample_page ?? undefined, bbox: { x: 0.08, y: 0.12, width: 0.84, height: 0.12 } });
                    setContextScope("selection");
                    return;
                  }
                  if (pendingSelectionText) acceptPdfSelection(pendingSelectionText, pendingSelectionPage, undefined, pendingSelectionRects, pendingSelectionPageSize);
                  else setChatError("请先在 PDF 文本层拖选文字，再点击此按钮");
                }}>选择摘要并询问</button>}
                <div className={`workbench-note-status ${readerPaper.note_status}`} role="status" aria-label="笔记生成状态">
                  {readerPaper.note_status === "idle" && readerPaper.preparation_status === "ready" && <button onClick={() => void startNoteRun()}>生成论文笔记</button>}
                  {readerPaper.note_status === "idle" && readerPaper.preparation_status !== "ready" && <span>论文解析完成后可生成笔记</span>}
                  {readerPaper.note_status === "generating" && <details open className="workbench-process-details"><summary>正在生成论文笔记 · {readerPaper.note_stage === "reading_material" ? "读取材料" : readerPaper.note_stage === "generating_note" ? "模型生成" : readerPaper.note_stage ?? "处理中"}</summary><ul><li>读取论文结构与证据块</li><li>调用阅读模型生成笔记</li><li>保存论文笔记并等待确认</li></ul></details>}
                  {readerPaper.note_status === "awaiting_approval" && <><span>论文笔记草稿已完成，等待发布确认</span><button onClick={() => { const run = (current?.note_runs ?? []).find((item) => item.paper_id === readerPaper.paper_id && item.status === "awaiting_approval"); if (run) void publishNoteRun(run.note_run_id); }}>确认发布</button></>}
                  {readerPaper.note_status === "failed" && <><span className="workbench-note-error" role="alert">笔记生成失败，可重试{readerPaper.note_error ? `：${readerPaper.note_error}` : ""}</span><button onClick={() => void startNoteRun()}>重新生成笔记</button></>}
                  {readerPaper.note_status === "published" && <><span>笔记生成完成</span><button onClick={() => readerPaper.note_url && onOpenKnowledge?.(readerPaper.note_url)}>打开论文笔记</button></>}
                  {readerPaper.note_status !== "idle" && (readerPaper.note_events?.length ?? 0) > 0 && (
                    <details className="workbench-note-trace" open={readerPaper.note_status === "generating"}>
                      <summary>生成链路明细{readerPaper.note_elapsed_ms ? ` · ${(readerPaper.note_elapsed_ms / 1000).toFixed(1)}s` : ""}</summary>
                      <ul>{(readerPaper.note_events ?? []).map((event) => <li key={event.sequence_no}>{event.summary}</li>)}</ul>
                      {(readerPaper.note_tool_uses?.length ?? 0) > 0 && <div className="workbench-note-trace-line">调用：{(readerPaper.note_tool_uses ?? []).map((tool) => `${tool.name}×${tool.calls}`).join("、")}</div>}
                      {readerPaper.note_token_usage && <div className="workbench-note-trace-line">Token：{readerPaper.note_token_usage.input_tokens} 入 / {readerPaper.note_token_usage.output_tokens} 出</div>}
                    </details>
                  )}
                </div>
                {readerPaper.preparation_status === "failed" && <button className="workbench-secondary" onClick={() => client.retryPaper && void client.retryPaper(current.session_id, readerPaper.paper_id).then(applyUpdatedSession)}>重新解析论文</button>}
                {readerPaper.preparation_status !== "ready" && readerPaper.preparation_status !== "failed" && <p className="workbench-paper-preparing" role="status">正在准备论文证据块，完成后即可使用选区提问…</p>}
                <div className="workbench-paper-scroll" role="region" aria-label="论文阅读区">
                  <PdfViewer url={readerPaper.pdf_url} onSelection={(selection) => acceptPdfSelection(selection.text, selection.page, selection.bbox, selection.rects, selection.pageSize)} onSelectionAction={(action, selection) => void handlePdfSelectionAction(action, selection)} onTextSelected={(selection) => { setPendingSelectionText(selection.text); setPendingSelectionPage(selection.page); setPendingSelectionRects(selection.rects ?? []); setPendingSelectionPageSize(selection.pageSize); if (selection.bbox) setSelectionContext((currentContext) => currentContext ? { ...currentContext, bbox: selection.bbox, rects: selection.rects, page_size: selection.pageSize } : currentContext); }} highlight={citationHighlight} highlightPage={citationHighlightPage} focusPage={citationPaperId === readerPaper.paper_id ? citationHighlightPage : undefined} />
                </div>
                </>}
              </aside>
            )}

            {!current.paper_panel_open && !activeWorkspaceDocument && (
              <>
              <div className="workbench-resizer" role="separator" aria-orientation="vertical" aria-label="调整研究概览宽度" onMouseDown={(event) => startResize(event, setOverviewWidth, overviewWidth ?? 360, 280, 720)} />
              <WorkspaceOverviewRail
                workspace={workspace}
                documents={workspaceDocuments}
                papers={current.papers}
                explorationRuns={current.exploration_runs ?? []}
                onOpenKnowledge={onOpenKnowledge}
                onOpenDocument={openWorkspaceDocument}
                onOpenPaper={(paperId) => void openPaper(paperId)}
                onAddPaperCandidate={(source) => void addExternalCandidate(source)}
                onSetFocus={(questionId) => void setWorkspaceFocus(questionId)}
                onContinueFocus={(questionId) => void prepareNextResearch(questionId)}
                onReload={() => void refreshWorkspace()}
                focusBusy={focusBusy}
              />
              </>
            )}
          </section>
        )}
      </div>
      {sessionContextMenu && (() => {
        const target = sessions.find((session) => session.session_id === sessionContextMenu.sessionId);
        if (!target) return null;
        return (
          <div
            className="workbench-session-context-menu"
            role="menu"
            aria-label={`会话操作：${target.title}`}
            style={{ left: sessionContextMenu.x, top: sessionContextMenu.y }}
            onClick={(event) => event.stopPropagation()}
          >
            <button type="button" role="menuitem" onClick={() => { setSessionContextMenu(null); setSessionRename({ sessionId: target.session_id, initial: target.title }); }}>重命名</button>
            <button type="button" role="menuitem" className="danger" onClick={() => { setSessionContextMenu(null); void deleteSession(target.session_id); }}>删除</button>
          </div>
        );
      })()}
      {confirm && (
        <ConfirmDialog message={confirm.message} dangerLabel={confirm.dangerLabel} onConfirm={confirm.onConfirm} onCancel={() => setConfirm(null)} />
      )}
      {sessionRename && (
        <TextPromptDialog label="重命名会话" initial={sessionRename.initial} onOk={(title) => void renameSessionById(sessionRename.sessionId, title)} onCancel={() => setSessionRename(null)} />
      )}
      {workspaceRename && (
        <TextPromptDialog label="重命名工作区" initial={workspaceRename.initial} onOk={(title) => void doRenameWorkspace(workspaceRename.workspaceId, title)} onCancel={() => setWorkspaceRename(null)} />
      )}
    </main>
  );
}

function ConfirmDialog({ message, dangerLabel, onConfirm, onCancel }: { message: string; dangerLabel: string; onConfirm: () => void; onCancel: () => void }) {
  return (
    <div className="workbench-modal-backdrop" role="dialog" aria-modal="true" aria-label={message}>
      <div className="workbench-modal">
        <p className="workbench-modal-message">{message}</p>
        <div className="workbench-modal-actions">
          <button className="workbench-modal-cancel" onClick={onCancel}>取消</button>
          <button className="workbench-modal-danger" onClick={onConfirm}>{dangerLabel}</button>
        </div>
      </div>
    </div>
  );
}

function TextPromptDialog({ label, initial, onOk, onCancel }: { label: string; initial: string; onOk: (value: string) => void; onCancel: () => void }) {
  const [value, setValue] = useState(initial);
  return (
    <div className="workbench-modal-backdrop" role="dialog" aria-modal="true" aria-label={label}>
      <div className="workbench-modal">
        <label className="workbench-modal-label">{label}</label>
        <input
          className="workbench-modal-input"
          value={value}
          placeholder={label}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => { if (event.key === "Enter" && value.trim()) onOk(value.trim()); }}
          autoFocus
        />
        <div className="workbench-modal-actions">
          <button className="workbench-modal-cancel" onClick={onCancel}>取消</button>
          <button className="workbench-modal-primary" disabled={!value.trim()} onClick={() => onOk(value.trim())}>确定</button>
        </div>
      </div>
    </div>
  );
}
