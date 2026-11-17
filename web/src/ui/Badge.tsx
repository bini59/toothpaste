import type { Status } from "../lib";
import { cn } from "./cn";

export function Badge({ className, status = "draft", ...props }: React.ComponentProps<"span"> & { status?: Status }) {
  return <span className={cn("ui-badge", `ui-badge-${status}`, className)} {...props} />;
}
