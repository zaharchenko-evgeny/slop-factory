import type { ComponentType } from "react";
import type { Snapshot } from "@/snapshot";

/**
 * View registration. A view is a React component that reads only the snapshot it is given (`props.snapshot`):
 * it never fetches, reads a tracker or writes. Mirrors `factory/ui/views.py` (`ViewSpec`).
 *
 *   import { registerView } from "@/views/registry";
 *   registerView({ id: "by-repo", title: "By repository", component: ByRepo });
 *
 * `id` is a unique lowercase slug. `default: true` marks the view shown first (one only). The app lists views from the
 * registry, so a new view needs no edit of the built-in views, the app or the tracker adapters.
 */
export interface ViewProps { snapshot: Snapshot }
export interface ViewSpec {
  id: string;
  title: string;
  component: ComponentType<ViewProps>;
  description?: string;
  default?: boolean;
}

const SLUG = /^[a-z][a-z0-9-]*$/;

export class ViewRegistry {
  private views = new Map<string, ViewSpec>();

  /** Add `spec`; throws for a bad or taken id, or a second default. */
  register(spec: ViewSpec): ViewSpec {
    if (!SLUG.test(spec.id)) throw new Error(`view id ${JSON.stringify(spec.id)}: use a lowercase slug`);
    if (this.views.has(spec.id)) throw new Error(`view ${JSON.stringify(spec.id)} is already registered`);
    if (spec.default && this.list().some((v) => v.default))
      throw new Error(`view ${JSON.stringify(spec.id)}: a default view exists`);
    this.views.set(spec.id, spec);
    return spec;
  }

  list(): ViewSpec[] { return [...this.views.values()]; }
  get(id: string): ViewSpec | undefined { return this.views.get(id); }
  defaultId(): string | undefined { return (this.list().find((v) => v.default) ?? this.list()[0])?.id; }
}

export const registry = new ViewRegistry();
export const registerView = (spec: ViewSpec) => registry.register(spec);
