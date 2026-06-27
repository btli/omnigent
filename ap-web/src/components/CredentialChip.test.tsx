// Tests for the composer credential chip. We assert on the trigger span's
// data attributes (kind / limit-status) and text — stable, presentation-
// agnostic signals — rather than reaching into the lucide glyph or the Radix
// HoverCard portal. Plus the store-connected wrapper's self-hiding behavior.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  CredentialChip,
  CredentialChipView,
  credentialDotClass,
  credentialProviderLabel,
} from "@/components/CredentialChip";
import type { ActiveCredential } from "@/lib/types";
import { useChatStore } from "@/store/chatStore";

const SUB: ActiveCredential = {
  id: "codex-pool/x1",
  name: "claude-pro-2",
  kind: "subscription",
  family: "anthropic",
  limitStatus: "available",
};

afterEach(() => {
  cleanup();
  useChatStore.setState({ sessionActiveCredential: null });
});

describe("CredentialChipView", () => {
  it("shows the bound account name", () => {
    render(<CredentialChipView credential={SUB} />);
    expect(screen.getByTestId("credential-chip")).toHaveTextContent("claude-pro-2");
  });

  it("distinguishes a subscription from an api_key", () => {
    const { rerender } = render(<CredentialChipView credential={SUB} />);
    expect(screen.getByTestId("credential-chip")).toHaveAttribute("data-kind", "subscription");

    rerender(<CredentialChipView credential={{ ...SUB, kind: "api_key", name: "openai-api" }} />);
    expect(screen.getByTestId("credential-chip")).toHaveAttribute("data-kind", "api_key");
  });

  it("reflects the usage-limit state for the status dot", () => {
    const { rerender } = render(<CredentialChipView credential={SUB} />);
    expect(screen.getByTestId("credential-chip")).toHaveAttribute("data-limit-status", "available");

    rerender(<CredentialChipView credential={{ ...SUB, limitStatus: "limited" }} />);
    expect(screen.getByTestId("credential-chip")).toHaveAttribute("data-limit-status", "limited");
  });
});

describe("credentialDotClass", () => {
  it("maps each limit status to its status token", () => {
    expect(credentialDotClass("available")).toBe("bg-success");
    expect(credentialDotClass("limited")).toBe("bg-warning");
    expect(credentialDotClass("unknown")).toContain("muted-foreground");
  });
});

describe("credentialProviderLabel", () => {
  it("joins provider family and credential kind", () => {
    expect(credentialProviderLabel(SUB)).toBe("Anthropic subscription");
    expect(credentialProviderLabel({ ...SUB, kind: "api_key", family: "openai" })).toBe(
      "OpenAI API key",
    );
  });
});

describe("CredentialChip (store-connected)", () => {
  it("renders nothing when no account is bound", () => {
    useChatStore.setState({ sessionActiveCredential: null });
    render(<CredentialChip />);
    expect(screen.queryByTestId("credential-chip")).toBeNull();
  });

  it("renders the chip when the session is bound to an account", () => {
    useChatStore.setState({ sessionActiveCredential: SUB });
    render(<CredentialChip />);
    expect(screen.getByTestId("credential-chip")).toHaveTextContent("claude-pro-2");
  });
});
