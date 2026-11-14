import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { App } from "./App";
import { ViewRegistry } from "./views/registry";
import { registerBuiltinViews } from "./views/builtin";
import type { Human, Snapshot, Ticket } from "./snapshot";

const mk = (o: Partial<Ticket>): Ticket => ({ key: o.id ?? "k", id: "1", title: "T", repository: "o/r", stage: "dev", ...o });
const review: Human = { kind: "review", label: "Needs human review", reason: "review requested", reviewer: "alice" };
const attention: Human = { kind: "attention", label: "Needs human attention", reason: "question", reviewer: null };
const DAY = 86400;
const done = (id: string, days: number) => mk({ id, key: id, title: `done ${id}`, stage: "done", stage_kind: "final", stage_age: { state: "exempt", kind: "final", seconds: days * DAY, warn: false } });
const tickets = [
  mk({ id: "1", title: "Alpha", human: review, environments: ["engine"] }),
  mk({ id: "2", title: "Beta", human: attention, repository: "o/x", environments: ["team"], warnings: [{ kind: "x", level: "amber", message: "slow" }] }),
  mk({ id: "3", title: "Gamma", human: { kind: null } }),
  done("4", 2), done("5", 30),
];
const snap: Snapshot = {
  generated_at: "t", workflow: { stages: [{ id: "dev" }, { id: "done", kind: "final" }] }, tickets, sources: [], errors: [],
  attention: { human_review: 1, human_attention: 1 },
};

const setup = async (view = "columns") => {
  const registry = new ViewRegistry();
  registerBuiltinViews((s) => registry.register(s));
  render(<App registry={registry} load={async () => snap} />);
  await screen.findByTestId("attention-strip");
  if (view === "columns") await userEvent.click(screen.getByRole("button", { name: "Columns" }));
};
const titles = () => screen.queryAllByTestId("ticket-card").map((c) => c.textContent ?? "");

describe("ui_human_review_attention", () => {
  for (const view of ["columns", "workflow"]) {
    it(`shows review and attention as separate badges in the ${view} view`, async () => {
      await setup(view);
      expect(screen.getAllByTestId("badge-human-review")).toHaveLength(1);
      expect(screen.getAllByTestId("badge-human-attention")).toHaveLength(1);
      expect(screen.getByTestId("badge-human-review")).toHaveTextContent("Needs human review");
      expect(screen.getByTestId("badge-human-attention")).toHaveTextContent("Needs human attention");
      expect(screen.getByTestId("count-human-review")).toHaveTextContent("1 need human review");
      expect(screen.getByTestId("count-human-attention")).toHaveTextContent("1 need human attention");
    });
  }

  it("shows reason and reviewer in details, and unknown when missing", async () => {
    await setup();
    await userEvent.click(within(screen.getByText("Alpha").closest("li")!).getByRole("button"));
    expect(screen.getByTestId("details-human")).toHaveTextContent("reason: review requested · reviewer: alice");
    await userEvent.click(within(screen.getByText("Beta").closest("li")!).getByRole("button"));
    expect(screen.getByTestId("details-human")).toHaveTextContent("Needs human attention · reason: question · reviewer: unknown");
  });

  it("human-review filter intersects the other filters", async () => {
    await setup();
    await userEvent.click(screen.getByRole("button", { name: "Human review only" }));
    expect(titles().join()).toMatch(/Alpha/);
    expect(titles()).toHaveLength(1);
    await userEvent.selectOptions(screen.getByLabelText("Repository"), "o/x");
    expect(titles()).toHaveLength(0);
    await userEvent.selectOptions(screen.getByLabelText("Repository"), "o/r");
    expect(titles()).toHaveLength(1);
  });

  const cases: [string, () => Promise<void>, string[]][] = [
    ["search by title", () => userEvent.type(screen.getByLabelText("Search"), "bet"), ["Beta"]],
    ["search by repository", () => userEvent.type(screen.getByLabelText("Search"), "o/x"), ["Beta"]],
    ["search no match", () => userEvent.type(screen.getByLabelText("Search"), "zzz"), []],
    ["environment", () => userEvent.selectOptions(screen.getByLabelText("Environment"), "team"), ["Beta"]],
    ["stage", () => userEvent.selectOptions(screen.getByLabelText("Stage"), "dev"), ["Alpha", "Beta", "Gamma"]],
    ["warnings", () => userEvent.click(screen.getByLabelText("Warnings only")), ["Beta"]],
  ];
  for (const [name, act, want] of cases) {
    it(`filters: ${name}`, async () => {
      await setup();
      await act();
      const got = titles();
      expect(got).toHaveLength(want.length);
      want.forEach((w) => expect(got.join()).toContain(w));
    });
  }

  it("hides completed tickets older than seven days until load older", async () => {
    await setup();
    expect(titles().join()).toContain("done 4");
    expect(titles().join()).not.toContain("done 5");
    await userEvent.click(screen.getByRole("button", { name: /Load older \(1\)/ }));
    expect(titles().join()).toContain("done 5");
    expect(screen.queryByRole("button", { name: /Load older/ })).toBeNull();
  });

  it("hide completed removes every completed ticket", async () => {
    await setup();
    await userEvent.click(screen.getByLabelText("Hide completed"));
    expect(titles().join()).not.toContain("done");
    expect(titles()).toHaveLength(3);
  });

  it("clear filters restores the list", async () => {
    await setup();
    await userEvent.type(screen.getByLabelText("Search"), "zzz");
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(titles()).toHaveLength(4);
  });
});
