import { describe, expect, it } from "vitest";
import stub from "../dev-stub/snapshot.json";

describe("dev stub", () => {
  it("has the snapshot fields the app reads", () => {
    for (const key of ["generated_at", "workflow", "tickets", "sources", "errors", "attention"]) expect(stub).toHaveProperty(key);
    expect(stub.workflow.stages.length).toBeGreaterThan(0);
  });
});
