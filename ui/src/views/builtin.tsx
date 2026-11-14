import { Columns } from "./Columns";
import { WorkflowFlow } from "./WorkflowFlow";
import { registerView } from "./registry";

/** Register `workflow` (default) and `columns` through the public interface; nothing special-cases them. */
export function registerBuiltinViews(register: (spec: Parameters<typeof registerView>[0]) => unknown = registerView) {
  register({ id: "workflow", title: "Workflow", component: WorkflowFlow, description: "Stages, nodes and their tickets", default: true });
  register({ id: "columns", title: "Columns", component: Columns, description: "One column per stage" });
}
