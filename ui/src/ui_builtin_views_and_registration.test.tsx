import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { App } from "./App";
import type { Snapshot } from "./snapshot";
import { registerBuiltinViews } from "./views/builtin";
import { ViewRegistry } from "./views/registry";

const snapshot: Snapshot = {
  generated_at: "2026-11-14T12:00:00+00:00",
  workflow: { stages: [{ id: "ready" }] },
  tickets: [{ key: "github-issues|o/r#1", id: "1", title: "T", repository: "o/r", stage: "ready" }],
  sources: [{ id: "github-issues|o/r", ok: true }],
  errors: [],
  attention: { human_review: 0, human_attention: 0 },
};
const load = () => Promise.resolve(snapshot);
const builtins = () => { const r = new ViewRegistry(); registerBuiltinViews((s) => r.register(s)); return r; };

describe("ui_builtin_views_and_registration", () => {
  it("registers workflow (default) and columns through the public interface", () => {
    const r = builtins();
    expect(r.list().map((v) => v.id)).toEqual(["workflow", "columns"]);
    expect(r.defaultId()).toBe("workflow");
  });

  it.each([
    ["Bad Id", "lowercase slug"],
    ["workflow", "already registered"],
    ["", "lowercase slug"],
  ])("refuses the id %j", (id, message) => {
    expect(() => builtins().register({ id, title: "x", component: () => null })).toThrow(message);
  });

  it("refuses a second default", () => {
    expect(() => builtins().register({ id: "other", title: "x", component: () => null, default: true })).toThrow("a default view exists");
  });

  it("shows both built-in views and a third view registered without editing them", async () => {
    const r = builtins();
    r.register({ id: "extra", title: "Extra", component: () => <p>extra view</p> });
    render(<App registry={r} load={load} />);
    await waitFor(() => expect(screen.getByTestId("view-workflow")).toBeInTheDocument());
    for (const title of ["Workflow", "Columns", "Extra"]) expect(screen.getByRole("button", { name: title })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Columns" }));
    expect(screen.getByTestId("view-columns")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Extra" }));
    expect(screen.getByText("extra view")).toBeInTheDocument();
  });

  it("reports a failed snapshot load", async () => {
    render(<App registry={builtins()} load={() => Promise.reject(new Error("boom"))} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
  });
});
