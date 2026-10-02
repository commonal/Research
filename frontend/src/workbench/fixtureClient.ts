import type { KnowledgeWorkbenchStatus, PaperContextRef, WorkbenchAttempt, WorkbenchClient, WorkbenchMessage, WorkbenchNoteRun, WorkbenchPaper, WorkbenchSession, WorkbenchWorkspaceState, WorkspaceDocument } from "./types";
import { projectDraftCitations } from "./citationProjection";

const now = () => new Date().toISOString();

function clone<T>(value: T): T {
  return structuredClone(value);
}

function fixturePaper(paperId: string): WorkbenchPaper {
  return {
    paper_id: paperId,
    title: "A demonstrative paper for the reading workspace",
    source_url: "https://example.test/paper.pdf",
    pdf_url: "/fixtures/paper-demo.pdf",
    preparation_status: "ready",
    parse_error: null,
    note_status: "idle",
    note_url: null,
    sample_block_id: "B1",
    sample_section_path: ["Abstract"],
    sample_page: 1,
    selection_blocks: [{ block_id: "B1", text: "We propose a memory-augmented lifelong learning framework that consolidates episodic memories into a scalable parametric store.", section_path: ["Abstract"], page: 1 }],
  };
}

type FixtureClientOptions = { noteOutcome?: "published" | "failed"; noteDelayMs?: number };

