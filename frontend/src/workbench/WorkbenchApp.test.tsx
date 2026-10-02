import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createFixtureWorkbenchClient } from "./fixtureClient";
import type { InteractionContextPayload } from "./types";
import { citationSummaryLabel, continuationActionLabel, retrievalPlanLabel, WorkbenchApp } from "./WorkbenchApp";

vi.mock("./PdfViewer", () => ({
  PdfViewer: ({ highlight, onSelectionAction, onSelection }: { highlight?: unknown; onSelectionAction?: (action: "translate" | "explain", selection: { text: string; page: number }) => void; onSelection?: (selection: { text: string; page: number }) => void }) => <div aria-label="PDF 预览">
    {highlight ? <i aria-label="引用定位高亮" /> : null}
    <button type="button" onClick={() => onSelectionAction?.("translate", { text: "We propose a memory-augmented lifelong learning framework that consolidates episodic memories into a scalable parametric store.", page: 1 })}>翻译</button>
    <button type="button" onClick={() => onSelectionAction?.("explain", { text: "We propose a memory-augmented lifelong learning framework that consolidates episodic memories into a scalable parametric store.", page: 1 })}>解释</button>
    <button type="button" onClick={() => onSelection?.({ text: "We propose a memory-augmented lifelong learning framework that consolidates episodic memories into a scalable parametric store.", page: 1 })}>提问</button>
  </div>,
}));

