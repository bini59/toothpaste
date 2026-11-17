import { cn } from "./cn";

export function Card({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("ui-card", className)} {...props} />;
}
