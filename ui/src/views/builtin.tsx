import { registerView, type ViewProps } from "./registry";

// Empty placeholders: the flow and columns content lands in later slices (#1392).
const Placeholder = ({ name, snapshot }: ViewProps & { name: string }) => (
  <div data-testid={`view-${name}`} className="p-4 text-sm text-muted-foreground">
    {name} view: {snapshot.tickets.length} tickets
  </div>
);
const Workflow = (props: ViewProps) => <Placeholder name="workflow" {...props} />;
const Columns = (props: ViewProps) => <Placeholder name="columns" {...props} />;

/** Register `workflow` (default) and `columns` through the public interface; nothing special-cases them. */
export function registerBuiltinViews(register: (spec: Parameters<typeof registerView>[0]) => unknown = registerView) {
  register({ id: "workflow", title: "Workflow", component: Workflow, description: "Stages, nodes and their tickets", default: true });
  register({ id: "columns", title: "Columns", component: Columns, description: "One column per stage" });
}