export function createFixtureWorkbenchClient(options: FixtureClientOptions = {}): WorkbenchClient & {
  addFixturePaper(sessionId: string, paperId: string): Promise<WorkbenchSession>;
  addFixtureMessage(sessionId: string, text: string, context?: PaperContextRef): Promise<WorkbenchSession>;
  startFixtureNoteRun(sessionId: string, paperId: string): Promise<WorkbenchSession>;
  attachPrototypePaper(sessionId: string, paper: { title: string; pdf_url: string; source_url?: string | null }): Promise<WorkbenchSession>;
} {
  const sessions = new Map<string, WorkbenchSession>();
  const workspaces = new Map<string, WorkbenchWorkspaceState>();
  const workspaceTitles = new Map<string, string>();
  let nextId = 1;

  const titleFor = (workspaceId: string | null | undefined): string =>
    (workspaceId && workspaceTitles.get(workspaceId)) || "研究项目";

  const hydrate = (session: WorkbenchSession): WorkbenchSession => {
    const copy = clone(session);
    copy.workspace_title = titleFor(copy.workspace_id);
    return copy;
  };

  const read = (sessionId: string): WorkbenchSession => {
    const session = sessions.get(sessionId);
    if (!session) throw new Error("session not found");
    return hydrate(session);
  };

  const write = (session: WorkbenchSession): WorkbenchSession => {
    session.updated_at = now();
    sessions.set(session.session_id, session);
    return clone(session);
  };

  return {
    async listSessions(includeArchived = false) {
      return [...sessions.values()]
        .filter((session) => includeArchived || session.lifecycle === "active")
        .sort((left, right) => right.updated_at.localeCompare(left.updated_at))
        .map(hydrate);
    },
    async createSession(workspaceId) {
      const session: WorkbenchSession = {
        session_id: `session-${nextId++}`,
        title: "新会话",
        title_source: "default",
        lifecycle: "active",
        workspace_id: workspaceId ?? `ws-${nextId}-${Date.now()}`,
        papers: [],
        active_paper_id: null,
        messages: [],
        note_runs: [],
        exploration_runs: [],
        paper_panel_open: false,
        updated_at: now(),
      };
      write(session);
      return read(session.session_id);
    },
    async createQuestionSession(question) {
      const session = await this.createSession();
      return this.renameSession(session.session_id, question);
    },
    async createKnowledgeSession(knowledgeId, workspaceId) {
      const session = await this.createSession(workspaceId);
      const paperId = knowledgeId.startsWith("kp:arxiv:")
        ? knowledgeId.slice("kp:arxiv:".length)
        : `note-paper-${nextId++}`;
      const updated = await this.addFixturePaper(session.session_id, paperId);
      const stored = read(updated.session_id);
      const paper = stored.papers.find((item) => item.paper_id === paperId);
      if (paper) {
        paper.note_status = "published";
        paper.note_url = knowledgeId;
      }
      return write(stored);
    },
    async getKnowledgePaperStatus(knowledgeId): Promise<KnowledgeWorkbenchStatus> {
      return {
        knowledge_id: knowledgeId,
        status: "available",
        can_import: false,
        paper_id: knowledgeId.startsWith("kp:arxiv:") ? knowledgeId.slice("kp:arxiv:".length) : null,
        title: "A demonstrative paper for the reading workspace",
        source_url: "https://example.test/paper.pdf",
        pdf_status: "ready",
        parse_status: "ready",
      };
    },
    async importKnowledgeSession(knowledgeId, workspaceId) {
      return this.createKnowledgeSession!(knowledgeId, workspaceId);
    },
    async getSession(sessionId) {
      return read(sessionId);
    },
    async renameWorkspace(workspaceId, title) {
      workspaceTitles.set(workspaceId, title);
      return { workspace_id: workspaceId, title };
    },
    async deleteWorkspace(workspaceId) {
      for (const [id, session] of sessions) {
        if (session.workspace_id === workspaceId) sessions.delete(id);
      }
      workspaceTitles.delete(workspaceId);
    },
    async renameSession(sessionId, title) {
      const session = read(sessionId);
      session.title = title.trim() || "新会话";
      session.title_source = "user";
      return write(session);
    },
    async updateLayout(sessionId, paperPanelOpen, activePaperId) {
      const session = read(sessionId);
      session.paper_panel_open = paperPanelOpen;
      if (activePaperId !== undefined) session.active_paper_id = activePaperId;
      else if (paperPanelOpen && !session.active_paper_id) session.active_paper_id = session.papers[0]?.paper_id ?? null;
      return write(session);
    },
    async archiveSession(sessionId) {
      const session = read(sessionId);
      session.lifecycle = "archived";
      return write(session);
    },
    async deleteSession(sessionId) {
      sessions.delete(sessionId);
    },
    async addFixturePaper(sessionId, paperId) {
      const session = read(sessionId);
      if (!session.papers.some((paper) => paper.paper_id === paperId)) {
        session.papers.push(fixturePaper(paperId));
      }
      session.active_paper_id = paperId;
      session.paper_panel_open = true;
      return write(session);
    },
    async attachPrototypePaper(sessionId, input) {
      const session = read(sessionId);
      if (session.papers.length) throw new Error("V1 每个会话只能添加一篇论文");
      const paperId = `prototype-paper-${nextId++}`;
      session.papers.push({
        paper_id: paperId,
        title: input.title,
        source_url: input.source_url ?? null,
        pdf_url: input.pdf_url,
        preparation_status: "ready",
        parse_error: null,
        note_status: "idle",
        note_url: null,
      });
      session.active_paper_id = paperId;
      session.paper_panel_open = true;
      return write(session);
    },
    async uploadPdf(sessionId, file) {
      return this.attachPrototypePaper!(sessionId, { title: file.name, pdf_url: URL.createObjectURL(file) });
    },
    async addPaperUrl(sessionId, url) {
      const parsed = new URL(url);
      const title = decodeURIComponent(parsed.pathname.split("/").filter(Boolean).at(-1) ?? parsed.hostname);
      return this.attachPrototypePaper!(sessionId, { title, pdf_url: url, source_url: url });
    },
    async retryPaper(sessionId) { return read(sessionId); },
    async addFixtureMessage(sessionId, text, context) {
      const session = read(sessionId);
      const createdAt = now();
      const userMessage: WorkbenchMessage = {
        message_id: `${sessionId}-message-${session.messages.length + 1}`,
        role: "user",
        text,
        generation_status: "completed",
        contexts: context ? [clone(context)] : [],
        citations: [],
        created_at: createdAt,
      };
      const answer = context ? "这段内容说明了论文的核心方法；下面的结论仅基于当前选区。 [B1] 演示中的越界引用会保持未解析。 [B999]" : "这是一个无论文上下文的演示回答，请先添加论文再进行证据问答。";
      const sentBlocks = context?.block_id && context.page ? (context.block_ids ?? [context.block_id]).map((blockId) => ({
        block_id: blockId,
        label: context.label,
        page: context.page!,
        bbox: context.bbox,
      })) : [];
      const assistantMessage: WorkbenchMessage = {
        message_id: `${sessionId}-message-${session.messages.length + 2}`,
        role: "assistant",
        text: answer,
        generation_status: "completed",
        contexts: [],
        citations: projectDraftCitations(answer, sentBlocks),
        created_at: now(),
      };
      session.messages.push(userMessage, assistantMessage);
      return write(session);
    },
    async startFixtureExploration(sessionId, question, profile, interactionContext) {
      const session = read(sessionId);
      const ordinal = (session.exploration_runs?.length ?? 0) + 1;
      const runId = `${sessionId}-explore-${ordinal}`;
      const runProfile = profile ?? "literature";
      const attempt: WorkbenchAttempt = {
        attempt_id: `${runId}-attempt-1`,
        attempt_no: 1,
        status: "running",
        generation: 1,
        budgets: { tool_calls: 16, block_reads: 24 },
        budget_used: { tool_calls: 2, block_reads: 1 },
        failure: null,
      };
      session.exploration_runs = [...(session.exploration_runs ?? []), {
        run_id: runId,
        current_attempt_id: attempt.attempt_id,
        current_attempt: attempt,
        attempt_history: [attempt],
        budget: { current: { ...attempt.budget_used }, cumulative: { ...attempt.budget_used } },
        question,
        attempt: 1,
        status: "running",
        created_at: now(),
        phase: runProfile === "assistant" ? "规划研究步骤" : "检索候选来源",
        budget_used: { tool_calls: 2, tool_limit: 16, block_reads: 1, block_limit: 24 },
        tools_used: [{ name: "search_sources", calls: 1 }],
        tool_details: [{
          tool_call_id: "call-search-1",
          tool_name: "search_sources",
          status: "rejected",
          retryability: "after_correction",
          side_effect_state: "none",
          error_code: "invalid_argument",
          safe_message: "工具参数或前置条件无效，请修正后重试",
          diagnostic_id: "diag-demo-1",
          recommended_action: "correct_parameters",
        }],
        token_usage: { input_tokens: 420, output_tokens: 180 },
        sources: [{ source_id: "paper-demo", title: "A demonstrative paper" }],
        partial_result: "已发现一条待验证的方法路线。",
        events: [
          { sequence_no: 1, event_type: "run_started", summary: "探索已开始", stable_ids: { run_id: runId }, counters: {} },
          { sequence_no: 2, event_type: "tool_started", summary: "调用只读工具 search_sources", stable_ids: { tool_name: "search_sources" }, counters: { tool_calls: 1 } },
          { sequence_no: 3, event_type: "source_discovered", summary: "发现受管来源", stable_ids: { source_id: "paper-demo" }, counters: { tool_calls: 2 } },
        ],
        profile: runProfile,
        continuation_mode: "persisted_results",
        continuation_label: "基于已有结果继续执行",
      }];
      return write(session);
    },
    async sendTurn(sessionId, text, options = {}) {
      if ((options.action === "translate" || options.action === "explain") && options.context) {
        return this.addFixtureMessage!(sessionId, text, options.context);
      }
      return this.startFixtureExploration!(
        sessionId,
        text,
        options.profile ?? "assistant",
        options.interactionContext,
      );
    },
    async cancelFixtureExploration(sessionId, runId) {
      const session = read(sessionId);
      const run = session.exploration_runs?.find((item) => item.run_id === runId);
      if (!run) throw new Error("exploration not found");
      if (run.current_attempt) {
        // Terminal attempts stay immutable: the cancelled record is preserved
        // in attempt_history, never reopened by a later continue.
        run.current_attempt = { ...run.current_attempt, status: "cancelled" };
        run.attempt_history = (run.attempt_history ?? []).map((item) =>
          item.attempt_id === run.current_attempt!.attempt_id ? run.current_attempt! : item
        );
      }
      run.status = "cancelled";
      run.phase = "已停止";
      run.events = [...(run.events ?? []), {
        sequence_no: (run.events?.length ?? 0) + 1,
        event_type: "run_cancelled",
        summary: "探索已停止",
        stable_ids: {},
        counters: {},
      }];
      return write(session);
    },
    async continueExploration(sessionId, runId) {
      const session = read(sessionId);
      const run = session.exploration_runs?.find((item) => item.run_id === runId);
      if (!run) throw new Error("exploration not found");
      if (!["cancelled", "budget_exhausted", "retryable_failure", "abandoned", "failed"].includes(run.status)) {
        throw new Error("当前 Attempt 仍在执行，无法继续");
      }
      // Continuing keeps the same run_id and appends a NEW attempt. The
      // previous attempt keeps its terminal state; per-attempt budgets reset
      // while the run-level view keeps the cumulative totals.
      const previous = run.current_attempt ?? null;
      const previousUsed = previous?.budget_used ?? {};
      const nextNo = (previous?.attempt_no ?? run.attempt) + 1;
      const nextAttempt: WorkbenchAttempt = {
        attempt_id: `${run.run_id}-attempt-${nextNo}`,
        attempt_no: nextNo,
        status: "running",
        generation: 1,
        budgets: { tool_calls: 16, block_reads: 24 },
        budget_used: { tool_calls: 0, block_reads: 0 },
        failure: null,
      };
      const cumulative: Record<string, number> = { ...(run.budget?.cumulative ?? previousUsed) };
      for (const [key, value] of Object.entries(previousUsed)) {
        cumulative[key] = (cumulative[key] ?? 0) + value;
      }
      run.current_attempt_id = nextAttempt.attempt_id;
      run.current_attempt = nextAttempt;
      run.attempt_history = [...(run.attempt_history ?? []), nextAttempt];
      run.budget = { current: { ...nextAttempt.budget_used }, cumulative };
      run.attempt = nextNo;
      run.status = "running";
      run.phase = "基于已有结果继续执行";
      run.budget_used = { tool_calls: 0, tool_limit: 16, block_reads: 0, block_limit: 24 };
      run.tools_used = [];
      run.token_usage = { input_tokens: 0, output_tokens: 0 };
      run.events = [
        { sequence_no: 1, event_type: "continuation_started", summary: "基于已有结果继续执行：继承已持久化的来源与草稿", stable_ids: { run_id: run.run_id, attempt_id: nextAttempt.attempt_id }, counters: {} },
        { sequence_no: 2, event_type: "phase_changed", summary: "重新规划研究步骤", stable_ids: {}, counters: {} },
      ];
      return write(session);
    },
    async startFixtureNoteRun(sessionId, paperId) {
      const session = read(sessionId);
      const paper = session.papers.find((candidate) => candidate.paper_id === paperId);
      if (!paper) throw new Error("paper not found");
      const existing = (session.note_runs ?? []).find((run) => run.paper_id === paperId && ["queued", "generating", "awaiting_approval"].includes(run.status));
      if (existing) return write(session);
      const runId = `${sessionId}-note-${(session.note_runs?.length ?? 0) + 1}`;
      const run: WorkbenchNoteRun = {
        note_run_id: runId,
        paper_id: paperId,
        triggering_session_id: sessionId,
        status: "generating",
        attempt: 1,
        stage: "generating_note",
          created_at: now(),
          draft_available: false,
          draft_preview: null,
          can_publish: false,
        events: [
          { sequence_no: 1, event_type: "run_started", summary: "论文笔记生成已开始", stable_ids: {}, counters: {} },
          { sequence_no: 2, event_type: "phase_changed", summary: "读取论文结构与证据块", stable_ids: { stage: "reading_material" }, counters: {} },
          { sequence_no: 3, event_type: "phase_changed", summary: "调用阅读模型生成笔记", stable_ids: { stage: "generating_note" }, counters: {} },
        ],
        tool_uses: [{ name: "read_paper", calls: 1 }],
        token_usage: { input_tokens: 420, output_tokens: 180 },
        elapsed_ms: 0,
      };
      session.note_runs = [...(session.note_runs ?? []), run];
      paper.note_status = "generating";
      paper.note_url = null;
      write(session);
      const delay = options.noteDelayMs ?? 250;
      setTimeout(() => {
        const current = sessions.get(sessionId);
        const currentRun = current?.note_runs?.find((item) => item.note_run_id === runId);
        const currentPaper = current?.papers.find((item) => item.paper_id === paperId);
        if (!current || !currentRun || !currentPaper) return;
        if (options.noteOutcome === "failed") {
          currentRun.status = "failed";
          currentRun.stage = "failed";
          currentRun.safe_error = "阅读模型生成失败，可重试";
          currentRun.events = [...currentRun.events, { sequence_no: currentRun.events.length + 1, event_type: "note_failed", summary: "论文笔记生成失败，可重试", stable_ids: {}, counters: {} }];
          currentPaper.note_status = "failed";
        } else {
          currentRun.status = "awaiting_approval";
          currentRun.stage = "awaiting_approval";
          currentRun.draft_available = true;
          currentRun.draft_preview = "## 论文要点\n\n这份笔记会在发布前保留为草稿，正文将基于已解析的论文材料生成。\n\n### 方法与证据\n\n生成过程会记录可回看的证据来源。";
          currentRun.can_publish = true;
          currentRun.events = [...currentRun.events, { sequence_no: currentRun.events.length + 1, event_type: "note_ready", summary: "论文笔记草稿已生成，等待你确认发布", stable_ids: { stage: "awaiting_approval" }, counters: {} }];
          currentPaper.note_status = "awaiting_approval";
        }
        write(current);
      }, Math.max(0, delay));
      return write(session);
    },
    async publishNoteRun(noteRunId) {
      for (const session of sessions.values()) {
        const run = session.note_runs?.find((item) => item.note_run_id === noteRunId);
        if (!run) continue;
        if (run.status !== "awaiting_approval") throw new Error("only a note draft awaiting approval can be published");
        run.status = "published";
        run.stage = "published";
        run.can_publish = false;
        run.knowledge_id = "kp:arxiv:2608.16447v1";
        run.events = [...run.events, { sequence_no: run.events.length + 1, event_type: "note_completed", summary: "论文笔记已发布并写入知识库", stable_ids: { knowledge_id: run.knowledge_id }, counters: {} }];
        const paper = session.papers.find((item) => item.paper_id === run.paper_id);
        if (paper) { paper.note_status = "published"; paper.note_url = run.knowledge_id; }
        return write(session);
      }
      throw new Error("note run not found");
    },
    async fetchWorkspace(sessionId) {
      const session = sessions.get(sessionId);
      if (!session) throw new Error("session not found");
      let workspace = workspaces.get(sessionId);
      if (!workspace) {
        workspace = {
          workspace_id: `ws-${sessionId}`,
          research_question: session.title && session.title !== "新会话" ? session.title : "如何降低推荐位置偏差？",
          research_intent: "探索如何降低推荐系统中的位置偏差",
          active_focus_id: null,
          anchor_paper_id: session.papers[0]?.paper_id ?? "paper-x",
          status: "waiting_for_user_action",
          workspace_revision: 3,
          research_map: [{ node_id: "M-001", label: "DPO 路线", related_question_ids: ["Q-001"], evidence_ids: ["E-001"] }],
          subquestions: [
            { question_id: "Q-001", text: "如何对位置偏差建模？", status: "resolved", answer: "用一个位置无关的修正项估计模型与用户偏好的偏差。", researchability: "unknown" },
            { question_id: "Q-002", text: "哪些数据能标识真实偏好？", status: "open", researchability: "candidate" },
          ],
          evidence: [{
            evidence_id: "E-001", source_id: session.papers[0]?.paper_id ?? "paper-demo", block_ids: ["B1"],
            supports_question_ids: ["Q-001"], evidence_role: "supporting",
            claim: "论文用观测量分解出位置偏差。", research_interpretation: "说明位置偏差可观测、可分离，能作为偏差修正目标。", confidence: "high",
          }],
          research_plan: {
            stage: "method_mapping",
            artifact_stage: "stage_note",
            artifact: {
              stage: "stage_note",
              label: "阶段性研究笔记",
              description: "已有可追溯证据和研究迭代，但还不是最终报告。",
              can_generate_report: true,
              is_final: false,
            },
            iterations: [
              {
                iteration_id: "I-001",
                sequence: 1,
                title: "论文初读：确认位置偏差缺口",
                status: "completed",
                focus_question_id: null,
                run_id: "run-fixture-1",
                summary: "锚点论文说明位置效应可以被分离，但没有验证跨场景稳定性。",
                evidence_ids: ["E-001"],
                candidate_question_ids: ["Q-002"],
                decision: "保留跨场景稳定性作为下一轮候选问题。",
                next_step: "核查哪些数据能够标识真实偏好。",
              },
            ],
            method_map: {
              map_id: "MM-001",
              status: "draft",
              entries: [
                {
                  method_id: "METHOD-001",
                  name: "位置偏差建模",
                  mechanism: "将展示位置与用户偏好信号分离，再把修正项反馈给排序器。",
                  assumptions: "位置效应可以从交互数据中稳定估计。",
                  evidence_ids: ["E-001"],
                  limitations: "还没有确认在不同数据分布和冷启动场景下是否成立。",
                },
              ],
            },
            hypotheses: [
              {
                hypothesis_id: "H-001",
                text: "显式建模位置偏差能够在不牺牲相关性的前提下改善排序公平性。",
                question_id: "Q-002",
                evidence_ids: ["E-001"],
                rationale: "锚点论文给出了可分离的偏差修正信号，但尚未覆盖跨场景稳定性。",
                falsifiers: ["在控制相关性后，偏差修正对公平性指标没有稳定提升。"],
                status: "candidate",
              },
            ],
            critiques: [],
            experiment_plans: [],
          },
          decision_points: [{ decision_id: "D-001", kind: "research_direction", prompt: "已形成初步理解与研究方向地图；请选择继续深入的方向。", status: "pending", created_at: now(), candidate_question_ids: ["Q-002"] }],
        };
        workspaces.set(sessionId, workspace);
      }
      return clone(workspace);
    },
    async fetchWorkspaceDocuments(sessionId) {
      const workspace = await this.fetchWorkspace!(sessionId);
      if (!workspace) return [];
      const active = workspace.subquestions.find((question) => question.question_id === workspace.active_focus_id);
      const candidates = workspace.subquestions.filter((question) => question.researchability === "candidate" && question.status !== "deprioritized");
      const boundaries = workspace.subquestions.filter((question) => question.researchability === "boundary");
      const bullet = (items: string[], empty = "暂无") => items.length ? items.map((item) => `- ${item}`).join("\n") : `- ${empty}`;
      const statusLabel: Record<string, string> = { created: "未开始", initial_research: "初步研究中", waiting_for_user_action: "等待你确认", investigating: "深入研究中", archived: "已归档" };
      const docs: WorkspaceDocument[] = [
        { document_id: "research-brief", title: "研究概览", filename: "research-brief.md", markdown: `# 研究概览\n\n**当前产物：** ${workspace.research_plan?.artifact?.label || "研究概览"}\n**报告状态：** 尚未单独生成最终研究报告。\n\n## 研究议题\n${workspace.research_intent || workspace.research_question}\n\n## 锚点论文\n${workspace.anchor_paper_id}\n\n## 当前研究焦点\n${active?.text || "尚未选择当前研究焦点"}\n` },
        { document_id: "current-progress", title: "当前研究进展", filename: "current-progress.md", markdown: `# 当前研究进展\n\n**状态：** ${statusLabel[workspace.status] || workspace.status}\n\n## 当前焦点\n${active?.text || "尚未选择当前研究焦点。"}\n\n## 研究迭代\n${(workspace.research_plan?.iterations ?? []).map((item) => `- 第 ${item.sequence} 轮：${item.title}（${item.status}）\n  - ${item.summary || "本轮尚未记录阶段结论"}\n  - 下一步：${item.next_step || "待确定"}`).join("\n") || "- 尚未开始多轮研究。"}\n\n## 研究结构\n${bullet(workspace.research_map.map((node) => node.label || node.node_id), "尚未形成研究结构")}\n\n## 候选研究线索\n${bullet(candidates.map((question) => question.text))}\n\n## 论文边界\n${bullet(boundaries.map((question) => question.text))}\n` },
        { document_id: "evidence-index", title: "证据清单", filename: "evidence-index.md", markdown: `# 证据清单\n\n${workspace.evidence.length ? workspace.evidence.map((item) => `## ${item.evidence_id}\n- 角色：${item.evidence_role}\n- 来源：${item.source_id}\n- 证据块：${item.block_ids.join(", ")}\n- 原文判断：${item.claim || "暂无"}\n- 研究解释：${item.research_interpretation || "暂无"}`).join("\n\n") : "- 尚未记录证据"}\n` },
        { document_id: "research-plan", title: "研究计划", filename: "research-plan.md", markdown: (() => {
          const plan = workspace.research_plan;
          const stageLabel: Record<string, string> = { not_started: "尚未开始", method_mapping: "方法路线整理", hypothesis_review: "候选假设评审", experiment_planning: "实验计划设计", ready: "可执行" };
          const artifactLabel: Record<string, string> = { overview: "研究概览", stage_note: "阶段性研究笔记", report_draft: "研究报告草稿", report: "研究报告" };
          const lines = [`# 研究计划`, ``, `**阶段（Stage）：** ${stageLabel[plan?.stage ?? "not_started"] || plan?.stage || "尚未开始"}`, `**产物成熟度：** ${artifactLabel[plan?.artifact_stage ?? "overview"] || "研究概览"}`, ``, `## 研究迭代记录`];
          if (!plan?.iterations?.length) lines.push("尚未记录研究迭代。", "");
          else plan.iterations.forEach((item) => lines.push(`### 第 ${item.sequence} 轮 · ${item.title}`, `- 状态：${item.status}`, `- 阶段结论：${item.summary || "暂无"}`, `- 下一步：${item.next_step || "暂无"}`, ""));
          lines.push(`## 方法路线图（Method Map）`);
          if (!plan?.method_map?.entries.length) lines.push("尚未形成方法路线图。", "");
          else plan.method_map.entries.forEach((entry) => lines.push(`### ${entry.name}（${entry.method_id}）`, `- 机制：${entry.mechanism || "暂无"}`, `- 前提：${entry.assumptions || "暂无"}`, `- 局限：${entry.limitations || "暂无"}`, ""));
          lines.push("## 候选研究假设（Candidate Hypotheses）");
          if (!plan?.hypotheses.length) lines.push("尚未形成可检验的候选假设。", "");
          else plan.hypotheses.forEach((hypothesis) => lines.push(`### ${hypothesis.hypothesis_id}`, hypothesis.text, `- 提出理由：${hypothesis.rationale || "暂无"}`, `- 可能证伪：${hypothesis.falsifiers.join("；") || "暂无"}`, ""));
          lines.push("## 批判与风险（Critique）", plan?.critiques.length ? plan.critiques.map((item) => `- ${item.critique_id}：${item.risks.join("；") || "暂无风险记录"}`).join("\n") : "尚未形成批判记录。", "", "## 实验计划（Experiment Plan）", plan?.experiment_plans.length ? plan.experiment_plans.map((item) => `- ${item.plan_id}：${item.intervention || "尚未填写干预"}`).join("\n") : "尚未形成实验计划。只有候选假设经过批判后，才进入干预、基线和判定标准设计。", "");
          return lines.join("\n");
        })() },
      ];
      return clone(docs);
    },
    async setWorkspaceFocus(sessionId, questionId) {
      const workspace = workspaces.get(sessionId);
      if (!workspace) throw new Error("workspace not found");
      const question = workspace.subquestions.find((item) => item.question_id === questionId);
      if (!question || question.researchability !== "candidate") throw new Error("只能选择可探索的研究线索");
      workspace.active_focus_id = questionId;
      return clone(workspace);
    },
    async resolveWorkspaceDecision(sessionId, decisionId, approved, decision, questionId) {
      const workspace = workspaces.get(sessionId);
      if (!workspace) throw new Error("workspace not found");
      const point = workspace.decision_points?.find((item) => item.decision_id === decisionId);
      if (!point) throw new Error("decision not found");
      point.status = approved ? "approved" : "rejected";
      point.decision = decision ?? (approved ? "approved" : "rejected");
      if (approved && questionId) {
        const question = workspace.subquestions.find((item) => item.question_id === questionId);
        if (!question || question.researchability !== "candidate") throw new Error("只能选择可探索的研究线索");
        workspace.active_focus_id = questionId;
      }
      return { decision: clone(point) };
    },
  };
}
