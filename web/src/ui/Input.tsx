import { cn } from "./cn";

export function Input({ className, type = "text", ...props }: React.ComponentProps<"input">) {
  return <input type={type} className={cn("ui-field ui-input", className)} {...props} />;
}
