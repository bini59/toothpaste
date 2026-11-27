import { Card, cn } from "../../ui";

/** one setting as a row: title and description on the left, the control on the right (stacked on narrow screens).
 * A <label> around its one control; `group` for rows that hold several controls. */
export function Row({ label, desc, children, group }: { label: string; desc?: React.ReactNode; children: React.ReactNode; group?: boolean }) {
  const Tag = group ? "div" : "label";
  const Box = group ? "div" : "span";
  return (
    <Tag role={group ? "group" : undefined} aria-label={group ? label : undefined}
      className="grid gap-x-6 gap-y-2 py-4 sm:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
      <Box className="block min-w-0 sm:pt-2">
        <span className="block text-sm font-semibold text-text-main">{label}</span>
        {desc && <span className="mt-1 block text-xs leading-relaxed text-text-muted">{desc}</span>}
      </Box>
      <Box className="block min-w-0">{children}</Box>
    </Tag>
  );
}
/** content of a section that is not a setting row (a list, a table, a note) */
export function Block({ children }: { children: React.ReactNode }) {
  return <div className="py-4">{children}</div>;
}
/** the settings key, small, for looking a value up with `tp config` */
export function Key({ k }: { k: string }) {
  return <code className="font-mono">{k}</code>;
}
export const boxCls = "h-4 w-4 accent-primary";
export function Check({ label, checked, onChange, disabled }: { label: React.ReactNode; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <label className={cn("inline-flex items-center gap-2 text-sm text-text-main", disabled ? "cursor-not-allowed opacity-60" : "cursor-pointer")}>
      <input type="checkbox" className={boxCls} checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  );
}
/** an on/off setting: the whole row is the checkbox's label */
export function CheckRow({ label, desc, checked, onChange }: { label: string; desc?: React.ReactNode; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <Row label={label} desc={desc}>
      <span className="flex h-9 items-center">
        <input type="checkbox" className={cn(boxCls, "cursor-pointer")} checked={checked} onChange={(e) => onChange(e.target.checked)} />
      </span>
    </Row>
  );
}
export function Section({ title, desc, aside, children }: { title: string; desc?: React.ReactNode; aside?: React.ReactNode; children: React.ReactNode }) {
  return (
    <Card className="p-4 sm:p-5">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h2 className="text-base font-bold text-text-main">{title}</h2>
          {desc && <p className="mt-1 text-xs leading-relaxed text-text-muted">{desc}</p>}
        </div>
        {aside}
      </div>
      <div className="mt-3 divide-y divide-border border-t border-border">{children}</div>
    </Card>
  );
}

export const inputCls = "h-9 text-sm";
export const numCls = "h-9 w-28 text-sm";
export const selectCls = "h-9 w-full sm:w-72";
export const areaCls = "w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-text-main outline-none hover:border-grey-300 focus:border-primary focus:ring-2 focus:ring-primary-200";
