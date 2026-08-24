import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ActiveRunConflictError,
  createResearchTopic,
  fetchProductionRun,
  fetchResearchTopics,
  fetchSchedulerStatus,
  retryResearchTopic,
  updateResearchTopic,
} from "./api";

const run = {
  run_id: "run:1/中文",
  topic_id: "topic:1/中文",
  status: "queued" as const,
  limit: 3,
  candidate_count: 0,
  published_count: 0,
  failed_count: 0,
  error_code: null,
  error_summary: null,
  created_at: "2026-08-22T00:00:00Z",
  started_at: null,
  finished_at: null,
  trigger: "manual" as const,
  window_start: null,
  window_end: "2026-08-22T00:00:00Z",
  scheduled_for: null,
};

afterEach(() => vi.unstubAllGlobals());

describe("research topic API", () => {
  it("parses topic creation and listing responses", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ topic: { topic_id: run.topic_id }, run }), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [] }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const created = await createResearchTopic("Agent 记忆", "LLM agent memory");
    const listed = await fetchResearchTopics();

    expect(created.run.run_id).toBe(run.run_id);
    expect(listed).toEqual([]);
    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/research-topics", expect.objectContaining({ method: "POST" }));
  });

  it("encodes topic and run identifiers in request paths", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(run), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(run), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await retryResearchTopic(run.topic_id);
    await fetchProductionRun(run.run_id);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/research-topics/topic%3A1%2F%E4%B8%AD%E6%96%87/runs");
    expect(fetchMock.mock.calls[1][0]).toBe("/api/production-runs/run%3A1%2F%E4%B8%AD%E6%96%87");
  });

  it("maps an active-run conflict to its existing run id", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      detail: { code: "active_run_exists", run_id: "run-existing" },
    }), { status: 409 })));

    await expect(retryResearchTopic("topic-1")).rejects.toEqual(
      expect.objectContaining<Partial<ActiveRunConflictError>>({ runId: "run-existing" }),
    );
  });

  it("updates encoded topic settings and reads scheduler status", async () => {
    const topic = {
      topic_id: run.topic_id,
      name: "Agent 记忆",
      query: "agent memory",
      domain: "topic:1",
      created_at: "2026-08-22T00:00:00Z",
      enabled: false,
      daily_limit: 2,
      last_successful_discovery_at: null,
    };
    const scheduler = {
      enabled: true,
      timezone: "Asia/Shanghai",
      daily_time: "08:00",
      next_run_at: "2026-08-23T00:00:00Z",
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(topic), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(scheduler), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await updateResearchTopic(run.topic_id, { enabled: false, daily_limit: 2 });
    await fetchSchedulerStatus();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/research-topics/topic%3A1%2F%E4%B8%AD%E6%96%87");
    expect(fetchMock.mock.calls[0][1]).toEqual(expect.objectContaining({
      method: "PATCH",
      body: JSON.stringify({ enabled: false, daily_limit: 2 }),
    }));
    expect(fetchMock.mock.calls[1][0]).toBe("/api/scheduler");
  });
});
