import { afterEach, describe, expect, it, vi } from "vitest";
import { createHttpWorkbenchChatClient } from "./httpChatClient";

afterEach(() => vi.unstubAllGlobals());

describe("production workbench chat HTTP client", () => {
  it("sends the exact fixed scope and projects server citations", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      message_id: "assistant-1",
      role: "assistant",
      text: "回答 [normalized:paper-1:text:method]",
      generation_status: "completed",
      created_at: "2026-08-31T16:00:00Z",
      metadata: {
        scope: "selection",
        context_chips: [{
          paper_id: "paper-1", block_id: "normalized:paper-1:text:method",
          section_path: ["Method"], page: 2, truncated: false,
        }],
      },
      citations: [{
        block_id: "normalized:paper-1:text:method", status: "resolved",
        locator: { page: 2, bbox: [10, 20, 50, 60], section_path: ["Method"] },
      }],
    }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const client = createHttpWorkbenchChatClient();

    const result = await client.sendFixedMessage("session:中文", "方法是什么？", {
      paper_id: "paper-1", scope: "selection",
      block_id: "normalized:paper-1:text:method", label: "Method",
      status: "attached", page: 2,
    });

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/workbench/sessions/session%3A%E4%B8%AD%E6%96%87/messages",
    );
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(expect.objectContaining({
      scope: "selection", paper_id: "paper-1",
      block_id: "normalized:paper-1:text:method",
    }));
    expect(result.citations[0]).toEqual(expect.objectContaining({
      status: "resolved", page: 2,
      bbox: { x: 10, y: 20, width: 40, height: 40 },
    }));
  });

  it("surfaces production failure without issuing a fixture fallback request", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("unavailable", { status: 503 }));
    vi.stubGlobal("fetch", fetchMock);
    const client = createHttpWorkbenchChatClient();

    await expect(client.sendFixedMessage("session-1", "问题")).rejects.toThrow(
      "固定范围问答暂不可用",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
