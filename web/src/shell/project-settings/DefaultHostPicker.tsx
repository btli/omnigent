import { HostOption } from "@/components/HostOption";
import type { Host } from "@/hooks/useHosts";

import {
  ProjectDefaultPicker,
  type ProjectDefaultPickerOption,
} from "./DefaultHarnessPicker";
import type { FieldProvenance } from "./projectDefaultsDraft";

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
  const selectedHost = hosts.find((host) => host.host_id === value);
  const unavailable = value !== "" && !selectedHost;
  const options: ProjectDefaultPickerOption[] = hosts.map((host) => ({
    value: host.host_id,
    label: `${host.name} (${host.status})`,
    content: (
      <HostOption
        host={host}
        subtitle={
          host.status === "offline"
            ? "Future sessions may fail until this host reconnects"
            : undefined
        }
      />
    ),
  }));

  if (unavailable) {
    options.unshift({
      value,
      label: `${value} (unavailable)`,
    });
  }

  const empty = hosts.length === 0;
  const triggerLabel = isLoading
    ? "Loading hosts…"
    : empty && !value
      ? "No connected hosts"
      : unavailable
        ? `${value} (unavailable)`
        : selectedHost
          ? `${selectedHost.name} (${selectedHost.status})`
          : "No pinned host";
  const hint = unavailable
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
      triggerContent={
        selectedHost ? (
          <HostOption
            host={selectedHost}
            subtitle={
              selectedHost.status === "offline"
                ? "Future sessions may fail until this host reconnects"
                : undefined
            }
          />
        ) : undefined
      }
      disabled={isLoading || empty}
      hint={hint}
      error={error}
      onChange={onChange}
      onReset={onReset}
    />
  );
}
