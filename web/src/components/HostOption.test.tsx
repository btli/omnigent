import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { Host } from "@/hooks/useHosts";
import { HostOption } from "./HostOption";

afterEach(cleanup);

function host(status: Host["status"]): Host {
  return {
    host_id: `host-${status}`,
    name: status === "online" ? "Cloud workstation" : "Office desktop",
    owner: "owner@example.com",
    status,
  };
}

describe("HostOption", () => {
  it("renders an online host name, status, and online dot", () => {
    const { container } = render(<HostOption host={host("online")} />);

    expect(screen.getByText("Cloud workstation")).toBeInTheDocument();
    expect(screen.getByText("online")).toBeInTheDocument();
    expect(container.querySelector(".bg-green-500")).toBeInTheDocument();
  });

  it("renders an offline host with its subtitle and neutral dot", () => {
    const { container } = render(
      <HostOption host={host("offline")} subtitle="May be unavailable" />,
    );

    expect(screen.getByText("Office desktop")).toBeInTheDocument();
    expect(screen.getByText("offline")).toBeInTheDocument();
    expect(screen.getByText("May be unavailable")).toBeInTheDocument();
    expect(container.querySelector(".bg-muted-foreground")).toBeInTheDocument();
  });
});