describe("session-driven workbench", () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });
  it("describes durable continuation without claiming lossless checkpoint recovery", () => {
    const label = continuationActionLabel({ status: "budget_exhausted" });
    expect(label).toBe("基于已有结果继续执行");
    expect(label).not.toMatch(/断点|无损|checkpoint/i);
  });
  it("labels the selected retrieval plan without exposing harness internals", () => {
    expect(retrievalPlanLabel("direct")).toBe("直接回答");
    expect(retrievalPlanLabel("paper_evidence")).toBe("论文证据");
    expect(retrievalPlanLabel(null)).toBeNull();
  });
  it("distinguishes citation locations from persisted evidence rows", () => {
    expect(citationSummaryLabel(11)).toBe("引用定位 11 处");
  });
  it("shows a history loading failure instead of presenting an empty conversation", async () => {
    const client = createFixtureWorkbenchClient();
    await client.createSession();
    const originalList = client.listSessions.bind(client);
    client.listSessions = async () => (await originalList()).map((session) => ({
      ...session,
      history_error: "探索历史加载失败，请刷新或检查后端日志",
    }));

    render(<WorkbenchApp client={client} />);

    expect(await screen.findByRole("alert", { name: "会话历史加载失败" })).toBeTruthy();
    expect(screen.getByText("探索历史加载失败，请刷新或检查后端日志")).toBeTruthy();
  });
  it("does not stay on a blank loading screen when the session list fails", async () => {
    const client = createFixtureWorkbenchClient();
    client.listSessions = async () => { throw new Error("后端连接失败"); };

    render(<WorkbenchApp client={client} />);

    expect((await screen.findByRole("alert")).textContent).toContain("后端连接失败");
    expect(screen.getAllByRole("button", { name: "新建会话" }).length).toBeGreaterThan(0);
  });
  it("creates sessions from the history sidebar and isolates paper context", async () => {
    const client = createFixtureWorkbenchClient();
    render(<WorkbenchApp client={client} />);

    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    await waitFor(() => expect(screen.getByRole("button", { name: "打开会话：新会话" })).toBeTruthy());
    fireEvent.click(screen.getByLabelText("新建会话"));
    await waitFor(() => expect(screen.getAllByRole("button", { name: "打开会话：新会话" })).toHaveLength(2));

    const sessionButtons = screen.getAllByRole("button", { name: "打开会话：新会话" });
    expect(sessionButtons).toHaveLength(2);
    fireEvent.click(sessionButtons[0]);
    await waitFor(() => expect(screen.getByText(/Session · session-/)).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "添加演示论文" }));
    expect(await screen.findByText("A demonstrative paper for the reading workspace")).toBeTruthy();

    fireEvent.click(sessionButtons[1]);
    await waitFor(() => expect(screen.getByText("当前没有论文上下文")).toBeTruthy());
    expect(screen.queryByText("A demonstrative paper for the reading workspace")).toBeNull();
  });

  it("keeps the paper panel reversible when switching sessions", async () => {
    const client = createFixtureWorkbenchClient();
    render(<WorkbenchApp client={client} />);

    fireEvent.click(await screen.findByLabelText("新建会话"));
    await waitFor(() => expect(screen.getByRole("button", { name: "添加演示论文" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "添加演示论文" }));
    expect(await screen.findByRole("region", { name: "论文上下文" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "关闭论文" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "论文上下文" })).toBeNull());
    expect(screen.getByRole("button", { name: "打开论文" })).toBeTruthy();
  });

  it("opens the first attached paper when the session has no active paper", async () => {
    const client = createFixtureWorkbenchClient();
    const originalAdd = client.addFixturePaper!.bind(client);
    client.addFixturePaper = async (...args) => {
      const session = await originalAdd(...args);
      return { ...session, active_paper_id: null, paper_panel_open: false };
    };
    let selectedPaper: string | null | undefined;
    const originalLayout = client.updateLayout.bind(client);
    client.updateLayout = async (sessionId, open, activePaperId) => {
      selectedPaper = activePaperId;
      return originalLayout(sessionId, open, activePaperId);
    };
    render(<WorkbenchApp client={client} />);
    fireEvent.click(await screen.findByLabelText("新建会话"));
    fireEvent.click(await screen.findByRole("button", { name: "添加演示论文" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "打开论文" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "打开论文" }));
    await waitFor(() => expect(screen.getByRole("region", { name: "论文上下文" })).toBeTruthy());
    expect(selectedPaper).toBe("paper-demo");
  });

  it("opens the session handed off from a paper note", async () => {
    const client = createFixtureWorkbenchClient();
    const handedOff = await client.createKnowledgeSession!("kp:arxiv:2608.16447v1");

    render(<WorkbenchApp client={client} initialSessionId={handedOff.session_id} />);

    expect(await screen.findByRole("region", { name: "论文上下文" })).toBeTruthy();
    expect(screen.getByText("A demonstrative paper for the reading workspace")).toBeTruthy();
  });

  it("groups paper notes and research documents under knowledge assets", async () => {
    const client = createFixtureWorkbenchClient();
    const handedOff = await client.createKnowledgeSession!("kp:arxiv:2608.16447v1");

    render(<WorkbenchApp client={client} initialSessionId={handedOff.session_id} />);
    fireEvent.click(await screen.findByRole("button", { name: "关闭论文" }));

    expect(await screen.findByRole("region", { name: "知识资产" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "打开笔记" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "研究概览" })).toBeTruthy();
  });

  it("keeps conversation input outside the independently scrolling content panes", async () => {
    const client = createFixtureWorkbenchClient();
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    fireEvent.click(await screen.findByRole("button", { name: "添加演示论文" }));
    const chatScroll = await screen.findByRole("region", { name: "会话内容" });
    const paperScroll = screen.getByRole("region", { name: "论文阅读区" });
    const composer = screen.getByRole("form", { name: "对话输入区" });
    expect(chatScroll.contains(composer)).toBe(false);
    expect(paperScroll).toBeTruthy();
  });

  it("supports an explicit session rename", async () => {
    const client = createFixtureWorkbenchClient();
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    const session = await screen.findByRole("button", { name: /打开会话：新会话/ });
    fireEvent.contextMenu(session);
    fireEvent.click(await screen.findByRole("menuitem", { name: "重命名" }));
    const input = screen.getByPlaceholderText("重命名会话");
    fireEvent.change(input, { target: { value: "记忆增强研究" } });
    fireEvent.click(screen.getByRole("button", { name: "确定" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "记忆增强研究" })).toBeTruthy());
    expect(screen.getByRole("button", { name: "打开会话：记忆增强研究" })).toBeTruthy();
  });

  it("starts the research assistant agent with the selection context sent as live interaction context", async () => {
    const client = createFixtureWorkbenchClient();
    const originalStart = client.startFixtureExploration!.bind(client);
    let lastContext: InteractionContextPayload | undefined;
    client.startFixtureExploration = async (sessionId, question, profile, interactionContext) => {
      lastContext = interactionContext;
      return originalStart(sessionId, question, profile, interactionContext);
    };
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    await waitFor(() => expect(screen.getByRole("button", { name: "添加演示论文" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "添加演示论文" }));
    await screen.findByRole("region", { name: "论文上下文" });
    fireEvent.click(screen.getByRole("button", { name: "选择摘要并询问" }));
    expect(screen.getByLabelText("当前选区上下文")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "移除当前选区" }));
    expect(screen.queryByLabelText("当前选区上下文")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "选择摘要并询问" }));
    fireEvent.change(screen.getByPlaceholderText("向研究助理提问…"), { target: { value: "这段在说什么？" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    // The selection is carried as structured interaction context (not inlined
    // into the question), so the echo stays clean while the Scope Resolver gets
    // the live selection anchor.
    const questionEcho = await screen.findByLabelText(/探索问题：这段在说什么/);
    expect(questionEcho.textContent).toContain("这段在说什么？");
    expect(questionEcho.textContent).not.toContain("求证范围");
    expect(lastContext?.surface).toBe("paper_reader");
    expect(lastContext?.selection?.block_id).toBeTruthy();
    expect(lastContext?.selection?.text).toBeTruthy();
    expect(await screen.findByText("研究助理 · Research Pulse")).toBeTruthy();
  });

  it("keeps selection translation and explanation on the fixed-context message path", async () => {
    const client = createFixtureWorkbenchClient();
    const originalSend = client.addFixtureMessage!.bind(client);
    const prompts: string[] = [];
    client.addFixtureMessage = async (sessionId, text, context) => {
      prompts.push(text);
      return originalSend(sessionId, text, context);
    };
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    fireEvent.click(await screen.findByRole("button", { name: "添加演示论文" }));
    await screen.findByRole("region", { name: "论文上下文" });
    fireEvent.click(screen.getByRole("button", { name: "选择摘要并询问" }));
    expect(screen.getByLabelText("当前选区上下文")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "翻译" }));
    await waitFor(() => expect(prompts).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "解释" }));
    await waitFor(() => expect(prompts).toHaveLength(2));

    expect(prompts[0]).toContain("自然、完整的中文");
    expect(prompts[0]).toContain("不要输出‘来源：[论文证据]’");
    expect(prompts[1]).toContain("使用中文并按段落组织");
    expect(prompts[1]).toContain("不要输出‘来源：[论文证据]’");
    expect(screen.getByText("翻译当前选区")).toBeTruthy();
    expect(screen.queryByText(/请将当前选区翻译成自然/)).toBeNull();
  });

  it("shows an immediate pending user and assistant state for a slow evidence action", async () => {
    const client = createFixtureWorkbenchClient();
    const originalSend = client.addFixtureMessage!.bind(client);
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    client.addFixtureMessage = async (sessionId, text, context) => {
      await gate;
      return originalSend(sessionId, text, context);
    };
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    fireEvent.click(await screen.findByRole("button", { name: "添加演示论文" }));
    await screen.findByRole("region", { name: "论文上下文" });
    fireEvent.click(screen.getByRole("button", { name: "选择摘要并询问" }));
    fireEvent.click(screen.getByRole("button", { name: "翻译" }));

    expect(await screen.findByLabelText("选区请求已发送")).toBeTruthy();
    expect(screen.getByLabelText("选区请求处理中").textContent).toContain("正在翻译当前选区");

    release();
    await waitFor(() => expect(screen.queryByLabelText("选区请求处理中")).toBeNull());
  });

  it("shows an immediate pending state for a slow selection question", async () => {
    const client = createFixtureWorkbenchClient();
    const originalSend = client.sendTurn!.bind(client);
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    client.sendTurn = async (sessionId, text, options) => {
      await gate;
      return originalSend(sessionId, text, options);
    };
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    fireEvent.click(await screen.findByRole("button", { name: "添加演示论文" }));
    await screen.findByRole("region", { name: "论文上下文" });
    fireEvent.click(screen.getByRole("button", { name: "选择摘要并询问" }));
    fireEvent.change(screen.getByPlaceholderText("向研究助理提问…"), { target: { value: "这段在说什么？" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByLabelText("选区问题已发送")).toBeTruthy();
    expect(screen.getByLabelText("选区问题处理中").textContent).toContain("正在回答当前选区");

    release();
    await waitFor(() => expect(screen.queryByLabelText("选区问题处理中")).toBeNull());
  });

  it("renders a persisted generating assistant message instead of an empty bubble", async () => {
    const client = createFixtureWorkbenchClient();
    const originalAddPaper = client.addFixturePaper!.bind(client);
    client.addFixturePaper = async (sessionId, paperId) => {
      const session = await originalAddPaper(sessionId, paperId);
      session.messages.push({
        message_id: `${sessionId}-pending-assistant`,
        role: "assistant",
        text: "",
        generation_status: "generating",
        contexts: [],
        citations: [],
        created_at: new Date().toISOString(),
      });
      return session;
    };
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    fireEvent.click(await screen.findByRole("button", { name: "添加演示论文" }));

    expect(await screen.findByText("正在生成回答…")).toBeTruthy();
  });

  it("shows a note draft in the conversation and publishes only after confirmation", async () => {
    const client = createFixtureWorkbenchClient({ noteOutcome: "published", noteDelayMs: 20 });
    let openedKnowledgeId = "";
    render(<WorkbenchApp client={client} onOpenKnowledge={(knowledgeId) => { openedKnowledgeId = knowledgeId; }} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    fireEvent.click(await screen.findByRole("button", { name: "添加演示论文" }));
    fireEvent.click(await screen.findByRole("button", { name: "生成论文笔记" }));
    expect(screen.getByRole("status", { name: "笔记生成状态" }).textContent).toContain("正在生成");
    expect(await screen.findByText("草稿已完成，尚未进入知识库。确认后才会发布。")).toBeTruthy();
    expect(await screen.findByText("预览论文笔记草稿")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "确认发布" })[0]);
    expect(await screen.findByText("论文笔记已发布，可从知识库继续阅读。")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "打开论文笔记" })[0]);
    expect(openedKnowledgeId).toBe("kp:arxiv:2608.16447v1");
  });

  it("does not offer note generation while a new paper is still preparing", async () => {
    const client = createFixtureWorkbenchClient();
    const originalAddPaper = client.addFixturePaper!.bind(client);
    client.addFixturePaper = async (sessionId, paperId) => {
      const session = await originalAddPaper(sessionId, paperId);
      const paper = session.papers.find((item) => item.paper_id === paperId);
      if (paper) paper.preparation_status = "parsing";
      return session;
    };
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    fireEvent.click(await screen.findByRole("button", { name: "添加演示论文" }));

    expect(await screen.findByText("论文解析完成后可生成笔记")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "生成论文笔记" })).toBeNull();
  });

  it("keeps a failed NoteRun visible and retryable", async () => {
    const client = createFixtureWorkbenchClient({ noteOutcome: "failed", noteDelayMs: 0 });
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    fireEvent.click(await screen.findByRole("button", { name: "添加演示论文" }));
    fireEvent.click(await screen.findByRole("button", { name: "生成论文笔记" }));
    expect(await screen.findByText("笔记生成失败，可重试")).toBeTruthy();
    expect(screen.getByRole("button", { name: "重新生成笔记" })).toBeTruthy();
  });

  it("lets a user attach their own local PDF to an empty session", async () => {
    const client = createFixtureWorkbenchClient();
    const createObjectUrl = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:paper-alpha");
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    await screen.findByText("当前没有论文上下文");
    const file = new File(["%PDF-1.7"], "我的论文.pdf", { type: "application/pdf" });
    fireEvent.change(screen.getByLabelText("上传本地 PDF"), { target: { files: [file] } });
    expect(await screen.findByRole("heading", { name: "我的论文.pdf" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "论文上下文" })).toBeTruthy();
    expect(createObjectUrl).toHaveBeenCalledWith(file);
  });

  it("shows an exploration as conversation bubbles with budget sources stop retry and partial result", async () => {
    const client = createFixtureWorkbenchClient();
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    fireEvent.change(await screen.findByPlaceholderText("向研究助理提问…"), { target: { value: "比较方法路线" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    // The question echoes as a user bubble; the run streams as one assistant
    // bubble with events and a stop action.
    const questionEcho = await screen.findByLabelText("探索问题：比较方法路线");
    expect(questionEcho.className).toContain("user");
    expect(await screen.findByLabelText("探索运行：比较方法路线")).toBeTruthy();
    expect(screen.getByText("规划研究步骤")).toBeTruthy();
    expect(screen.getAllByText(/研究助理/).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("tab", { name: "运行轨迹" }));
    expect(screen.getByRole("region", { name: "公开运行轨迹" })).toBeTruthy();
    expect(screen.getByText("研究进度")).toBeTruthy();
    expect(screen.getByText("理解问题")).toBeTruthy();
    expect(screen.getByText("检索来源")).toBeTruthy();
    expect(screen.getByText(/技术细节 ·/)).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "工作区" }));
    fireEvent.click(screen.getByText("查看运行详情"));
    expect(screen.getByText("调用只读工具 search_sources")).toBeTruthy();
    expect(screen.getByText("发现受管来源")).toBeTruthy();
    const toolFailure = screen.getByLabelText("工具失败详情");
    expect(toolFailure.textContent).toContain("invalid_argument");
    expect(toolFailure.textContent).toContain("after_correction");
    expect(toolFailure.textContent).toContain("none");
    expect(toolFailure.textContent).toContain("diag-demo-1");
    expect(toolFailure.textContent).toContain("correct_parameters");
    expect(toolFailure.textContent).not.toMatch(/api[_-]?key|stack|prompt|full paper/i);
    expect(screen.getByRole("button", { name: "停止探索" })).toBeTruthy();

    // Stop → terminal: the process collapses into a summary row with the
    // draft outside the fold; expanding reveals the full trace.
    fireEvent.click(screen.getByRole("button", { name: "停止探索" }));
    expect(await screen.findByText("初步结论 · 待核验")).toBeTruthy();
    expect(screen.getByText("已发现一条待验证的方法路线。")).toBeTruthy();
    expect(screen.getAllByText("已停止").length).toBeGreaterThan(0);
    // The terminal process is collapsed behind the summary row; expanding it
    // (details toggle) reveals the full event trace again.
    fireEvent.click(screen.getByText("查看过程"));
    expect(screen.getByText("调用只读工具 search_sources")).toBeTruthy();
    expect(screen.getAllByText("Attempt 1").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "重新开始探索" })).toBeTruthy();

    // Continue → the SAME run card (one card per run_id) shows a new attempt
    // as an explicit "基于已有结果继续执行" continuation, never a second card.
    fireEvent.click(screen.getByRole("button", { name: "重新开始探索" }));
    expect(await screen.findByText(/运行中 · 基于已有结果继续执行/)).toBeTruthy();
    expect(screen.getAllByLabelText("探索运行：比较方法路线")).toHaveLength(1);
    expect(screen.getAllByText("Attempt 2").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/0\/16 工具/).length).toBeGreaterThan(0);
    expect(screen.getByText("基于已有结果继续执行：继承已持久化的来源与草稿")).toBeTruthy();
    // The continuation copy never claims lossless checkpoint recovery.
    expect(screen.getByLabelText("探索运行：比较方法路线").textContent).not.toMatch(/无损|断点|checkpoint/i);
    // The expanded attempt history keeps the cancelled attempt 1 immutable
    // while attempt 2 is live.
    const history = screen.getByLabelText("Attempt 历史");
    expect(history.textContent).toContain("Attempt 1");
    expect(history.textContent).toContain("已停止");
    expect(history.textContent).toContain("Attempt 2");
    expect(history.textContent).toContain("运行中");
  });

  it("polls a running exploration, streams new events, and stops polling at a terminal status", async () => {
    vi.useFakeTimers();
    const client = createFixtureWorkbenchClient();
    let polls = 0;
    const originalGetSession = client.getSession.bind(client);
    vi.spyOn(client, "getSession").mockImplementation(async (sessionId) => {
      const session = await originalGetSession(sessionId);
      polls += 1;
      if (session.exploration_runs?.some((run) => run.status === "running")) {
        session.exploration_runs = session.exploration_runs.map((run) => run.status === "running" ? {
          ...run,
          status: "completed",
          events: [...(run.events ?? []), { sequence_no: 4, event_type: "final_draft", summary: "探索草稿已生成", stable_ids: {}, counters: {} }],
        } : run);
      }
      return session;
    });
    const view = render(<WorkbenchApp client={client} />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });

    fireEvent.click(screen.getAllByLabelText("新建会话")[0]);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
    fireEvent.change(screen.getByPlaceholderText("向研究助理提问…"), { target: { value: "比较方法路线" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
    expect(screen.getByText("规划研究步骤")).toBeTruthy();

    await act(async () => {
      vi.advanceTimersByTime(2_000);
      await Promise.resolve(); await Promise.resolve();
    });
    expect(polls).toBeGreaterThan(0);
    expect(screen.getByText("探索草稿已生成")).toBeTruthy();
    expect(screen.getAllByText("已完成").length).toBeGreaterThan(0);

    const pollsAtTerminal = polls;
    view.unmount();
    await act(async () => { vi.advanceTimersByTime(4_000); });
    expect(polls).toBe(pollsAtTerminal);
    vi.useRealTimers();
  });

  it("treats the composer as the research assistant agent (no separate explore toggle)", async () => {
    const client = createFixtureWorkbenchClient();
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    const scope = await screen.findByLabelText("上下文范围") as HTMLSelectElement;
    expect(Array.from(scope.options).map((option) => option.text)).toEqual(["无论文", "选区", "章节", "全文"]);
    // The independent "开始探索" toggle is gone; the one submit is the agent.
    expect(screen.queryByRole("button", { name: "开始探索" })).toBeNull();
    expect(screen.queryByRole("group", { name: "探索模式" })).toBeNull();
    expect(screen.getByPlaceholderText("向研究助理提问…")).toBeTruthy();
  });

  it("starts the research assistant agent by default and streams its planning events", async () => {
    const client = createFixtureWorkbenchClient();
    const started: Array<string | undefined> = [];
    const originalStart = client.startFixtureExploration!.bind(client);
    client.startFixtureExploration = async (sessionId, question, profile) => {
      started.push(profile);
      return originalStart(sessionId, question, profile);
    };
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    fireEvent.change(await screen.findByPlaceholderText("向研究助理提问…"), { target: { value: "给出实验设计" } });

    // The single submit defaults to the research assistant agent (no profile toggle).
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByText(/运行中 · 规划研究步骤/)).toBeTruthy();
    expect(screen.getAllByText(/研究助理/).length).toBeGreaterThan(0);
    expect(started.at(-1)).toBe("assistant");
  });

  it("passes a manually selected answer mode to the unified turn router", async () => {
    const client = createFixtureWorkbenchClient();
    const originalSend = client.sendTurn!.bind(client);
    const sentActions: Array<string | undefined> = [];
    client.sendTurn = async (sessionId, text, options) => {
      sentActions.push(options?.action);
      return originalSend(sessionId, text, options);
    };
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    fireEvent.change(await screen.findByRole("combobox", { name: "回答方式" }), { target: { value: "web_search" } });
    fireEvent.change(await screen.findByPlaceholderText("向研究助理提问…"), { target: { value: "查网页上的最新进展" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(sentActions.at(-1)).toBe("web_search"));
  });

  it("keeps discovered paper candidates in a separate related-papers overview section", async () => {
    const client = createFixtureWorkbenchClient();
    const originalSend = client.sendTurn!.bind(client);
    client.sendTurn = async (sessionId, question, options) => {
      const session = await originalSend(sessionId, question, options);
      return {
        ...session,
        exploration_runs: (session.exploration_runs ?? []).map((run) => ({
          ...run,
          sources: [
            ...run.sources,
            {
              source_id: "arxiv://2407.13399",
              title: "Understanding Reference Policies in DPO",
              kind: "external_candidate",
              url: "https://arxiv.org/pdf/2407.13399",
              relevance: "92%",
            },
          ],
        })),
      };
    };
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    fireEvent.change(await screen.findByPlaceholderText("向研究助理提问…"), { target: { value: "寻找相关论文" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    const related = await screen.findByRole("region", { name: "相关论文" });
    expect(related.textContent).toContain("Understanding Reference Policies in DPO");
    expect(related.textContent).toContain("待核验");
    expect(related.textContent).toContain("研究地图");
    expect(screen.getByRole("button", { name: "添加" })).toBeTruthy();
    expect(screen.queryByText("Understanding Reference Policies in DPO", { selector: ".workbench-overview-map-item strong" })).toBeNull();
  });

  it("opens workspace documents and resolves a pending direction into the active focus", async () => {
    const client = createFixtureWorkbenchClient();
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    expect(await screen.findByRole("region", { name: "研究概览" })).toBeTruthy();
    expect(await screen.findByLabelText("研究概览统计")).toBeTruthy();
    expect(screen.getByLabelText("研究进展已更新")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "研究计划" }).length).toBeGreaterThan(0);

    fireEvent.click(screen.getAllByRole("button", { name: /当前研究进展$/ })[0]);
    expect(await screen.findByRole("heading", { name: "当前研究进展" })).toBeTruthy();
    expect(screen.getAllByText("候选研究线索").length).toBeGreaterThan(0);
    const focusOption = screen.getByRole("button", { name: /设为当前焦点.*哪些数据能标识真实偏好/ });
    fireEvent.click(focusOption);
    await waitFor(() => expect(screen.getAllByText("哪些数据能标识真实偏好？").length).toBeGreaterThan(0));
    expect(screen.getAllByText(/当前焦点/).length).toBeGreaterThan(0);

    fireEvent.click(screen.getAllByRole("button", { name: "研究计划" }).at(-1)!);
    expect(await screen.findByRole("heading", { name: "研究计划" })).toBeTruthy();
    expect(screen.getByText("方法路线图（Method Map）")).toBeTruthy();
  });
  it("makes the next operation explicit after a focus is selected", async () => {
    const client = createFixtureWorkbenchClient();
    const originalSend = client.sendTurn!.bind(client);
    const sentActions: Array<string | undefined> = [];
    client.sendTurn = async (sessionId, text, options) => {
      sentActions.push(options?.action);
      return originalSend(sessionId, text, options);
    };
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);

    const overview = await screen.findByRole("region", { name: "研究概览" });
    const candidate = await screen.findByRole("button", { name: /选择.*哪些数据能标识真实偏好/ });
    fireEvent.click(candidate);

    await waitFor(() => expect(overview.textContent).toContain("已选定"));
    await waitFor(() => expect(overview.textContent).toContain("等待你继续"));
    const nextStep = await screen.findByRole("button", { name: "开始下一步研究" });
    await waitFor(() => expect((nextStep as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(nextStep);

    const composer = screen.getByPlaceholderText("围绕当前研究焦点提问…") as HTMLTextAreaElement;
    await waitFor(() => expect(composer.value).toContain("请围绕当前研究焦点继续深入研究"));
    expect(composer.value).toContain("哪些数据能标识真实偏好？");
    const persisted = await client.fetchWorkspace!("session-1");
    expect(persisted?.decision_points?.[0].status).toBe("approved");

    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(sentActions.at(-1)).toBe("research_run"));
    expect(await screen.findByText(/运行中 · 规划研究步骤/)).toBeTruthy();
  });
  it("keeps the research overview in a resizable side rail and removes duplicate header shortcuts", async () => {
    const client = createFixtureWorkbenchClient();
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);

    const overview = await screen.findByRole("region", { name: "研究概览" });
    expect(overview).toBeTruthy();
    expect(screen.queryByLabelText("研究文档入口")).toBeNull();

    const separator = screen.getByRole("separator", { name: "调整研究概览宽度" });
    fireEvent.mouseDown(separator, { clientX: 1000 });
    fireEvent.mouseMove(window, { clientX: 800 });
    fireEvent.mouseUp(window);

    const sessionView = screen.getByRole("region", { name: "当前会话" });
    expect(sessionView.style.getPropertyValue("--overview-width")).toBe("560px");
  });
  it("renames a workspace in place through the rename dialog", async () => {
    const client = createFixtureWorkbenchClient();
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    await waitFor(() => expect(screen.getByRole("button", { name: "打开会话：新会话" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "重命名工作区：研究项目" }));
    fireEvent.change(screen.getByPlaceholderText("重命名工作区"), { target: { value: "记忆增强研究" } });
    fireEvent.click(screen.getByRole("button", { name: "确定" }));
    await waitFor(() => expect(screen.getByText("记忆增强研究")).toBeTruthy());
  });
  it("confirms before deleting a session and a workspace", async () => {
    const client = createFixtureWorkbenchClient();
    render(<WorkbenchApp client={client} />);
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    await waitFor(() => expect(screen.getByRole("button", { name: "打开会话：新会话" })).toBeTruthy());
    // delete session requires confirmation: cancel keeps it
    const session = screen.getByRole("button", { name: "打开会话：新会话" });
    fireEvent.contextMenu(session);
    fireEvent.click(await screen.findByRole("menuitem", { name: "删除" }));
    expect(screen.getByText(/确定删除会话/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByText(/确定删除会话/)).toBeNull();
    // delete workspace requires confirmation: confirm removes its threads
    fireEvent.click(screen.getByRole("button", { name: /删除工作区：研究项目/ }));
    expect(screen.getByText(/确定删除工作区/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "删除工作区" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "打开会话：新会话" })).toBeNull());
  });
  it("groups sessions into collapsible workspaces in the sidebar", async () => {
    const client = createFixtureWorkbenchClient();
    render(<WorkbenchApp client={client} />);
    // Create two sessions: each is its own workspace when no workspace is pinned.
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    await waitFor(() => expect(screen.getByRole("button", { name: "打开会话：新会话" })).toBeTruthy());
    fireEvent.click((await screen.findAllByLabelText("新建会话"))[0]);
    await waitFor(() => expect(screen.getAllByRole("button", { name: "打开会话：新会话" })).toHaveLength(2));
    // Two independent workspaces → two collapsible group nodes, each with a thread.
    expect(document.querySelectorAll(".workbench-workspace-group")).toHaveLength(2);
    expect(document.querySelectorAll(".workbench-workspace-sessions")).toHaveLength(2);
  });
});
