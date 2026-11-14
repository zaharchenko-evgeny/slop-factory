import { type Filters, NO_FILTERS } from "@/filters";

export type Theme = "system" | "light" | "dark";
export const INTERVALS = [15_000, 30_000, 60_000, 300_000];
export interface Prefs { view?: string; filters: Filters; theme: Theme; auto: boolean; interval: number }
export const DEFAULT_PREFS: Prefs = { filters: NO_FILTERS, theme: "system", auto: false, interval: 60_000 };

/** One key per factory: each factory's UI is served from its own origin and path. */
export const defaultStorageKey = () => `factory-ui:prefs:${location.origin}${location.pathname}`;

const str = (v: unknown) => (typeof v === "string" ? v : undefined);

/** Read saved preferences. Missing, corrupt or wrong-typed data falls back to the default field by field. */
export function loadPrefs(key: string, storage: Pick<Storage, "getItem"> | undefined = safeStorage()): Prefs {
  let raw: unknown;
  try { raw = JSON.parse(storage?.getItem(key) ?? "null"); } catch { raw = null; }
  const o = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const f = (o.filters && typeof o.filters === "object" ? o.filters : {}) as Record<string, unknown>;
  const filters = { ...NO_FILTERS };
  for (const k of Object.keys(NO_FILTERS) as (keyof Filters)[]) {
    if (typeof f[k] === typeof NO_FILTERS[k]) (filters as Record<string, unknown>)[k] = f[k];
  }
  return {
    view: str(o.view),
    filters,
    theme: o.theme === "light" || o.theme === "dark" ? o.theme : "system",
    auto: o.auto === true,
    interval: INTERVALS.includes(o.interval as number) ? (o.interval as number) : DEFAULT_PREFS.interval,
  };
}

/** Save preferences. A full or blocked store must not break the app, so a failure is ignored. */
export function savePrefs(key: string, prefs: Prefs, storage: Pick<Storage, "setItem"> | undefined = safeStorage()): void {
  try { storage?.setItem(key, JSON.stringify(prefs)); } catch { /* storage unavailable: preferences last for this page only */ }
}

function safeStorage(): Storage | undefined {
  try { return globalThis.localStorage; } catch { return undefined; }
}

/** `system` removes the attribute so the `prefers-color-scheme` media query decides. */
export function applyTheme(theme: Theme, root: HTMLElement = document.documentElement): void {
  if (theme === "system") root.removeAttribute("data-theme"); else root.setAttribute("data-theme", theme);
}
