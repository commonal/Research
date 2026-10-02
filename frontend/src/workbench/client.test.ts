import { describe, expect, it } from "vitest";
import { createFixtureWorkbenchClient } from "./fixtureClient";

describe("WorkbenchClient public seam", () => {
  it("creates a stable session with a papers list and active paper slot", async () => {
    const client = createFixtureWorkbenchClient();
    const created = await client.createSession();

    expect(created.session_id).toMatch(/^session-/);
    expect(created.title).toBe("新会话");
    expect(created.papers).toEqual([]);
    expect(created.active_paper_id).toBeNull();
    expect(created.messages).toEqual([]);
  });

  it("keeps fixture data behind the client seam and isolates sessions", async () => {
    const client = createFixtureWorkbenchClient();
    const first = await client.createSession();
    const second = await client.createSession();

    await client.addFixturePaper(first.session_id, "paper-demo");
    const firstDetail = await client.getSession(first.session_id);
    const secondDetail = await client.getSession(second.session_id);

    expect(firstDetail.papers).toHaveLength(1);
    expect(firstDetail.active_paper_id).toBe("paper-demo");
    expect(secondDetail.papers).toEqual([]);
    expect(secondDetail.active_paper_id).toBeNull();
  });

  it("continues a run as a new attempt under the same run_id, never a new run", async () => {
    const client = createFixtureWorkbenchClient();
    const session = await client.createSession();
    await client.startFixtureExploration!(session.session_id, "比较方法路线", "assistant");

    let detail = await client.getSession(session.session_id);
    const started = detail.exploration_runs![0];
    expect(started.attempt).toBe(1);
    expect(started.attempt_history).toHaveLength(1);
    expect(started.continuation_mode).toBe("persisted_results");
    expect(started.continuation_label).toBe("基于已有结果继续执行");

    await client.cancelFixtureExploration!(session.session_id, started.run_id);
    detail = await client.getSession(session.session_id);
    const cancelled = detail.exploration_runs![0];
    expect(cancelled.run_id).toBe(started.run_id);
    expect(cancelled.current_attempt?.status).toBe("cancelled");

    await client.continueExploration!(session.session_id, started.run_id);
    detail = await client.getSession(session.session_id);
    // One run card: the runs list still holds exactly the same run_id.
    const runs = detail.exploration_runs!;
    expect(runs).toHaveLength(1);
    const continued = runs[0];
    expect(continued.run_id).toBe(started.run_id);
    expect(continued.attempt).toBe(2);
    expect(continued.status).toBe("running");
    expect(continued.continuation_label).toBe("基于已有结果继续执行");
    expect(continued.current_attempt_id).not.toBe(cancelled.current_attempt_id);
    expect(continued.current_attempt?.attempt_no).toBe(2);
    // New attempt starts with a fresh attempt-scoped budget.
    expect(continued.current_attempt?.budget_used).toEqual({ tool_calls: 0, block_reads: 0 });
    // Attempt history keeps the terminal attempt immutable.
    expect(continued.attempt_history).toHaveLength(2);
    expect(continued.attempt_history?.[0].attempt_id).toBe(cancelled.current_attempt_id);
    expect(continued.attempt_history?.[0].status).toBe("cancelled");
  });

  it("refuses to continue while the current attempt is still live", async () => {
    const client = createFixtureWorkbenchClient();
    const session = await client.createSession();
    await client.startFixtureExploration!(session.session_id, "比较方法路线", "assistant");
    const detail = await client.getSession(session.session_id);
    const run = detail.exploration_runs![0];
    await expect(client.continueExploration!(session.session_id, run.run_id)).rejects.toThrow("无法继续");
  });
});
