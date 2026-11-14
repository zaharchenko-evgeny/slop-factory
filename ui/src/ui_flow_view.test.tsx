import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { Snapshot } from "./snapshot";
import { WorkflowFlow } from "./views/WorkflowFlow";

const base: Snapshot = {
  generated_at: "t",
  workflow: {
    stages: [{ id: "ready" }, { id: "in-dev" }, { id: "idle" }],
    nodes: [
      { id: "dev", role: "developer", enabled: true, background: false, stages: ["in-dev"], outcomes: [
        { name: "done", to: "ready", condition: { ci: "green" }, enabled: true, comment_only: false, else: "blocked" },
        { name: "note", to: null, condition: null, enabled: false, comment_only: true } ] },
      { id: "retro", enabled: true, background: true, stages: [], outcomes: [] },
      { id: "human", enabled: true, background: false, stages: [], outcomes: [] },
    ],
    loops: [["in-dev", "ready"]],
    branches: [{ kind: "stage", stage: "in-dev", targets: ["ready", "blocked"] }, { kind: "else", node: "dev", outcome: "done", else: "blocked" }],
  },
  tickets: [
    { key: "k1", id: "1", title: "First", repository: "o/r", stage: "in-dev" },
    { key: "k2", id: "2", title: null, repository: null, stage: "ready" },
  ],
  sources: [], errors: [], attention: { human_review: 0, human_attention: 0 },
};
const stage = (id: string) => screen.getByTestId(`stage-${id}`);

describe("ui_flow_view", () => {
  it("shows ticket cards inside their stage nodes, collapsed by default", () => {
    render(<WorkflowFlow snapshot={base} />);
    expect(within(stage("in-dev")).getByText("First")).toBeInTheDocument();
    expect(within(stage("ready")).getByTestId("ticket-card")).toHaveTextContent("#2#2");
    expect(within(stage("idle")).queryAllByTestId("ticket-card")).toHaveLength(0);
    expect(screen.queryByTestId("node-dev")).toBeNull();
    expect(within(stage("in-dev")).getByRole("button")).toHaveAttribute("aria-expanded", "false");
  });

  it("expands a stage to nodes, outcomes, conditions, branches and loops", async () => {
    render(<WorkflowFlow snapshot={base} />);
    expect(within(stage("in-dev")).getByText("loop")).toBeInTheDocument();
    await userEvent.click(within(stage("in-dev")).getByRole("button"));
    const node = within(stage("in-dev")).getByTestId("node-dev");
    expect(node).toHaveTextContent("done → ready if ci: green else blocked");
    expect(node).toHaveTextContent("note (comment only) (disabled)");
    expect(within(stage("in-dev")).getAllByTestId("branch").map((b) => b.textContent)).toEqual(["branches to ready, blocked", "dev.done else blocked"]);
    await userEvent.click(within(stage("idle")).getByRole("button"));
    expect(within(stage("idle")).getByText("No execution nodes.")).toBeInTheDocument();
    await userEvent.click(within(stage("in-dev")).getByRole("button"));
    expect(screen.queryByTestId("node-dev")).toBeNull();
  });

  it("shows background and cross-stage nodes outside the stages, without a ticket card", () => {
    render(<WorkflowFlow snapshot={base} />);
    const bg = screen.getByRole("region", { name: "Background nodes" });
    expect(within(bg).getByTestId("node-retro")).toHaveTextContent("background");
    expect(within(bg).queryAllByTestId("ticket-card")).toHaveLength(0);
    expect(within(screen.getByRole("region", { name: "Nodes for any stage" })).getByTestId("node-human")).toBeInTheDocument();
  });

  it("renders a snapshot without a graph", () => {
    render(<WorkflowFlow snapshot={{ ...base, workflow: { stages: [{ id: "ready" }] } }} />);
    expect(stage("ready")).toBeInTheDocument();
  });

  it("keeps the viewport and expanded stages when the snapshot changes", async () => {
    const { rerender } = render(<WorkflowFlow snapshot={base} />);
    await userEvent.click(within(stage("in-dev")).getByRole("button"));
    const canvas = screen.getByTestId("view-workflow");
    canvas.scrollLeft = 120; canvas.scrollTop = 40;
    fireEvent.scroll(canvas);
    canvas.scrollLeft = 0; canvas.scrollTop = 0; // what a remount or layout reset would do
    rerender(<WorkflowFlow snapshot={{ ...base, generated_at: "t2", tickets: [...base.tickets, { key: "k3", id: "3", title: "Third", repository: null, stage: "ready" }] }} />);
    expect(screen.getByTestId("view-workflow")).toBe(canvas);
    expect([canvas.scrollLeft, canvas.scrollTop]).toEqual([120, 40]);
    expect(within(stage("in-dev")).getByTestId("node-dev")).toBeInTheDocument();
    expect(within(stage("ready")).getByText("Third")).toBeInTheDocument();
  });
});
