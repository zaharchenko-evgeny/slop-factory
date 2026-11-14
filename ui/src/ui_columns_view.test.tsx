import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { Snapshot, Ticket } from "./snapshot";
import { Columns } from "./views/Columns";

const mk = (o: Partial<Ticket>): Ticket => ({ key: "k", id: "1", title: "T", repository: "o/r", stage: "dev", ...o });
const snap = (tickets: Ticket[]): Snapshot => ({
  generated_at: "t", workflow: { stages: [{ id: "dev" }, { id: "idle" }] }, tickets, sources: [], errors: [], attention: { human_review: 0, human_attention: 0 },
});
const age = (seconds: number | null, kind = "active", state = "ok") => ({ state: state as "ok", seconds, kind, warn: false });

describe("ui_columns_view", () => {
  it("puts each ticket in its stage column with ID, title, repository, stage and age", () => {
    render(<Columns snapshot={snap([mk({ key: "a", id: "7", title: "Seven", stage_age: age(300) }), mk({ key: "b", id: "8", stage: "idle" })])} />);
    const card = within(screen.getByTestId("column-dev")).getByTestId("ticket-card");
    expect(card).toHaveTextContent("Seven");
    expect(card).toHaveTextContent("#7");
    expect(card).toHaveTextContent("o/r");
    expect(card).toHaveTextContent("dev");
    expect(within(card).getByTestId("card-age")).toHaveTextContent("5m");
    expect(within(screen.getByTestId("column-idle")).getAllByTestId("ticket-card")).toHaveLength(1);
  });

  it("shows the tracker only when equal short IDs come from different tracker instances", () => {
    const { rerender } = render(<Columns snapshot={snap([mk({ key: "a", tracker: "gh|o/r" }), mk({ key: "b", tracker: "gh|o/r" })])} />);
    expect(screen.queryAllByTestId("card-tracker")).toHaveLength(0);
    rerender(<Columns snapshot={snap([mk({ key: "a", tracker: "gh|o/a" }), mk({ key: "b", tracker: "gh|o/b" })])} />);
    expect(screen.getAllByTestId("card-tracker").map((e) => e.textContent)).toEqual([" · gh|o/a", " · gh|o/b"]);
  });

  it("marks missing fields and keeps the selection across a new snapshot", async () => {
    const t = mk({ key: "a", title: null, repository: null });
    const { rerender } = render(<Columns snapshot={snap([t])} />);
    const card = screen.getByTestId("ticket-card");
    expect(card).toHaveTextContent("Title missing");
    expect(card).toHaveTextContent("repository missing");
    await userEvent.click(within(card).getByRole("button"));
    expect(screen.getByTestId("ticket-details")).toBeInTheDocument();
    rerender(<Columns snapshot={snap([{ ...t, stage: "idle" }])} />);
    expect(screen.getByTestId("ticket-details")).toBeInTheDocument();
    await userEvent.click(screen.getByText("Close"));
    expect(screen.queryByTestId("ticket-details")).toBeNull();
  });
});
