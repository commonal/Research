import { FormEvent, type ReactNode, useEffect, useMemo, useState } from "react";
import {
  ActiveRunConflictError,
  createResearchTopic,
  fetchKnowledge,
  fetchKnowledgeDetail,
  fetchReviewDraft,
  fetchReviewDrafts,
  fetchProductionRun,
  fetchResearchTopics,
  fetchSchedulerStatus,
  KnowledgeNotFoundError,
  resumeChat,
  retryResearchTopic,
  approveReviewDraft,
  rejectReviewDraft,
  startChat,
  updateResearchTopic,
} from "./api";
import { headingSlug, MarkdownReader } from "./MarkdownReader";
import type {
  ChatResponse,
  EvidenceAnchor,
  KnowledgeDetail,
  KnowledgeItem,
  ProductionRun,
  ProductionRunStatus,
  ReviewDraft,
  SchedulerStatus,
  TopicWithLatestRun,
} from "./types";
import { WorkbenchApp } from "./workbench/WorkbenchApp";
import { createHttpWorkbenchClient, WorkbenchApiError } from "./workbench/httpWorkbenchClient";
import type { KnowledgeWorkbenchStatus } from "./workbench/types";
import { ResearchUiPrototype } from "./ResearchUiPrototype";

type Message = { role: "assistant" | "user"; text: string };
type ReadingState = "loading" | "ready" | "empty" | "not_found" | "unavailable";

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric" }).format(new Date(value));
}

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function levelLabel(level: string): string {
  return level === "full_text_text" ? "全文精读" : level === "full_text_multimodal" ? "图文精读" : "摘要速读";
}

function evidenceKindLabel(kind: EvidenceAnchor["block_kind"]): string {
  return { text: "正文", formula: "公式", table: "表格", figure: "图片", caption: "图注" }[kind ?? "text"];
}

function evidenceLocator(anchor: EvidenceAnchor): string {
  if (anchor.page_start) return anchor.page_end && anchor.page_end !== anchor.page_start ? `第 ${anchor.page_start}–${anchor.page_end} 页` : `第 ${anchor.page_start} 页`;
  if (anchor.section) return `章节：${anchor.section}`;
  return "无页级定位";
}

function readingSectionLabel(name: string): string {
  return {
  summary: "一分钟总结",
  problem: "问题",
  research_question: "论文真正要解决的问题",
    core_idea: "核心直觉",
    method: "方法",
    workflow: "工作流程",
    experiments: "实验与结果",
    experiment_design: "实验设计",
    result_interpretation: "结果如何解读",
    limitations: "局限与阅读边界",
    reproduction: "复现线索",
  }[name] ?? name;
}

function runStatusLabel(status: ProductionRunStatus): string {
  return {
    queued: "等待开始",
    running: "抓取中",
    completed: "已完成",
    partial_failed: "部分完成",
    failed: "抓取失败",
  }[status];
}

function runTriggerLabel(trigger: ProductionRun["trigger"]): string {
  return { initial: "首次抓取", manual: "手动抓取", scheduled: "自动更新" }[trigger];
}

function runErrorLabel(code: string | null): string | null {
  if (!code) return null;
  return {
    deepseek_not_configured: "尚未配置 DeepSeek，配置后可重新抓取。",
    process_restarted: "服务在任务完成前重启，请重新抓取。",
    production_run_failed: "论文生产未完成，请稍后重试。",
    candidate_failures: "部分候选未能通过处理或质量校验。",
  }[code] ?? "论文生产未完成，请重新抓取。";
}

type ActiveProductionRun = ProductionRun & { status: "queued" | "running" };

function isActiveRun(run: ProductionRun | null): run is ActiveProductionRun {
  return run?.status === "queued" || run?.status === "running";
}

