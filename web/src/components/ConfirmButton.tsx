import { useEffect, useState } from "react";
import { Button, cn, type ButtonProps } from "../ui";

/**
 * Two-step in-app confirmation (no window.confirm: automation-attached browser tabs can auto-accept native dialogs).
 * First click shows the message with explicit confirm / cancel buttons; it resets after 15s.
 */
export function ConfirmButton({
  message,
  confirmLabel = "확인",
  onConfirm,
  className,
  children,
  ...props
}: ButtonProps & { message: React.ReactNode; confirmLabel?: string; onConfirm: () => void }) {
  const [asking, setAsking] = useState(false);
  useEffect(() => {
    if (!asking) return;
    const id = setTimeout(() => setAsking(false), 15000);
    return () => clearTimeout(id);
  }, [asking]);
  if (!asking)
    return (
      <Button {...props} className={className} onClick={() => setAsking(true)}>
        {children}
      </Button>
    );
  return (
    <div className={cn("rounded-lg border border-primary-200 bg-primary-50 p-2.5", className)}>
      <p className="text-xs leading-relaxed text-text-main">{message}</p>
      <div className="mt-2 flex gap-2">
        <Button
          size="sm"
          onClick={() => {
            setAsking(false);
            onConfirm();
          }}
        >
          {confirmLabel}
        </Button>
        <Button size="sm" variant="secondary" onClick={() => setAsking(false)}>
          취소
        </Button>
      </div>
    </div>
  );
}
