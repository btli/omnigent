import { MonitorCloudIcon, MonitorIcon } from "lucide-react";

import type { Host } from "@/hooks/useHosts";

export function HostOption({ host, subtitle }: { host: Host; subtitle?: string }) {
  const isOnline = host.status === "online";
  return (
    <span className="flex min-w-0 items-center gap-2">
      {host.name.toLowerCase().includes("cloud") ? (
        <MonitorCloudIcon className="size-4 shrink-0 text-muted-foreground" />
      ) : (
        <MonitorIcon className="size-4 shrink-0 text-muted-foreground" />
      )}
      <span className="flex min-w-0 flex-col">
        <span className="flex items-center gap-2">
          <span className="truncate text-xs">{host.name}</span>
          <span
            className={`inline-flex shrink-0 items-center gap-1 text-[10px] font-semibold uppercase tracking-wider ${isOnline ? "text-green-600" : "text-muted-foreground"}`}
          >
            <span
              className={`inline-block size-1.5 rounded-full ${isOnline ? "bg-green-500" : "bg-muted-foreground"}`}
            />
            {host.status}
          </span>
        </span>
        {subtitle && (
          <span className="text-[10px] leading-tight text-muted-foreground">{subtitle}</span>
        )}
      </span>
    </span>
  );
}
