import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Host } from "@/hooks/useHosts";
import { DefaultHostPicker } from "./DefaultHostPicker";

afterEach(cleanup);

const HOSTS: Host[] = [
  { host_id: "host-online", name: "Cloud runner", owner: "owner", status: "online" },
  { host_id: "host-offline", name: "Office Mac", owner: "owner", status: "offline" },
];

describe("DefaultHostPicker", () => {
  it("renders hosts, warns for offline hosts, and changes selection", () => {
    const onChange = vi.fn();
    render(
      <DefaultHostPicker
        value="host-online"
        provenance="inherited"
        hosts={HOSTS}
        isLoading={false}
        onChange={onChange}
        onReset={vi.fn()}
      />,
    );

    fireEvent.pointerDown(screen.getByTestId("project-default-host_id-control"), {
      button: 0,
    });
    expect(screen.getByTestId("project-default-host_id-option-host-online")).toBeInTheDocument();
    expect(screen.getByTestId("project-default-host_id-option-host-offline")).toHaveTextContent(
      "Future sessions may fail until this host reconnects",
    );
    fireEvent.click(screen.getByTestId("project-default-host_id-option-host-offline"));
    expect(onChange).toHaveBeenCalledWith("host-offline");
  });

  it("preserves an unknown stored host as unavailable and allows Reset", () => {
    const onReset = vi.fn();
    render(
      <DefaultHostPicker
        value="host-legacy"
        provenance="overridden"
        hosts={HOSTS}
        isLoading={false}
        onChange={vi.fn()}
        onReset={onReset}
      />,
    );

    expect(screen.getByTestId("project-default-host_id-control")).toHaveTextContent(
      "host-legacy (unavailable)",
    );
    fireEvent.pointerDown(screen.getByTestId("project-default-host_id-control"), {
      button: 0,
    });
    expect(screen.getByTestId("project-default-host_id-option-host-legacy")).toHaveTextContent(
      "unavailable",
    );
    fireEvent.click(screen.getByTestId("project-default-host_id-reset"));
    expect(onReset).toHaveBeenCalledTimes(1);
  });

  it("disables the picker when no hosts are connected", () => {
    render(
      <DefaultHostPicker
        value=""
        provenance="inherited"
        hosts={[]}
        isLoading={false}
        onChange={vi.fn()}
        onReset={vi.fn()}
      />,
    );

    expect(screen.getByTestId("project-default-host_id-control")).toBeDisabled();
    expect(screen.getByTestId("project-default-host_id-control")).toHaveTextContent(
      "No connected hosts",
    );
  });
});
