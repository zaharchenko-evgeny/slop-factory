import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { mergeSnapshot, type Snapshot, type Ticket } from "./snapshot";
import { ViewRegistry } from "./views/registry";
import { registerBuiltinViews } from "./views/builtin";

const mk = (o: Partial<Ticket>): Ticket => ({ key: o.id ?? "k", id: "1", title: "T", repository: "o/r", stage: "dev", ...o });
const snap = (tickets: Ticket[], extra: Partial<Snapshot> = {}): Snapshot => ({
  generated_at: "t", workflow: { stages: [{ id: "dev" }] }, tickets, sources: [], errors: [],
  attention: { human_review: 0, human_attention: 0 }, ...extra,
});
const A = mk({ id: "1", title: "Alpha", tracker: "a", environments: ["env-x"] });
const B = mk({ id: "2", title: "Beta", tracker: "b", environments: ["env-y"] });

const deferred = () => {
  let resolve!: (s: Snapshot) => void; let reject!: (e: Error) => void;
  const promise = new Promise<Snapshot>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
};
const mount = (load: () => Promise<Snapshot>) => {
  const registry = new ViewRegistry();
  registerBuiltinViews((s) => registry.register(s));
  return render(<App registry={registry} load={load} />);
};
afterEach(() => vi.useRealTimers());

describe("ui_refresh_and_failures", () => {
  it("loads once and refreshes only on demand by default", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const load = vi.fn(async () => snap([A]));
    mount(load);
    await screen.findByText("1 tickets");
    expect(screen.getByLabelText("Auto-refresh")).not.toBeChecked();
    await act(async () => { await vi.advanceTimersByTimeAsync(600_000); });
    expect(load).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await screen.findByRole("button", { name: "Refresh" });
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("offers 15s, 30s, 1m and 5m, and starts at 60s when first enabled", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const load = vi.fn(async () => snap([A]));
    mount(load);
    await screen.findByText("1 tickets");
    const select = screen.getByLabelText("Refresh interval") as HTMLSelectElement;
    expect([...select.options].map((o) => o.text)).toEqual(["15s", "30s", "1m", "5m"]);
    expect(select.value).toBe("60000");
    fireEvent.click(screen.getByLabelText("Auto-refresh"));
    await act(async () => { await vi.advanceTimersByTimeAsync(59_000); });
    expect(load).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1_500); });
    expect(load).toHaveBeenCalledTimes(2);
    fireEvent.change(select, { target: { value: "15000" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(15_500); });
    expect(load).toHaveBeenCalledTimes(3);
    fireEvent.click(screen.getByLabelText("Auto-refresh")); // off
    await act(async () => { await vi.advanceTimersByTimeAsync(600_000); });
    expect(load).toHaveBeenCalledTimes(3);
  });

  it("never overlaps requests, and shows the last successful refresh time", async () => {
    const first = deferred(); const second = deferred();
    const load = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    mount(load);
    expect(screen.getByTestId("last-refresh")).toHaveTextContent("Not refreshed yet");
    expect(screen.getByRole("button", { name: "Refreshing…" })).toBeDisabled();
    await act(async () => { await userEvent.click(screen.getByRole("button", { name: "Refreshing…" })); });
    expect(load).toHaveBeenCalledTimes(1);
    await act(async () => { first.resolve(snap([A])); });
    await screen.findByText("1 tickets");
    expect(screen.getByTestId("last-refresh")).toHaveTextContent(/^Last refreshed /);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(load).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "Refreshing…" }));
    expect(load).toHaveBeenCalledTimes(2);
    await act(async () => { second.resolve(snap([A, B])); });
    await screen.findByText("2 tickets");
  });

  it("keeps stale data visible during a request and after an error, and keeps the last good time", async () => {
    const next = deferred();
    const load = vi.fn().mockResolvedValueOnce(snap([A])).mockReturnValueOnce(next.promise);
    mount(load);
    await screen.findByText("1 tickets");
    const time = screen.getByTestId("last-refresh").textContent;
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(screen.getByText("1 tickets")).toBeInTheDocument();
    await act(async () => { next.reject(new Error("boom")); });
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    expect(screen.getByText("1 tickets")).toBeInTheDocument();
    expect(screen.getByTestId("last-refresh").textContent).toBe(time);
  });

  it("shows source errors and applies the sources that succeeded", async () => {
    const load = vi.fn()
      .mockResolvedValueOnce(snap([A, B], { sources: [{ id: "a", ok: true }, { id: "b", ok: true }] }))
      .mockResolvedValueOnce(snap([mk({ id: "1", title: "Alpha v2", tracker: "a", environments: ["env-x"] })], {
        sources: [{ id: "a", ok: true }, { id: "b", ok: false }], errors: [{ source: "b", message: "b is down" }],
      }));
    mount(load);
    await screen.findByText("2 tickets");
    await userEvent.click(screen.getByRole("button", { name: "Columns" }));
    expect(screen.getByText("Beta")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByTestId("source-error")).toHaveTextContent("b: b is down");
    expect(screen.getByText("Alpha v2")).toBeInTheDocument(); // source a applied
    expect(screen.getByText("Beta")).toBeInTheDocument(); // source b stale
  });

  it("keeps the selection and the scroll position across a refresh", async () => {
    const load = vi.fn(async () => snap([A, B]));
    const { container } = mount(load);
    await screen.findByText("2 tickets");
    await userEvent.click(screen.getByRole("button", { name: "Columns" }));
    const card = screen.getAllByTestId("ticket-card").find((c) => c.textContent?.includes("Beta"))!;
    await userEvent.click(within(card).getByRole("button"));
    const main = container.querySelector("main")!;
    main.scrollTop = 120;
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await screen.findByRole("button", { name: "Refresh" });
    expect(load).toHaveBeenCalledTimes(2);
    expect(screen.getAllByRole("button", { pressed: true }).some((b) => b.textContent?.includes("Beta"))).toBe(true);
    expect(container.querySelector("main")).toBe(main);
    expect(main.scrollTop).toBe(120);
  });
});

describe("mergeSnapshot", () => {
  const cases: [string, Snapshot | undefined, Snapshot, string[]][] = [
    ["no previous", undefined, snap([A]), ["1"]],
    ["all ok", snap([A, B]), snap([A], { sources: [{ id: "a", ok: true }, { id: "b", ok: true }] }), ["1"]],
    ["source failed by sources flag", snap([A, B]), snap([A], { sources: [{ id: "b", ok: false }] }), ["1", "2"]],
    ["source failed by error only", snap([A, B]), snap([A], { errors: [{ source: "b", message: "x" }] }), ["1", "2"]],
    ["ticket error", snap([A, B]), snap([A], { errors: [{ source: "b", ticket: "2", message: "x" }] }), ["1", "2"]],
    ["ticket without tracker is dropped", snap([mk({ id: "9" })]), snap([], { errors: [{ source: "b", message: "x" }] }), []],
    ["environment name equal to failed source id does not keep a ticket", snap([mk({ id: "8", tracker: "a", environments: ["b"] })]), snap([], { sources: [{ id: "b", ok: false }] }), []],
    ["error without source", snap([A]), snap([], { errors: [{ source: null, message: "x" }] }), []],
  ];
  for (const [name, prev, next, keys] of cases)
    it(name, () => expect(mergeSnapshot(prev, next).tickets.map((t) => t.key)).toEqual(keys));
});
