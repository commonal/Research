import { afterEach, describe, expect, it, vi } from "vitest";
import { createHttpWorkbenchClient } from "./httpWorkbenchClient";

const ok = (payload: unknown, status = 200) =>
  new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => vi.restoreAllMocks());

describe("httpWorkbenchClient workspace state", () => {
  it("lists session metadata without hydrating every historical session", async () => {
    const session = {
      session_id: "session-1",
      title: "研究会话",
      lifecycle: "active",
      paper_panel_open: false,
      active_paper_id: null,
      created_at: "2026-09-16T00:00:00.000Z",
      workspace_id: "workspace-1",
      workspace_title: "研究项目",
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(ok({ items: [session] }));
    const client = createHttpWorkbenchClient("/api");

    const result = await client.listSessions();

    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      session_id: "session-1",
      title: "研究会话",
      workspace_id: "workspace-1",
      papers: [],
      messages: [],
      exploration_runs: [],
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith("/api/workbench/sessions?lifecycle=active", undefined);
  });

  it("fetchWorkspace projects the real HTTP response", async () => {
    const state = { workspace_id: "ws-1", research_question: "q", anchor_paper_id: "p", status: "initial_research", workspace_revision: 3, research_map: [], subquestions: [], evidence: [] };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(ok(state));
    const client = createHttpWorkbenchClient("/api");

    const result = await client.fetchWorkspace!("session-1");

    expect(result).toEqual(state);
    expect(fetchMock).toHaveBeenCalledWith("/api/workbench/workspaces/session-1");
  });

  it("fetchWorkspace returns null (no fixture fallback) on 404", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(ok({ detail: "not found" }, 404));
    const client = createHttpWorkbenchClient("/api");

    const result = await client.fetchWorkspace!("session-1");

    expect(result).toBeNull();
  });

  it("fetchWorkspace throws (does not silently fall back) on 500", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(ok({}, 500));
    const client = createHttpWorkbenchClient("/api");

    await expect(client.fetchWorkspace!("session-1")).rejects.toThrow("无法读取工作区状态");
  });

  it("resolveWorkspaceDecision POSTs the decision body", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      ok({ decision: { decision_id: "D-1", kind: "patch_approval", status: "approved" } }),
    );
    const client = createHttpWorkbenchClient("/api");

    await client.resolveWorkspaceDecision!("session-1", "D-1", true, "同意");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/workbench/workspaces/session-1/decisions/D-1/resolve",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ approved: true, decision: "同意" }) }),
    );
  });
});