export default function App() {
  const [items, setItems] = useState<KnowledgeItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<KnowledgeDetail | null>(null);
  const [detailRevision, setDetailRevision] = useState(0);
  const [reviewDraft, setReviewDraft] = useState<ReviewDraft | null>(null);
  const [reviewLoading, setReviewLoading] = useState(false);
  const [reviewAction, setReviewAction] = useState<"approve" | "reject" | null>(null);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [readingState, setReadingState] = useState<ReadingState>("loading");
  const [chatOpen, setChatOpen] = useState(false);
  const [scopeCurrentPaper, setScopeCurrentPaper] = useState(false);
  const [query, setQuery] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [pending, setPending] = useState<Extract<ChatResponse, { status: "needs_confirmation" }> | null>(null);
  const [chatLoading, setChatLoading] = useState(false);
  const [topics, setTopics] = useState<TopicWithLatestRun[]>([]);
  const [topicsLoading, setTopicsLoading] = useState(true);
  const [topicsError, setTopicsError] = useState<string | null>(null);
  const [schedulerStatus, setSchedulerStatus] = useState<SchedulerStatus | null>(null);
  const [schedulerLoading, setSchedulerLoading] = useState(true);
  const [topicModalOpen, setTopicModalOpen] = useState(false);
  const [topicName, setTopicName] = useState("");
  const [topicQuery, setTopicQuery] = useState("");
  const [topicFormError, setTopicFormError] = useState<string | null>(null);
  const [topicSubmitting, setTopicSubmitting] = useState(false);
  const [watchedRunIds, setWatchedRunIds] = useState<string[]>([]);
  const [timelineMode, setTimelineMode] = useState(true);
  const [topicsPanelOpen, setTopicsPanelOpen] = useState(false);
  const [appMode, setAppMode] = useState<"knowledge" | "workbench">("knowledge");
  const [workbenchEntrySessionId, setWorkbenchEntrySessionId] = useState<string | null>(null);
  const [workbenchEntryLoading, setWorkbenchEntryLoading] = useState(false);
  const [workbenchEntryError, setWorkbenchEntryError] = useState<string | null>(null);
  const [workbenchPaperStatus, setWorkbenchPaperStatus] = useState<KnowledgeWorkbenchStatus | null>(null);
  const [workbenchPaperStatusLoading, setWorkbenchPaperStatusLoading] = useState(false);
  const workbenchClient = useMemo(() => createHttpWorkbenchClient(), []);
  const showResearchUiPrototype = import.meta.env.DEV && new URLSearchParams(window.location.search).get("prototype") === "research-ui";

  async function loadTimeline(showLoading = true) {
    if (showLoading) {
      setReadingState("loading");
      setDetail(null);
    }
    try {
      const records = await fetchKnowledge();
      setItems(records);
      if (!records.length) {
        setSelectedId(null);
        setReadingState("empty");
        return;
      }
      // Stay in timeline view unless the user already opened a paper; we no
      // longer auto-select the first note on load.
      setSelectedId(null);
      setTimelineMode(true);
      setDetailRevision((current) => current + 1);
    } catch {
      setItems([]);
      setSelectedId(null);
      setReadingState("unavailable");
    }
  }

  useEffect(() => {
    void loadTimeline();
    const controller = new AbortController();
    setTopicsLoading(true);
    fetchResearchTopics(controller.signal)
      .then((records) => {
        if (controller.signal.aborted) return;
        setTopics(records);
        setWatchedRunIds(records.flatMap((record) => isActiveRun(record.latest_run) ? [record.latest_run.run_id] : []));
        setTopicsError(null);
      })
      .catch(() => {
        if (!controller.signal.aborted) setTopicsError("研究方向暂不可用");
      })
      .finally(() => {
        if (!controller.signal.aborted) setTopicsLoading(false);
      });
    fetchSchedulerStatus(controller.signal)
      .then((status) => {
        if (!controller.signal.aborted) setSchedulerStatus(status);
      })
      .catch(() => {
        if (!controller.signal.aborted) setSchedulerStatus(null);
      })
      .finally(() => {
        if (!controller.signal.aborted) setSchedulerLoading(false);
      });
    return () => controller.abort();
  }, []);

  const watchedRunKey = watchedRunIds.join("|");
  useEffect(() => {
    if (!watchedRunIds.length) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      void Promise.allSettled(watchedRunIds.map((runId) => fetchProductionRun(runId, controller.signal)))
        .then((results) => {
          if (controller.signal.aborted) return;
          const runs = results.flatMap((result) => result.status === "fulfilled" ? [result.value] : []);
          if (!runs.length) return;
          setTopics((current) => current.map((record) => {
            const updated = runs.find((run) => run.topic_id === record.topic.topic_id);
            return updated ? { ...record, latest_run: updated } : record;
          }));
          setWatchedRunIds(runs.filter(isActiveRun).map((run) => run.run_id));
          if (runs.some((run) => !isActiveRun(run) && run.published_count > 0)) {
            void loadTimeline(false);
          }
        });
    }, 2_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [watchedRunKey]);

  useEffect(() => {
    if (!selectedId) return;
    const controller = new AbortController();
    setReadingState("loading");
    setDetail(null);
    setReviewLoading(true);
    setReviewDraft(null);
    setReviewError(null);
    setWorkbenchEntryError(null);
    fetchKnowledgeDetail(selectedId, controller.signal)
      .then((record) => {
        if (!controller.signal.aborted) {
          setDetail(record);
          setReadingState("ready");
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setDetail(null);
        setReadingState(error instanceof KnowledgeNotFoundError ? "not_found" : "unavailable");
      });
    fetchReviewDrafts(selectedId, controller.signal)
      .then(async (drafts) => {
        if (controller.signal.aborted || !drafts.length) return;
        const latest = drafts[0];
        const full = await fetchReviewDraft(selectedId, latest.draft_id, controller.signal);
        if (!controller.signal.aborted) setReviewDraft(full);
      })
      .catch(() => {
        if (!controller.signal.aborted) setReviewError("待审核精读暂不可用");
      })
      .finally(() => {
        if (!controller.signal.aborted) setReviewLoading(false);
      });
    return () => controller.abort();
  }, [selectedId, detailRevision]);

  useEffect(() => {
    const knowledgeId = detail?.knowledge_id;
    const readStatus = workbenchClient.getKnowledgePaperStatus;
    if (!knowledgeId || !readStatus) {
      setWorkbenchPaperStatus(null);
      setWorkbenchPaperStatusLoading(false);
      return;
    }
    const controller = new AbortController();
    setWorkbenchPaperStatus(null);
    setWorkbenchPaperStatusLoading(true);
    void readStatus(knowledgeId)
      .then((status) => {
        if (!controller.signal.aborted) setWorkbenchPaperStatus(status);
      })
      .catch(() => {
        if (!controller.signal.aborted) setWorkbenchPaperStatus(null);
      })
      .finally(() => {
        if (!controller.signal.aborted) setWorkbenchPaperStatusLoading(false);
      });
    return () => controller.abort();
  }, [detail?.knowledge_id, workbenchClient]);

  const selected = useMemo(
    () => items.find((item) => item.knowledge_id === selectedId) ?? null,
    [items, selectedId],
  );

  function openPaper(id: string) {
    setSelectedId(id);
    setTimelineMode(false);
  }

  async function openCurrentNoteInWorkbench() {
    if (!detail || workbenchEntryLoading) return;
    const shouldImport = workbenchPaperStatus?.status === "not_registered"
      || workbenchPaperStatus?.status === "preparing"
      || workbenchPaperStatus?.status === "failed";
    const open = shouldImport
      ? workbenchClient.importKnowledgeSession
      : workbenchClient.createKnowledgeSession;
    if (!open) return;
    if (workbenchPaperStatus?.status === "unavailable") {
      setWorkbenchEntryError("这份笔记没有可下载的论文来源，暂时无法进入工作台。");
      return;
    }
    setWorkbenchEntryLoading(true);
    setWorkbenchEntryError(null);
    try {
      const session = await open(detail.knowledge_id);
      setWorkbenchEntrySessionId(session.session_id);
      setChatOpen(false);
      setScopeCurrentPaper(false);
      setAppMode("workbench");
    } catch (error) {
      if (error instanceof WorkbenchApiError && error.code === "paper_not_registered") {
        setWorkbenchPaperStatus({
          knowledge_id: detail.knowledge_id,
          status: "not_registered",
          can_import: Boolean(workbenchClient.importKnowledgeSession),
          paper_id: null,
          title: detail.title,
          source_url: detail.source_urls[0] ?? null,
        });
        setWorkbenchEntryError("这份笔记还没有进入工作台，可以点击“导入论文并进入工作台”。");
      } else {
        setWorkbenchEntryError(error instanceof Error ? error.message : "无法打开论文工作台");
      }
    } finally {
      setWorkbenchEntryLoading(false);
    }
  }

  async function submitQuestion(event: FormEvent) {
    event.preventDefault();
    const trimmed = query.trim();
    if (!trimmed || chatLoading) return;
    setMessages((current) => [...current, { role: "user", text: trimmed }]);
    setQuery("");
    setChatLoading(true);
    try {
      const currentKnowledgeId = scopeCurrentPaper && readingState === "ready" ? detail?.knowledge_id : undefined;
      handleChatResponse(await startChat(trimmed, currentKnowledgeId));
    } catch {
      setMessages((current) => [
        ...current,
        { role: "assistant", text: "问答后端暂不可用，请确认 FastAPI 与 PostgreSQL 已启动后重试。" },
      ]);
    } finally {
      setChatLoading(false);
    }
  }

  async function decideSupplement(approved: boolean) {
    if (!pending) return;
    setChatLoading(true);
    setPending(null);
    try {
      handleChatResponse(await resumeChat(pending.thread_id, approved));
    } catch {
      setMessages((current) => [...current, { role: "assistant", text: "本次问答无法恢复，请重新提问。" }]);
    } finally {
      setChatLoading(false);
    }
  }

  function handleChatResponse(response: ChatResponse) {
    if (response.status === "needs_confirmation") {
      setPending(response);
      setMessages((current) => [...current, { role: "assistant", text: response.interrupt.message }]);
      return;
    }
    setMessages((current) => [...current, { role: "assistant", text: response.answer }]);
  }

  async function submitTopic(event: FormEvent) {
    event.preventDefault();
    const name = topicName.trim();
    const paperQuery = topicQuery.trim();
    if (!name || !paperQuery) {
      setTopicFormError("请填写方向名称和论文检索词");
      return;
    }
    setTopicSubmitting(true);
    setTopicFormError(null);
    try {
      const created = await createResearchTopic(name, paperQuery);
      setTopics((current) => [
        { topic: created.topic, latest_run: created.run },
        ...current.filter((record) => record.topic.topic_id !== created.topic.topic_id),
      ]);
      if (isActiveRun(created.run)) setWatchedRunIds((current) => [...new Set([...current, created.run.run_id])]);
      setTopicName("");
      setTopicQuery("");
      setTopicModalOpen(false);
    } catch (error) {
      setTopicFormError(error instanceof Error ? error.message : "无法保存研究方向");
    } finally {
      setTopicSubmitting(false);
    }
  }

  function applyRun(run: ProductionRun) {
    setTopics((current) => current.map((record) => (
      record.topic.topic_id === run.topic_id ? { ...record, latest_run: run } : record
    )));
    if (isActiveRun(run)) setWatchedRunIds((current) => [...new Set([...current, run.run_id])]);
  }

  async function retryTopic(topicId: string) {
    setTopicsError(null);
    try {
      applyRun(await retryResearchTopic(topicId));
    } catch (error) {
      if (error instanceof ActiveRunConflictError) {
        try {
          applyRun(await fetchProductionRun(error.runId));
          return;
        } catch {
          setTopicsError("无法读取正在运行的抓取任务");
          return;
        }
      }
      setTopicsError(error instanceof Error ? error.message : "无法重新抓取该方向");
    }
  }

  async function updateTopic(
    topicId: string,
    settings: { enabled?: boolean; daily_limit?: number },
  ) {
    setTopicsError(null);
    try {
      const updated = await updateResearchTopic(topicId, settings);
      setTopics((current) => current.map((record) => (
        record.topic.topic_id === topicId ? { ...record, topic: updated } : record
      )));
    } catch (error) {
      setTopicsError(error instanceof Error ? error.message : "无法更新自动追踪设置");
    }
  }

  async function handleReviewAction(action: "approve" | "reject") {
    if (!reviewDraft || reviewAction) return;
    setReviewAction(action);
    setReviewError(null);
    try {
      if (action === "approve") {
        await approveReviewDraft(reviewDraft.knowledge_id, reviewDraft.draft_id);
        setReviewDraft(null);
        await loadTimeline(false);
        setDetailRevision((current) => current + 1);
      } else {
        setReviewDraft(await rejectReviewDraft(reviewDraft.knowledge_id, reviewDraft.draft_id));
      }
    } catch (error) {
      setReviewError(error instanceof Error ? error.message : "待审核操作失败");
    } finally {
      setReviewAction(null);
    }
  }

  if (showResearchUiPrototype) return <ResearchUiPrototype />;

  return (
    <main className="app-shell">
      <header className={`topbar ${appMode === "workbench" ? "topbar-workbench" : ""}`}>
        <a className="brand" href="#top" aria-label="Research Pulse 首页">
          <span className="brand-mark">R</span>
          <span>Research Pulse</span>
        </a>
        <div className="topbar-actions">
          {appMode === "workbench" ? <span className="workbench-topbar-caption">研究工作台</span> : <span className="sync-status"><i /> {readingState === "ready" ? "论文笔记已同步" : "本地论文笔记"}</span>}
          <button
            className="workspace-switch"
            aria-pressed={appMode === "workbench"}
            onClick={() => setAppMode(appMode === "workbench" ? "knowledge" : "workbench")}
          >
            {appMode === "workbench" ? "论文笔记" : "工作台"}
          </button>
        </div>
      </header>

      <section className={`workspace ${appMode === "workbench" ? "workbench-workspace" : ""}`} id="top">
        {appMode === "workbench" ? (
          <WorkbenchApp
            client={workbenchClient}
            initialSessionId={workbenchEntrySessionId}
            onOpenKnowledge={(knowledgeId) => { setAppMode("knowledge"); openPaper(knowledgeId); }}
          />
        ) : timelineMode ? (
          <section className="timeline-panel" aria-label="论文时间线">
            <div className="timeline-toolbar">
              <div className="timeline-toolbar-left">
                <button className="primary-button" onClick={() => { setTopicFormError(null); setTopicModalOpen(true); }}>＋ 添加研究方向</button>
                <button className="topics-manage-button" onClick={() => setTopicsPanelOpen(true)}>研究方向</button>
              </div>
              {items.length > 0 && <span className="timeline-count">{items.length} 篇笔记</span>}
            </div>
            {readingState === "empty" && (
              <ReadingNotice
                badge="空库引导 · 非真实知识"
                title="先生成第一份论文精读"
                text="当前还没有已发布论文笔记。点击“添加研究方向”，系统会抓取少量论文；只有通过质量门禁的 Markdown 才会出现在这里。"
              >
                <button className="secondary-button" onClick={() => setTopicModalOpen(true)}>添加第一个研究方向</button>
              </ReadingNotice>
            )}
            {readingState === "unavailable" && (
              <ReadingNotice title="阅读服务暂不可用" text="无法连接论文笔记服务。页面不会用演示正文代替真实内容。">
                <button className="secondary-button" onClick={() => void loadTimeline()}>重试连接</button>
              </ReadingNotice>
            )}
            <div className="timeline-feed">
              {items.map((item, index) => (
                <button
                  className="timeline-card"
                  key={item.knowledge_id}
                  onClick={() => openPaper(item.knowledge_id)}
                >
                  {index === 0 && <span className="timeline-newest">最新</span>}
                  <span className="timeline-date">{formatDate(item.knowledge_version)}</span>
                  <strong className="timeline-title">{item.title}</strong>
                  <span className="timeline-meta">
                    {levelLabel(item.evidence_level)}{item.domain ? ` · ${item.domain.replaceAll("_", " ")}` : ""}
                  </span>
                  {item.source_url && (
                    <span className="timeline-source">来源：{item.source_url.replace("https://arxiv.org/abs/", "arXiv ")}</span>
                  )}
                </button>
              ))}
            </div>
            <p className="library-footnote">仅展示通过校验的已发布论文笔记 · 点击任意笔记查看正文</p>

            {topicsPanelOpen && (
              <div className="topic-backdrop" onMouseDown={() => setTopicsPanelOpen(false)}>
                <section className="topics-manage-panel" aria-label="研究方向管理" onMouseDown={(event) => event.stopPropagation()}>
                  <header>
                    <div><span className="panel-label">研究方向</span><h2>每日追踪设置</h2></div>
                    <button type="button" className="icon-button" onClick={() => setTopicsPanelOpen(false)} aria-label="关闭研究方向">×</button>
                  </header>
                  <p className="topics-manage-copy">这些方向决定 scout 每天挑选论文的兴趣画像；暂停后不再参与每日自动更新。</p>
                  <p className="scheduler-summary">
                    {schedulerLoading
                      ? "正在读取自动更新时间…"
                      : schedulerStatus?.enabled
                        ? schedulerStatus.next_run_at
                          ? `下次自动更新：${formatDateTime(schedulerStatus.next_run_at)}`
                          : `每日 ${schedulerStatus.daily_time} 自动更新`
                        : schedulerStatus
                          ? "自动更新未启用"
                          : "自动更新状态暂不可用"}
                  </p>
                  <button className="new-topic" onClick={() => { setTopicsPanelOpen(false); setTopicFormError(null); setTopicModalOpen(true); }}>＋ 添加研究方向</button>
                  {topicsLoading && <p className="topic-empty">正在读取方向…</p>}
                  {!topicsLoading && !topics.length && <p className="topic-empty">还没有研究方向</p>}
                  {topics.map((record) => {
                    const latest = record.latest_run;
                    const error = runErrorLabel(latest?.error_code ?? null);
                    return (
                      <article className="topic-card" key={record.topic.topic_id}>
                        <strong>{record.topic.name}</strong>
                        {latest && <span className={`run-status ${latest.status}`}>{runStatusLabel(latest.status)}</span>}
                        <p>{record.topic.query}</p>
                        <div className="topic-subscription-row">
                          <span className={`topic-auto-state ${record.topic.enabled ? "enabled" : "paused"}`}>
                            {record.topic.enabled ? "自动更新" : "已暂停"}
                          </span>
                          <label>
                            每日
                            <select
                              aria-label={`${record.topic.name}每日篇数`}
                              value={record.topic.daily_limit}
                              onChange={(event) => void updateTopic(record.topic.topic_id, { daily_limit: Number(event.target.value) })}
                            >
                              {[1, 2, 3].map((value) => <option value={value} key={value}>{value} 篇</option>)}
                            </select>
                          </label>
                        </div>
                        {record.topic.last_successful_discovery_at && (
                          <small>上次发现：{formatDateTime(record.topic.last_successful_discovery_at)}</small>
                        )}
                        {latest && (
                          <small>{runTriggerLabel(latest.trigger)} · {latest.published_count} 篇新精读 · {latest.failed_count} 篇未发布</small>
                        )}
                        {error && <p className="topic-error">{error}</p>}
                        <button
                          className="topic-toggle"
                          aria-label={`${record.topic.enabled ? "暂停" : "恢复"}${record.topic.name}自动更新`}
                          onClick={() => void updateTopic(record.topic.topic_id, { enabled: !record.topic.enabled })}
                        >
                          {record.topic.enabled ? "暂停自动更新" : "恢复自动更新"}
                        </button>
                        {latest?.status && !isActiveRun(latest) && (
                          <button className="retry-topic" onClick={() => void retryTopic(record.topic.topic_id)}>重新抓取</button>
                        )}
                      </article>
                    );
                  })}
                  {topicsError && <p className="topic-error">{topicsError}</p>}
                </section>
              </div>
            )}
          </section>
        ) : (
          <div className="reader-layout">
        <TableOfContents headings={detail ? extractHeadings(detail.markdown) : []} />
        <article className="reader-panel" aria-live="polite">
          <div className="reader-back">
            <button className="secondary-button" onClick={() => setTimelineMode(true)}>← 返回时间线</button>
          </div>
          {readingState === "loading" && <ReadingNotice title="正在读取论文笔记" text="正在校验当前版本与正文哈希…" />}
          {readingState === "empty" && (
            <ReadingNotice
              badge="空库引导 · 非真实知识"
              title="先生成第一份论文精读"
              text="当前还没有已发布论文笔记。点击左侧“添加研究方向”，系统会抓取少量论文；只有通过质量门禁的 Markdown 才会出现在这里。"
            >
              <button className="secondary-button" onClick={() => setTopicModalOpen(true)}>添加第一个研究方向</button>
            </ReadingNotice>
          )}
          {readingState === "not_found" && (
            <ReadingNotice title="这份论文笔记当前不可用" text="文件可能已更新、尚未发布或未通过完整性校验。请选择其他条目或刷新时间线。">
              <button className="secondary-button" onClick={() => void loadTimeline()}>刷新论文笔记</button>
            </ReadingNotice>
          )}
          {readingState === "unavailable" && (
            <ReadingNotice title="阅读服务暂不可用" text="无法连接论文笔记服务。页面不会用演示正文代替真实内容。">
              <button className="secondary-button" onClick={() => void loadTimeline()}>重试连接</button>
            </ReadingNotice>
          )}
          {readingState === "ready" && detail && (
            <>
              <div className="reader-eyebrow">{levelLabel(detail.evidence_level)} · {detail.domain.replaceAll("_", " ")}</div>
              {detail.reading_mode === "deep_reading" && (
                <div
                  className="reading-mode-badge"
                  role="status"
                  aria-label="分阶段精读：模型解读与证据主张已分离"
                >
                  分阶段精读：模型解读与证据主张已分离
                </div>
              )}
              {detail.provenance_status === "legacy_missing_provenance" && (
                <p className="legacy-warning">旧版知识：正文可阅读，但缺少持久来源映射，不会作为事实型 RAG 证据。</p>
              )}
              {detail.evidence_model === "legacy_v2" && (
                <p className="legacy-warning">旧证据模型：该版本没有逐节来源映射</p>
              )}
              <MarkdownReader
                markdown={detail.markdown}
                assetBaseUrl={`/api/knowledge/${encodeURIComponent(detail.knowledge_id)}/assets`}
              />
              {detail.evidence_model === "section_anchors" && !!detail.reading_sections?.length && (
                <section aria-label="逐节证据">
                  <h2>逐节证据</h2>
                  <ul className="evidence-boundaries" aria-label="逐节证据">
                    {detail.reading_sections.map((section) => {
                      const sectionAnchors = section.anchor_ids
                        .map((anchorId) => detail.anchors.find((anchor) => anchor.anchor_id === anchorId))
                        .filter((anchor): anchor is EvidenceAnchor => anchor !== undefined);
                      return (
                        <li key={section.section_name}>
                          <strong>{readingSectionLabel(section.section_name)}</strong>
                          {sectionAnchors.length ? sectionAnchors.map((anchor) => (
                            <span key={anchor.anchor_id}>{anchor.anchor_id} · {evidenceLocator(anchor)}</span>
                          )) : <small>该节没有可持久化的合格来源</small>}
                        </li>
                      );
                    })}
                  </ul>
                </section>
              )}
              {reviewDraft && reviewDraft.status === "needs_review" && (
                <section className="review-preview" aria-label="待审核精读预览">
                  <div className="review-preview-header">
                    <div>
                      <span className="review-badge">待审核精读</span>
                      <h2>新版本预览</h2>
                    </div>
                    <span className="review-not-rag">不会进入 RAG</span>
                  </div>
                  <p className="review-warning">这份精读尚未通过自动质量门禁，仅用于查看问题和人工复核。</p>
                  {!!reviewDraft.quality_issues.length && (
                    <ul className="review-issues" aria-label="质量问题">
                      {reviewDraft.quality_issues.map((issue) => <li key={`${issue.code}-${issue.claim_id ?? "none"}`}>{issue.code}</li>)}
                    </ul>
                  )}
                  <MarkdownReader markdown={reviewDraft.markdown} />
                  <p className="review-boundary">{reviewDraft.evidence_boundary}</p>
                </section>
              )}
            </>
          )}
        </article>

        <aside className="detail-panel">
          <span className="detail-label">当前论文笔记</span>
          {readingState === "ready" && detail && selected ? (
            <>
              <dl>
                <div><dt>范围</dt><dd>{levelLabel(detail.evidence_level)}</dd></div>
                <div><dt>版本</dt><dd>{new Date(detail.knowledge_version).toLocaleDateString("zh-CN")}</dd></div>
                <div><dt>状态</dt><dd><span className="approved-dot" /> 已发布</dd></div>
              </dl>
              {reviewDraft && reviewDraft.status === "needs_review" && (
                <section className="review-actions" aria-label="待审核操作">
                  <h3 className="side-heading">待审核版本</h3>
                  <p className="review-action-copy">新版本未通过质量门禁，确认后会重新解析并重新校验，不会直接覆盖旧版本。</p>
                  {reviewError && <p className="form-error">{reviewError}</p>}
                  <div className="review-action-buttons">
                    <button className="secondary-button" disabled={reviewAction !== null} onClick={() => void handleReviewAction("reject")}>
                      {reviewAction === "reject" ? "正在拒绝…" : "拒绝草稿"}
                    </button>
                    <button className="primary-button" disabled={reviewAction !== null} onClick={() => void handleReviewAction("approve")}>
                      {reviewAction === "approve" ? "重新校验中…" : "确认发布"}
                    </button>
                  </div>
                </section>
              )}
              {!reviewDraft && reviewLoading && <p className="detail-hint">正在检查待审核精读…</p>}
              <h3 className="side-heading">来源链接</h3>
              {detail.source_urls.map((url, index) => (
                <a className="source-link" href={url} target="_blank" rel="noopener noreferrer" key={url}>
                  打开来源 {index + 1} ↗
                </a>
              ))}
              <h3 className="side-heading">知识锚点</h3>
              {detail.anchors.length ? (
                <ul className="evidence-boundaries" aria-label="证据边界">
                  {detail.anchors.slice(0, 6).map((anchor) => (
                    <li key={anchor.anchor_id}>
                      <strong>{evidenceKindLabel(anchor.block_kind)}</strong>
                      <span>{evidenceLocator(anchor)}</span>
                      {anchor.figure_or_table && <em>{anchor.figure_or_table}</em>}
                      {anchor.parse_status && anchor.parse_status !== "available" && <small>解析降级：{anchor.parse_status}</small>}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="detail-hint">此版本没有可持久化的来源锚点；正文可阅读，但不会作为事实型 RAG 证据。</p>
              )}
              <p className="detail-hint">页码仅在解析器实际提供时显示；知识块 ID 不会被伪装成论文页码。</p>
              <section className="workbench-note-entry" aria-label="论文工作台入口">
                <div className="workbench-note-entry-status">
                  <span>工作台状态</span>
                  {workbenchPaperStatusLoading && <strong>正在检查…</strong>}
                  {!workbenchPaperStatusLoading && workbenchPaperStatus?.status === "available" && <strong className="is-ready">已准备好</strong>}
                  {!workbenchPaperStatusLoading && workbenchPaperStatus?.status === "preparing" && <strong>正在准备论文</strong>}
                  {!workbenchPaperStatusLoading && workbenchPaperStatus?.status === "failed" && <strong className="is-failed">上次导入失败</strong>}
                  {!workbenchPaperStatusLoading && workbenchPaperStatus?.status === "not_registered" && <strong>仅笔记可读</strong>}
                  {!workbenchPaperStatusLoading && workbenchPaperStatus?.status === "unavailable" && <strong>没有可下载来源</strong>}
                  {!workbenchPaperStatusLoading && !workbenchPaperStatus && <strong>状态暂不可用</strong>}
                </div>
                <div className="paper-actions">
                  <button
                    className="paper-question"
                    disabled={workbenchEntryLoading || workbenchPaperStatusLoading || workbenchPaperStatus?.status === "unavailable"}
                    onClick={() => void openCurrentNoteInWorkbench()}
                  >
                    {workbenchEntryLoading
                      ? (workbenchPaperStatus?.status === "not_registered" || workbenchPaperStatus?.status === "failed" ? "正在导入论文…" : "正在打开论文工作台…")
                      : workbenchPaperStatus?.status === "not_registered" || workbenchPaperStatus?.status === "failed"
                        ? "导入论文并进入工作台"
                        : workbenchPaperStatus?.status === "preparing"
                          ? "打开工作台查看准备进度"
                          : "进入工作台研究这篇论文"}
                  </button>
                </div>
                {workbenchEntryError && <p className="form-error" role="alert">{workbenchEntryError}</p>}
                <p className="detail-hint">只有完成论文登记后，工作台才会提供 PDF 阅读、精确选区和论文问答；导入不会改变当前笔记。</p>
              </section>
            </>
          ) : (
            <p className="detail-hint">选择并成功加载一份真实知识后，这里会显示版本和来源。</p>
          )}
        </aside>
          </div>
        )}
      </section>

      {chatOpen && (
        <div className="chat-backdrop" onMouseDown={() => setChatOpen(false)}>
          <aside className="chat-drawer" onMouseDown={(event) => event.stopPropagation()}>
            <header className="chat-header">
              <div><span className="panel-label">知识库问答</span><h2>{scopeCurrentPaper ? "当前论文" : "全部知识"}</h2></div>
              <button className="icon-button" onClick={() => setChatOpen(false)} aria-label="关闭问答">×</button>
            </header>
            <div className="chat-messages">
              {!messages.length && <p className="chat-placeholder">试着问：这篇论文的方法有什么局限？或近期记忆研究有哪些共识？</p>}
              {messages.map((message, index) => <p className={`message ${message.role}`} key={`${message.role}-${index}`}>{message.text}</p>)}
              {pending && (
                <div className="confirmation-card">
                  <strong>当前库证据不足</strong>
                  <p>是否检索并补充少量相关论文？新知识只有通过质量门禁才会入库。</p>
                  <div><button onClick={() => decideSupplement(false)}>暂不补充</button><button className="primary-button" onClick={() => decideSupplement(true)}>补充文献</button></div>
                </div>
              )}
            </div>
            <form className="chat-form" onSubmit={submitQuestion}>
              <textarea value={query} onChange={(event) => setQuery(event.target.value)} placeholder="向知识库提问…" rows={3} />
              <button className="primary-button" disabled={chatLoading}>{chatLoading ? "处理中…" : "发送"}</button>
            </form>
          </aside>
        </div>
      )}

      {topicModalOpen && (
        <div className="topic-backdrop" onMouseDown={() => setTopicModalOpen(false)}>
          <form className="topic-dialog" aria-label="新增研究方向" onSubmit={submitTopic} onMouseDown={(event) => event.stopPropagation()}>
            <header>
              <div><span className="panel-label">研究方向</span><h2>生成第一批论文精读</h2></div>
              <button type="button" className="icon-button" onClick={() => setTopicModalOpen(false)} aria-label="关闭新增方向">×</button>
            </header>
            <p>每次最多处理 3 篇候选。原始 PDF 仅用于当次解析，不会保存到知识库。</p>
            <label htmlFor="topic-name">方向名称</label>
            <input id="topic-name" maxLength={120} value={topicName} onChange={(event) => setTopicName(event.target.value)} placeholder="例如：Agent 记忆" />
            <label htmlFor="topic-query">论文检索词</label>
            <textarea id="topic-query" maxLength={500} rows={3} value={topicQuery} onChange={(event) => setTopicQuery(event.target.value)} placeholder="例如：LLM agent memory" />
            {topicFormError && <p className="form-error">{topicFormError}</p>}
            <button className="primary-button" disabled={topicSubmitting}>{topicSubmitting ? "正在保存…" : "保存并开始抓取"}</button>
          </form>
        </div>
      )}
    </main>
  );
}

/** Extract markdown ATX headings (## / ### / ####) as a TOC. */
type TocEntry = { id: string; text: string; depth: number };
function extractHeadings(markdown: string): TocEntry[] {
  const out: TocEntry[] = [];
  const re = /^(#{2,4})\s+(.+?)\s*$/gm;
  let match: RegExpExecArray | null;
  while ((match = re.exec(markdown)) !== null) {
    const text = match[2].replace(/[*_`]/g, "").trim();
    if (!text) continue;
    out.push({ id: headingSlug(text), text, depth: match[1].length });
  }
  return out;
}

function TableOfContents({ headings }: { headings: TocEntry[] }) {
  if (!headings.length) return null;
  return (
    <nav className="toc-panel" aria-label="本篇目录">
      <span className="toc-label">本篇目录</span>
      <ul>
        {headings.map((h, index) => (
          <li key={`${h.id}-${index}`} className={`toc-depth-${Math.min(h.depth, 4) - 2}`}>
            <a href={`#${h.id}`} onClick={(e) => {
              const target = document.getElementById(h.id);
              if (target) {
                e.preventDefault();
                target.scrollIntoView({ behavior: "smooth", block: "start" });
              }
            }}>{h.text}</a>
          </li>
        ))}
      </ul>
    </nav>
  );
}

function ReadingNotice({
  badge,
  title,
  text,
  children,
}: {
  badge?: string;
  title: string;
  text: string;
  children?: ReactNode;
}) {
  return (
    <section className="reading-notice">
      {badge && <span className="demo-badge">{badge}</span>}
      <h1>{title}</h1>
      <p>{text}</p>
      {children}
    </section>
  );
}
