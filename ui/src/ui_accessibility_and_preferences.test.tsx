import { render, screen, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { NO_FILTERS } from "./filters";
import { DEFAULT_PREFS, loadPrefs, savePrefs } from "./prefs";
import type { Snapshot, Ticket } from "./snapshot";
import { ViewRegistry } from "./views/registry";
import { registerBuiltinViews } from "./views/builtin";
import { TicketCard } from "./views/TicketCard";

const mk = (o: Partial<Ticket>): Ticket => ({ key: o.id ?? "k", id: "1", title: "T", repository: "o/r", stage: "dev", ...o });
const snap = (tickets: Ticket[]): Snapshot => ({
  generated_at: "t", workflow: { stages: [{ id: "dev" }] }, tickets, sources: [], errors: [], attention: { human_review: 0, human_attention: 0 },
});
const mount = (key: string, tickets = [mk({ id: "1", title: "Alpha" }), mk({ id: "2", title: "Beta" })]) => {
  const registry = new ViewRegistry();
  registerBuiltinViews((s) => registry.register(s));
  return render(<App registry={registry} load={async () => snap(tickets)} storageKey={key} />);
};

// Node's own experimental `localStorage` can shadow jsdom's, so the test installs a plain in-memory one.
const memory = () => { const m = new Map<string, string>(); return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v) }; };
beforeEach(() => { vi.stubGlobal("localStorage", memory()); document.documentElement.removeAttribute("data-theme"); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("ui_accessibility_and_preferences", () => {
  it("restores view, filters, theme and refresh settings per factory", async () => {
    const user = userEvent.setup();
    const a = mount("factory-a");
    await screen.findByText("2 tickets");
    await user.click(screen.getByRole("button", { name: "Columns" }));
    await user.type(screen.getByLabelText("Search"), "alpha");
    await user.selectOptions(screen.getByLabelText("Theme"), "dark");
    await user.click(screen.getByLabelText("Auto-refresh"));
    await user.selectOptions(screen.getByLabelText("Refresh interval"), "15000");
    expect(document.documentElement.dataset.theme).toBe("dark");
    a.unmount();
    document.documentElement.removeAttribute("data-theme");

    mount("factory-a");
    await screen.findByText("2 tickets");
    expect(screen.getAllByTestId("ticket-card")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Columns" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("Search")).toHaveValue("alpha");
    expect(screen.getByLabelText("Theme")).toHaveValue("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(screen.getByLabelText("Auto-refresh")).toBeChecked();
    expect(screen.getByLabelText("Refresh interval")).toHaveValue("15000");
  });

  it("keeps each factory's preferences apart", async () => {
    const user = userEvent.setup();
    const a = mount("factory-a");
    await screen.findByText("2 tickets");
    await user.selectOptions(screen.getByLabelText("Theme"), "light");
    a.unmount();
    mount("factory-b");
    await screen.findByText("2 tickets");
    expect(screen.getByLabelText("Theme")).toHaveValue("system");
  });

  it.each<[string, string | null]>([
    ["missing", null], ["not json", "{"], ["null", "null"], ["array", "[]"], ["number", "7"],
    ["wrong types", JSON.stringify({ view: 3, filters: { search: 1, warnings: "x" }, theme: "neon", auto: "yes", interval: 7 })],
  ])("falls back to defaults for %s saved data", (_n, raw) => {
    const storage = { getItem: () => raw };
    expect(loadPrefs("k", storage)).toEqual({ ...DEFAULT_PREFS, view: undefined });
  });

  it("keeps valid fields next to invalid ones, and survives a broken store", () => {
    const raw = JSON.stringify({ view: "columns", filters: { search: "x", stage: 5 }, theme: "light", interval: 30000 });
    expect(loadPrefs("k", { getItem: () => raw })).toEqual({ view: "columns", filters: { ...NO_FILTERS, search: "x" }, theme: "light", auto: false, interval: 30000 });
    const broken = { getItem: () => { throw new Error("denied"); }, setItem: () => { throw new Error("full"); } };
    expect(loadPrefs("k", broken)).toEqual({ ...DEFAULT_PREFS, view: undefined });
    expect(() => savePrefs("k", DEFAULT_PREFS, broken)).not.toThrow();
  });

  it("ignores a saved view that is not registered", async () => {
    localStorage.setItem("factory-c", JSON.stringify({ view: "gone" }));
    mount("factory-c");
    await screen.findByText("2 tickets");
    expect(screen.getByRole("button", { name: "Workflow" })).toHaveAttribute("aria-pressed", "true");
  });

  it("works from the keyboard: skip link, tab order and Enter on controls", async () => {
    const user = userEvent.setup();
    mount("factory-k");
    await screen.findByText("2 tickets");
    await user.tab();
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Workflow" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Columns" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: "Columns" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("status")).toHaveTextContent(/refreshed/i);
  });

  it("animates a card only when it changes after it is shown", async () => {
    vi.useFakeTimers();
    const props = { showTracker: false, selected: false, onSelect: () => {} };
    const { rerender } = render(<TicketCard ticket={mk({ id: "1" })} {...props} />);
    expect(screen.getByTestId("ticket-card")).toHaveAttribute("data-changed", "false");
    rerender(<TicketCard ticket={mk({ id: "1" })} {...props} />);
    expect(screen.getByTestId("ticket-card")).toHaveAttribute("data-changed", "false");
    rerender(<TicketCard ticket={mk({ id: "1", stage: "review" })} {...props} />);
    expect(screen.getByTestId("ticket-card")).toHaveAttribute("data-changed", "true");
    await act(async () => { await vi.advanceTimersByTimeAsync(900); });
    expect(screen.getByTestId("ticket-card")).toHaveAttribute("data-changed", "false");
  });

  it.each([
    ["title", { title: "New title" }],
    ["repository", { repository: "other/repo" }],
    ["short_id", { short_id: 99 }],
    ["tracker", { tracker: "other-tracker" }],
    ["stage", { stage: "review" }],
    ["warnings", { warnings: [{ kind: "x", level: "warn", message: "new warning" }] }],
    ["human", { human: { kind: "review" as const, label: "Review" } }],
  ])("flashes when the displayed %s changes", (_name, patch) => {
    const props = { showTracker: true, selected: false, onSelect: () => {} };
    const { rerender } = render(<TicketCard ticket={mk({ id: "1" })} {...props} />);
    rerender(<TicketCard ticket={mk({ id: "1", ...patch })} {...props} />);
    expect(screen.getByTestId("ticket-card")).toHaveAttribute("data-changed", "true");
  });
});
