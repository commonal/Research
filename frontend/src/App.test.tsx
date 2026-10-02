import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
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
  retryResearchTopic,
  updateResearchTopic,
  approveReviewDraft,
  rejectReviewDraft,
} from "./api";
import type { ProductionRun, ResearchTopic } from "./types";

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  fetchKnowledge: vi.fn(),
  fetchKnowledgeDetail: vi.fn(),
  fetchReviewDraft: vi.fn(),
  fetchReviewDrafts: vi.fn(),
  fetchResearchTopics: vi.fn(),
  fetchSchedulerStatus: vi.fn(),
  createResearchTopic: vi.fn(),
  retryResearchTopic: vi.fn(),
  updateResearchTopic: vi.fn(),
  approveReviewDraft: vi.fn(),
  rejectReviewDraft: vi.fn(),
  fetchProductionRun: vi.fn(),
  startChat: vi.fn(),
  resumeChat: vi.fn(),
}));

const topic: ResearchTopic = {
  topic_id: "topic-1",
  name: "Agent 记忆",
  query: "LLM agent memory",
  domain: "topic:topic-1",
  created_at: "2026-08-22T00:00:00Z",
  enabled: true,
  daily_limit: 3,
  last_successful_discovery_at: null,
};

function run(status: ProductionRun["status"], overrides: Partial<ProductionRun> = {}): ProductionRun {
  return {
    run_id: "run-1",
    topic_id: topic.topic_id,
    status,
    limit: 3,
    candidate_count: 0,
    published_count: 0,
    failed_count: 0,
    error_code: null,
    error_summary: null,
    created_at: "2026-08-22T00:00:00Z",
    started_at: null,
    finished_at: null,
    trigger: "manual",
    window_start: null,
    window_end: "2026-08-22T00:00:00Z",
    scheduled_for: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(fetchKnowledge).mockResolvedValue([]);
  vi.mocked(fetchKnowledgeDetail).mockReset();
  vi.mocked(fetchReviewDraft).mockReset();
  vi.mocked(fetchReviewDrafts).mockResolvedValue([]);
  vi.mocked(approveReviewDraft).mockReset();
  vi.mocked(rejectReviewDraft).mockReset();
  vi.mocked(fetchResearchTopics).mockResolvedValue([]);
  vi.mocked(fetchSchedulerStatus).mockResolvedValue({
    enabled: true,
    timezone: "Asia/Shanghai",
    daily_time: "08:00",
    next_run_at: "2026-08-23T00:00:00Z",
  });
  vi.mocked(fetchProductionRun).mockReset();
  vi.mocked(createResearchTopic).mockReset();
  vi.mocked(retryResearchTopic).mockReset();
  vi.mocked(updateResearchTopic).mockReset();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("research topic UI", () => {
  it("renders durable evidence boundaries without inventing a page locator", async () => {
    vi.mocked(fetchKnowledge).mockResolvedValue([{
      knowledge_id: "kp:detail",
      knowledge_version: "2026-08-22T00:00:00Z",
      title: "Evidence-aware paper",
      domain: "test",
      evidence_level: "full_text_multimodal",
      source_url: "https://example.com/paper",
      provenance_status: "complete",
    }]);
    vi.mocked(fetchKnowledgeDetail).mockResolvedValue({
      knowledge_id: "kp:detail",
      knowledge_version: "2026-08-22T00:00:00Z",
      title: "Evidence-aware paper",
      domain: "test",
      evidence_level: "full_text_multimodal",
      source_urls: ["https://example.com/paper"],
      markdown: "# Evidence-aware paper\n\n正文。",
      provenance_status: "complete",
      reading_mode: "deep_reading",
      evidence_model: "section_anchors",
      reading_sections: [
        { section_name: "experiments", anchor_ids: ["source:table"] },
        { section_name: "method", anchor_ids: ["source:section-only"] },
      ],
      anchors: [
        {
          anchor_id: "source:table",
          source_url: "https://example.com/paper",
          section: "Results",
          page_start: 4,
          page_end: 4,
          figure_or_table: "Table 1: Accuracy",
          block_kind: "table",
          parse_status: "available",
          locator_completeness: "exact",
          bbox: null,
          evidence_excerpt: "Parent 67.75%, continuation 90.50%.",
          excerpt_sha256: "test",
        },
        {
          anchor_id: "source:section-only",
          source_url: "https://example.com/paper",
          section: "Method",
          page_start: null,
          page_end: null,
          figure_or_table: null,
          block_kind: "text",
          parse_status: "available",
          locator_completeness: "section_only",
          bbox: null,
          evidence_excerpt: "Method text.",
          excerpt_sha256: "test2",
        },
      ],
    });
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: /Evidence-aware paper/ }));

    expect(screen.queryByRole("button", { name: "问知识库" })).toBeNull();
    expect(await screen.findByRole("list", { name: "证据边界" })).toBeTruthy();
    expect(screen.getByRole("status", { name: "分阶段精读：模型解读与证据主张已分离" })).toBeTruthy();
    expect(screen.getByText("表格")).toBeTruthy();
    expect(screen.getByText("第 4 页")).toBeTruthy();
    expect(screen.getByText("章节：Method")).toBeTruthy();
    expect(screen.getByRole("list", { name: "逐节证据" })).toBeTruthy();
    expect(screen.getByText("实验与结果")).toBeTruthy();
    expect(screen.getByText(/不会被伪装成论文页码/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "在知识库内提问" })).toBeNull();
  });

  it("labels schema-v2 details as the old evidence model", async () => {
    vi.mocked(fetchKnowledge).mockResolvedValue([{
      knowledge_id: "kp:legacy-v2",
      knowledge_version: "2026-08-22T00:00:00Z",
      title: "Old evidence model",
      domain: "test",
      evidence_level: "full_text_text",
      source_url: "https://example.com/paper",
      provenance_status: "complete",
    }]);
    vi.mocked(fetchKnowledgeDetail).mockResolvedValue({
      knowledge_id: "kp:legacy-v2",
      knowledge_version: "2026-08-22T00:00:00Z",
      title: "Old evidence model",
      domain: "test",
      evidence_level: "full_text_text",
      source_urls: ["https://example.com/paper"],
      markdown: "# Old evidence model\n\n正文。",
      provenance_status: "complete",
      reading_mode: "legacy",
      evidence_model: "legacy_v2",
      reading_sections: [],
      anchors: [],
    });

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: /Old evidence model/ }));

    expect(await screen.findByText("旧证据模型：该版本没有逐节来源映射")).toBeTruthy();
  });

  it("guides an empty library, rejects blank input, and displays a created topic", async () => {
    vi.mocked(createResearchTopic).mockResolvedValue({ topic, run: run("queued") });
    render(<App />);

    // Empty timeline shows the onboarding call-to-action and the topics-manage entry.
    expect(await screen.findByText("先生成第一份论文精读")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "研究方向" }));
    expect(await screen.findByText("还没有研究方向")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "关闭研究方向" }));

    fireEvent.click(screen.getByRole("button", { name: "＋ 添加研究方向" }));
    fireEvent.change(screen.getByLabelText("方向名称"), { target: { value: "  " } });
    fireEvent.change(screen.getByLabelText("论文检索词"), { target: { value: "LLM agent memory" } });
    fireEvent.submit(screen.getByRole("form", { name: "新增研究方向" }));
    expect(await screen.findByText("请填写方向名称和论文检索词")).toBeTruthy();
    expect(createResearchTopic).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("方向名称"), { target: { value: "Agent 记忆" } });
    fireEvent.submit(screen.getByRole("form", { name: "新增研究方向" }));

    expect(await screen.findByRole("button", { name: "研究方向" })).toBeTruthy();
    expect(createResearchTopic).toHaveBeenCalledWith("Agent 记忆", "LLM agent memory");
  });

  it("shows a review-only preview and keeps approval separate from current reading", async () => {
    const knowledgeId = "kp:review";
    vi.mocked(fetchKnowledge).mockResolvedValue([{
      knowledge_id: knowledgeId,
      knowledge_version: "2026-08-22T00:00:00Z",
      title: "Reviewable paper",
      domain: "test",
      evidence_level: "full_text_text",
      source_url: "https://example.com/review",
      provenance_status: "complete",
    }]);
    vi.mocked(fetchKnowledgeDetail).mockResolvedValue({
      knowledge_id: knowledgeId,
      knowledge_version: "2026-08-22T00:00:00Z",
      title: "Reviewable paper",
      domain: "test",
      evidence_level: "full_text_text",
      source_urls: ["https://example.com/review"],
      markdown: "# 正式版本\n\n旧知识。",
      provenance_status: "complete",
      anchors: [],
    });
    vi.mocked(fetchReviewDrafts).mockResolvedValue([{
      draft_id: "review:one",
      knowledge_id: knowledgeId,
      knowledge_version: "2026-08-23T00:00:00Z",
      title: "Reviewable paper",
      domain: "test",
      status: "needs_review",
      source_urls: ["https://example.com/review"],
      quality_issues: [{ code: "missing_evidence_facet", severity: "blocking" }],
      evidence_boundary: "实验块未充分覆盖。",
      created_at: "2026-08-23T00:00:00Z",
      updated_at: "2026-08-23T00:00:00Z",
    }]);
    vi.mocked(fetchReviewDraft).mockResolvedValue({
      draft_id: "review:one",
      knowledge_id: knowledgeId,
      knowledge_version: "2026-08-23T00:00:00Z",
      title: "Reviewable paper",
      domain: "test",
      status: "needs_review",
      source_id: "review",
      source_urls: ["https://example.com/review"],
      quality_issues: [{ code: "missing_evidence_facet", severity: "blocking" }],
      evidence_boundary: "实验块未充分覆盖。",
      created_at: "2026-08-23T00:00:00Z",
      updated_at: "2026-08-23T00:00:00Z",
      markdown: "# 新精读\n\n## 核心直觉\n\n仅供审核。",
      content_sha256: "hash",
    });
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: /Reviewable paper/ }));

    expect(await screen.findByRole("region", { name: "待审核精读预览" })).toBeTruthy();
    expect(screen.getByText("不会进入 RAG")).toBeTruthy();
    expect(screen.getByText("missing_evidence_facet")).toBeTruthy();
    expect(screen.getByRole("button", { name: "确认发布" })).toBeTruthy();
    expect(screen.getByText("正式版本")).toBeTruthy();
  });

  it("polls an active run, refreshes knowledge after publication, and stops after unmount", async () => {
    vi.useFakeTimers();
    vi.mocked(fetchResearchTopics).mockResolvedValue([{ topic, latest_run: run("running") }]);
    vi.mocked(fetchProductionRun).mockResolvedValue(run("completed", { candidate_count: 1, published_count: 1 }));
    const view = render(<App />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    fireEvent.click(screen.getByRole("button", { name: "研究方向" }));
    expect(screen.getByText("Agent 记忆")).toBeTruthy();
    await act(async () => {
      vi.advanceTimersByTime(2_000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(fetchProductionRun).toHaveBeenCalledWith("run-1", expect.any(AbortSignal));
    expect(fetchKnowledge).toHaveBeenCalledTimes(2);
    vi.mocked(fetchProductionRun).mockClear();
    view.unmount();
    await act(async () => { vi.advanceTimersByTime(4_000); });
    expect(fetchProductionRun).not.toHaveBeenCalled();
  });

  it("shows only safe failure copy and reuses an existing active run when retry conflicts", async () => {
    const failed = run("failed", {
      error_code: "deepseek_not_configured",
      error_summary: "provider raw body sk-secret C:\\private\\paper.pdf",
    });
    vi.mocked(fetchResearchTopics).mockResolvedValue([{ topic, latest_run: failed }]);
    vi.mocked(retryResearchTopic).mockRejectedValue(new ActiveRunConflictError("run-existing"));
    vi.mocked(fetchProductionRun).mockResolvedValue(run("running", { run_id: "run-existing" }));
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "研究方向" }));
    expect(await screen.findByText("尚未配置 DeepSeek，配置后可重新抓取。")).toBeTruthy();
    expect(document.body.textContent).not.toContain("sk-secret");
    fireEvent.click(screen.getByRole("button", { name: "重新抓取" }));

    await waitFor(() => expect(fetchProductionRun).toHaveBeenCalledWith("run-existing"));
    expect(await screen.findByText("抓取中")).toBeTruthy();
  });

  it("shows scheduler state and updates pause and daily-limit controls", async () => {
    vi.mocked(fetchResearchTopics).mockResolvedValue([{
      topic,
      latest_run: run("completed", { trigger: "scheduled", scheduled_for: "2026-08-22T00:00:00Z" }),
    }]);
    vi.mocked(updateResearchTopic)
      .mockResolvedValueOnce({ ...topic, enabled: false })
      .mockResolvedValueOnce({ ...topic, enabled: false, daily_limit: 2 });
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "研究方向" }));
    expect(await screen.findByText(/下次自动更新/)).toBeTruthy();
    expect(screen.getAllByText("自动更新").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "暂停Agent 记忆自动更新" }));
    expect(await screen.findByText("已暂停")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Agent 记忆每日篇数"), { target: { value: "2" } });

    await waitFor(() => expect(updateResearchTopic).toHaveBeenLastCalledWith(topic.topic_id, { daily_limit: 2 }));
    expect((screen.getByLabelText("Agent 记忆每日篇数") as HTMLSelectElement).value).toBe("2");
  });

  it("does not fake a paused state when updating the topic fails", async () => {
    vi.mocked(fetchResearchTopics).mockResolvedValue([{ topic, latest_run: run("completed") }]);
    vi.mocked(updateResearchTopic).mockRejectedValue(new Error("无法更新研究方向"));
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "研究方向" }));
    fireEvent.click(await screen.findByRole("button", { name: "暂停Agent 记忆自动更新" }));

    expect(await screen.findByText("无法更新研究方向")).toBeTruthy();
    expect(screen.getByText("自动更新")).toBeTruthy();
    expect(screen.queryByText("已暂停")).toBeNull();
  });
});
