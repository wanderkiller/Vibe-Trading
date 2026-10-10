import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AlphaKeelHandoff } from "../AlphaKeelHandoff";

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ request: mocks.request }));
vi.mock("sonner", () => ({ toast: { success: mocks.success, error: mocks.error } }));

function state(over: Record<string, unknown> = {}) {
  return {
    ir: true,
    strategy_id: "illiq-v2",
    name: "Illiquidity v2",
    window: { start_day: "2024-01-01", end_day: "2025-01-01" },
    robustness: { grade: "B", score: 0.7, caps: ["cap_a"], schema: "s", sha256: "x" },
    precheck: [
      { id: "P1", label: "check one", result: "PASS", observed: 1, note: null },
      { id: "P2", label: "check two", result: "FAIL", observed: 2, note: null },
      { id: "P3", label: "check three", result: "N/A", observed: null, note: null },
    ],
    protocol: { version: "1", sha256: "y" },
    target: { container_path: "/c/x", host_path: "/host/handoff/illiq-v2" },
    exported: null,
    credential: true,
    ...over,
  };
}

describe("AlphaKeelHandoff", () => {
  beforeEach(() => {
    mocks.request.mockReset();
    mocks.success.mockReset();
    mocks.error.mockReset();
  });

  it("renders nothing for non-IR runs and on fetch error", async () => {
    mocks.request.mockResolvedValueOnce(state({ ir: false }));
    const { container, rerender } = render(<AlphaKeelHandoff runId="r1" />);
    await waitFor(() => expect(mocks.request).toHaveBeenCalledTimes(1));
    expect(container.querySelector("button")).toBeNull();
    mocks.request.mockRejectedValueOnce(new Error("boom"));
    vi.spyOn(console, "error").mockImplementation(() => {});
    rerender(<AlphaKeelHandoff runId="r2" />);
    await waitFor(() => expect(mocks.request).toHaveBeenCalledTimes(2));
    expect(container.querySelector("button")).toBeNull();
  });

  it("is disabled without robustness or credential", async () => {
    mocks.request.mockResolvedValueOnce(state({ robustness: null }));
    const { unmount } = render(<AlphaKeelHandoff runId="r1" />);
    const btn = await screen.findByRole("button");
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("title", "Run ir robustness first");
    unmount();
    mocks.request.mockResolvedValueOnce(state({ credential: false }));
    render(<AlphaKeelHandoff runId="r1" />);
    const btn2 = await screen.findByRole("button");
    expect(btn2).toBeDisabled();
    expect(btn2).toHaveAttribute("title", "Research credential not configured");
  });

  it("shows precheck rows, posts once on confirm and switches to exported", async () => {
    const user = userEvent.setup();
    mocks.request.mockResolvedValueOnce(state());
    mocks.request.mockResolvedValueOnce({ strategy_id: "illiq-v2", path: "/host/handoff/illiq-v2", files: [], at: "t", grade: "B" });
    render(<AlphaKeelHandoff runId="r1" />);
    await user.click(await screen.findByRole("button", { name: /Hand off to AlphaKeel/ }));
    expect(screen.getByText("PASS")).toBeInTheDocument();
    expect(screen.getByText("FAIL")).toBeInTheDocument();
    expect(screen.getByText("N/A")).toBeInTheDocument();
    expect(screen.getByText("/host/handoff/illiq-v2")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Export" }));
    await waitFor(() => expect(mocks.success).toHaveBeenCalledTimes(1));
    const posts = mocks.request.mock.calls.filter((c) => c[1]?.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0][0]).toBe("/runs/r1/alphakeel/export");
    expect(await screen.findByRole("button", { name: /Handed off/ })).toBeInTheDocument();
  });

  it("shows the server message on export error", async () => {
    const user = userEvent.setup();
    mocks.request.mockResolvedValueOnce(state());
    mocks.request.mockRejectedValueOnce(Object.assign(new Error("robustness mismatch"), { code: "robustness.mismatch" }));
    render(<AlphaKeelHandoff runId="r1" />);
    await user.click(await screen.findByRole("button", { name: /Hand off to AlphaKeel/ }));
    await user.click(screen.getByRole("button", { name: "Export" }));
    await waitFor(() => expect(mocks.error).toHaveBeenCalledWith("robustness mismatch"));
    expect(mocks.success).not.toHaveBeenCalled();
  });

  it("opens the re-export dialog when already exported", async () => {
    const user = userEvent.setup();
    mocks.request.mockResolvedValueOnce(
      state({ exported: { path: "/host/handoff/illiq-v2", at: "t", strategy_id: "illiq-v2", files: [] } }),
    );
    render(<AlphaKeelHandoff runId="r1" />);
    const btn = await screen.findByRole("button", { name: /Handed off/ });
    expect(btn).toHaveAttribute("title", "/host/handoff/illiq-v2");
    await user.click(btn);
    expect(screen.getByText(/idempotently overwrites/)).toBeInTheDocument();
  });
});
