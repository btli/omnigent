import { HostOption } from "@/components/HostOption";
import type { Host } from "@/hooks/useHosts";

import { ProjectDefaultPicker, withCurrentOption } from "./ProjectDefaultPicker";
import type { FieldProvenance } from "./projectDefaultsDraft";

function hostRow(host: Host) {
  return (
    <HostOption
      host={host}
      subtitle={
        host.status === "offline"
          ? "Future sessions may fail until this host reconnects"
          : undefined
      }
    />
  );
}

export function DefaultHostPicker({
  value,
  provenance,
  hosts,
  isLoading,
  error,
  onChange,
  onReset,
}: {
  value: string;
  provenance: FieldProvenance;
  hosts: readonly Host[];
  isLoading: boolean;
  error?: string;
  onChange: (value: string) => void;
  onReset: () => void;
}) {
  const { options, selected, unknown } = withCurrentOption(
    hosts.map((host) => ({
      value: host.host_id,
      label: `${host.name} (${host.status})`,
      content: hostRow(host),
    })),
    value,
    `${value} (unavailable)`,
  );
  const selectedHost = hosts.find((host) => host.host_id === value);

  const empty = hosts.length === 0;
  const triggerLabel = isLoading
    ? "Loading hosts…"
    : empty && !value
      ? "No connected hosts"
      : value
        ? (selected?.label ?? value)
        : "No pinned host";
  const hint = unknown
    ? "This saved host is unavailable. Reset it or choose a connected host."
    : selectedHost?.status === "offline"
      ? "This host is offline. Future sessions may fail until it reconnects."
      : empty
        ? "No connected hosts"
        : "No pinned host";

  return (
    <ProjectDefaultPicker
      field="host_id"
      label="Host"
      value={value}
      provenance={provenance}
      options={options}
      triggerLabel={triggerLabel}
      triggerContent={selectedHost ? hostRow(selectedHost) : undefined}
      disabled={isLoading || empty}
      hint={hint}
      error={error}
      onChange={onChange}
      onReset={onReset}
    />
  );
}
