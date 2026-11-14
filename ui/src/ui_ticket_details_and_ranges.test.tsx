import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { StageAge, Ticket } from "./snapshot";
import { Columns } from "./views/Columns";
import { formatAge, isAmber } from "./views/ticketFormat";

const mk = (o: Partial<Ticket>): Ticket => ({ key: "k", id: "1", title: "T", repository: "o/r", stage: "dev", ...o });
const ageOf = (seconds: number | null, kind: string | null, state: StageAge["state"] = "ok"): StageAge => ({ state, seconds, kind, warn: false });

describe("ui_ticket_details_and_ranges", () => {
  it.each([
    ["exactly one hour", 3600, "active", false],
    ["one second over an hour", 3601, "active", true],
    ["a day in an active stage", 90000, "active", true],
    ["a day in a waiting stage", 90000, "waiting", false],
    ["a day in a final stage", 90000, "final", false],
    ["a stage without a kind", 90000, null, false],
    ["unknown entry time", null, "active", false],
  ])("amber warning: %s", (_n, seconds, kind, amber) => {
    expect(isAmber(mk({ stage_age: ageOf(seconds, kind, seconds == null ? "unknown" : "ok") }))).toBe(amber);
  });
  it("no age at all is not amber", () => expect(isAmber(mk({}))).toBe(false));

  it.each([
    [null, "age unknown"], [0, "<1m"], [59, "<1m"], [60, "1m"], [3599, "59m"], [3600, "1h 0m"], [3 * 3600 + 125, "3h 2m"],
    [86400, "1d 0h"], [2 * 86400 + 5 * 3600 + 59, "2d 5h"], [Number.NaN, "age unknown"],
  ])("formats %s as %s", (seconds, text) => expect(formatAge(ageOf(seconds, "active"))).toBe(text));
  it.each(["active", "waiting", "final", null])("missing entry time reads 'age unknown' for kind %s, exempt or not", (kind) => {
    for (const state of ["exempt", "unknown", "ok"] as const) expect(formatAge(ageOf(null, kind, state))).toBe("age unknown");
    expect(formatAge(undefined)).toBe("age unknown");
  });

  it("card shows amber only past an hour and 'age unknown' without entry time", () => {
    const tickets = [
      mk({ key: "a", id: "1", stage_age: ageOf(7200, "active") }),
      mk({ key: "b", id: "2", stage_age: ageOf(60, "active") }),
      mk({ key: "c", id: "3", stage_age: ageOf(null, "active", "unknown") }),
    ];
    render(<Columns snapshot={{ generated_at: "t", workflow: { stages: [{ id: "dev" }] }, tickets, sources: [], errors: [], attention: { human_review: 0, human_attention: 0 } }} />);
    const cards = screen.getAllByTestId("ticket-card");
    expect(cards.map((c) => c.getAttribute("data-amber"))).toEqual(["true", "false", "false"]);
    expect(within(cards[0]).getByText(/amber/)).toBeInTheDocument();
    expect(within(cards[2]).getByTestId("card-age")).toHaveTextContent("age unknown");
  });

  const open = async (t: Ticket) => {
    render(<Columns snapshot={{ generated_at: "t", workflow: { stages: [{ id: "dev" }] }, tickets: [t], sources: [], errors: [], attention: { human_review: 0, human_attention: 0 } }} />);
    await userEvent.click(within(screen.getByTestId("ticket-card")).getByRole("button"));
    return screen.getByTestId("ticket-details");
  };

  it("shows every missing field explicitly", async () => {
    const d = await open(mk({ title: null }));
    for (const s of ["Title missing", "Linked PR missing", "CI missing", "Blocker count missing", "Node match missing", "Stage history missing", "Ticket link missing", "PR link missing"]) {
      expect(d).toHaveTextContent(s);
    }
  });

  it("shows PRs, CI, blockers, node matches, history and links when present", async () => {
    const d = await open(mk({
      ci: "green", blockers_open: 0, facts_error: "facts unavailable",
      prs: [{ repo: "o/r", number: 5, state: "open", ci: "red" }, { repo: null, number: null }],
      nodes: [{ node: "dev", eligible: "yes" }, { node: "rev", eligible: "no", guards: { reason: "label missing" } }],
      history: [{ stage: "ready", at: "2026-01-01T00:00:00Z", actor: "bot" }, { stage: "dev", at: null }],
      links: { ticket: "https://x/1", prs: ["https://x/pull/5"] }, missing: ["pr"],
    }));
    expect(within(d).getAllByTestId("details-pr").map((e) => e.textContent)).toEqual(["o/r#5 · open · CI red", "repository missing#? · state missing · CI missing"]);
    expect(d).toHaveTextContent("green");
    expect(d).toHaveTextContent("0 open");
    expect(within(d).getAllByTestId("details-node").map((e) => e.textContent)).toEqual(["dev: eligible yes", "rev: eligible no (label missing)"]);
    expect(within(d).getAllByTestId("details-history").map((e) => e.textContent)).toEqual(["ready · 2026-01-01T00:00:00Z · bot", "dev · time missing"]);
    expect(within(d).getByText("Ticket")).toHaveAttribute("href", "https://x/1");
    expect(within(d).getByText("https://x/pull/5")).toHaveAttribute("href", "https://x/pull/5");
    expect(d).toHaveTextContent("facts unavailable");
  });
});
