import { ChevronRight, type LucideIcon } from "lucide-react";
import { Card } from "../ui";

type MonitoringItem = { key: string; label: string; icon: LucideIcon };

export function Monitoring({ title, items }: { title: string; items: MonitoringItem[] }) {
  return (
    <div className="max-w-5xl">
      <h1 id="monitoring-title" className="mb-4 text-xl font-bold text-text-main sm:text-2xl">{title}</h1>
      <nav aria-labelledby="monitoring-title">
        <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {items.map(({ key, label, icon: Icon }) => (
            <li key={key}>
              <Card>
                <a href={`#${key}`}
                  className="flex items-center gap-3 rounded-xl p-4 text-sm font-semibold text-text-main transition-colors hover:bg-grey-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary">
                  <Icon className="h-5 w-5 shrink-0 text-primary" aria-hidden />
                  <span className="flex-1">{label}</span>
                  <ChevronRight className="h-4 w-4 shrink-0 text-text-muted" aria-hidden />
                </a>
              </Card>
            </li>
          ))}
        </ul>
      </nav>
    </div>
  );
}
