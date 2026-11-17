import { Badge } from "../ui";
import { STAGES } from "../lib";

export function StageBadge({ stage }: { stage: string }) {
  const [label, status] = STAGES[stage] ?? [stage, "draft"];
  return <Badge status={status} className="whitespace-nowrap">{label}</Badge>;
}
