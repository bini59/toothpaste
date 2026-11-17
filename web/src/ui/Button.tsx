import { LoaderCircle } from "lucide-react";
import { cn } from "./cn";

/** loading: disabled + aria-busy, the label stays (invisible) so the width does not change, a spinner sits on top (TP-174) */
export type ButtonProps = React.ComponentProps<"button"> & { variant?: "primary" | "secondary"; size?: "sm" | "md"; loading?: boolean };

export function Button({ className, variant = "primary", size = "md", type = "button", loading = false, disabled, children, ...props }: ButtonProps) {
  return (
    <button type={type} className={cn("ui-button", `ui-button-${size}`, `ui-button-${variant}`, className)}
      disabled={disabled || loading} aria-busy={loading || undefined} {...props}>
      {/* display: contents keeps the children in the button's flex row; visibility is inherited by them */}
      <span className={cn("contents", loading && "invisible")}>{children}</span>
      {loading && (
        // visibility: hidden also hides the label from screen readers: an sr-only copy keeps the button's name
        <span className="absolute inset-0 flex items-center justify-center">
          <span className="sr-only">{children}</span>
          <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden />
        </span>
      )}
    </button>
  );
}
