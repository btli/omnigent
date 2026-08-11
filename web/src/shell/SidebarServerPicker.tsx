import { useEffect, useState } from "react";
import { CheckIcon, ChevronDownIcon, ChevronUpIcon, PlusIcon, ServerIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  getServerPicker,
  openServerSetup,
  switchServer,
  type ServerPickerInfo,
} from "@/lib/nativeBridge";
import { cn } from "@/lib/utils";
import { SIDEBAR_ROW } from "./sidebarStyles";

/** Short display label for a server URL — its host, e.g. "localhost:8000". */
function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

/** Origin of a server URL, for matching recents against the current origin. */
function originOf(url: string): string | null {
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

/** Server picker that self-hides when the active shell has no picker IPC. */
export function SidebarServerPicker({ variant = "sidebar" }: { variant?: "sidebar" | "header" }) {
  const [info, setInfo] = useState<ServerPickerInfo | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getServerPicker().then((result) => {
      if (!cancelled) setInfo(result);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!info) return null;

  // The current server leads the list even when the recents file was edited
  // out from under us; recents matching the current origin collapse into it.
  const others = info.recentServers.filter((url) => originOf(url) !== info.currentOrigin);
  const currentHost = hostOf(info.currentOrigin);
  const inSidebar = variant === "sidebar";

  return (
    <div
      className={cn(inSidebar && "shrink-0 px-2 pt-1 pb-2")}
      data-testid="sidebar-server-picker-row"
    >
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            className={cn(
              inSidebar && SIDEBAR_ROW,
              "w-full justify-start border-0 font-normal",
              "text-muted-foreground",
              "hover:bg-muted hover:text-foreground dark:hover:bg-muted/50",
              "data-[state=open]:bg-muted data-[state=open]:text-foreground",
            )}
            aria-label={`Server: ${currentHost}. Switch server`}
            data-testid="sidebar-server-picker"
          >
            <ServerIcon className="ui-icon text-muted-foreground" />
            <span className="truncate">{currentHost}</span>
            {inSidebar ? (
              <ChevronUpIcon className="ui-icon ml-auto shrink-0 text-muted-foreground" />
            ) : (
              <ChevronDownIcon className="ui-icon ml-auto shrink-0 text-muted-foreground" />
            )}
          </Button>
        </DropdownMenuTrigger>
        {/* The sidebar dock opens upward; the header control opens downward. */}
        <DropdownMenuContent side={inSidebar ? "top" : "bottom"} align="start" className="min-w-56">
          <DropdownMenuLabel className="text-muted-foreground">Recents</DropdownMenuLabel>
          <DropdownMenuItem disabled className="gap-2 opacity-100">
            <CheckIcon className="size-4 shrink-0" />
            <span className="truncate font-medium">{currentHost}</span>
          </DropdownMenuItem>
          {others.map((url) => (
            <DropdownMenuItem key={url} className="gap-2" onSelect={() => void switchServer(url)}>
              {/* Spacer aligns hosts under the current-server check. */}
              <span className="size-4 shrink-0" aria-hidden="true" />
              <span className="truncate">{hostOf(url)}</span>
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <DropdownMenuItem className="gap-2" onSelect={() => openServerSetup()}>
            <PlusIcon className="size-4 shrink-0" />
            Connect to new server…
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
