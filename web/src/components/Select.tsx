import { forwardRef } from "react";
import { cn } from "../ui";

/** Native <select> with the Input look (ui.css). */
export const Select = forwardRef<HTMLSelectElement, Omit<React.SelectHTMLAttributes<HTMLSelectElement>, "size"> & { size?: "sm" | "md" }>(
  ({ className, size = "md", ...props }, ref) => (
    <select
      ref={ref}
      className={cn("ui-field ui-select", `ui-select-${size}`, className)}
      {...props}
    />
  ),
);
Select.displayName = "Select";
