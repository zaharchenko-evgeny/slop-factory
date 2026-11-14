import { Badge } from "@/components/ui/badge";
import type { Human } from "@/snapshot";

/** `Needs human review` and the generic `Needs human attention` are two separate badges; neither implies the other. */
export function HumanBadge({ human }: { human?: Human }) {
  if (human?.kind === "review") return <Badge data-testid="badge-human-review" className="border-blue-500 text-blue-700 dark:text-blue-300">Needs human review</Badge>;
  if (human?.kind === "attention") return <Badge data-testid="badge-human-attention">Needs human attention</Badge>;
  return null;
}
