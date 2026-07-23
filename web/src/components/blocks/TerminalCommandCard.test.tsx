// DOM smoke tests for the terminal-command card rendered for !cmd inputs.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TerminalCommandCard } from "./TerminalCommandCard";

afterEach(cleanup);

describe("TerminalCommandCard", () => {
  it("kind='input' renders the command text with a $ prefix", () => {
    render(<TerminalCommandCard kind="input" input="pwd" stdout={null} stderr={null} />);
    const card = screen.getByTestId("terminal-command-card");
    expect(card.getAttribute("data-terminal-kind")).toBe("input");
    expect(screen.getByText("pwd")).toBeDefined();
    expect(screen.getByText("$")).toBeDefined();
  });

  it("kind='input' with no text renders without crashing", () => {
    render(<TerminalCommandCard kind="input" input={null} stdout={null} stderr={null} />);
    const card = screen.getByTestId("terminal-command-card");
    expect(card.getAttribute("data-terminal-kind")).toBe("input");
  });

  it("kind='output' with no stdout/stderr renders '(no output)' and no collapsible", () => {
    const { container } = render(
      <TerminalCommandCard kind="output" input={null} stdout={null} stderr={null} />,
    );
    expect(screen.getByTestId("terminal-command-card").getAttribute("data-terminal-kind")).toBe(
      "output",
    );
    expect(screen.getByText("(no output)")).toBeDefined();
    expect(container.querySelector('[data-slot="collapsible-trigger"]')).toBeNull();
  });

  it("kind='output' with stdout renders collapsible; click reveals stdout panel", () => {
    const { container } = render(
      <TerminalCommandCard kind="output" input={null} stdout="/home/user" stderr={null} />,
    );
    const trigger = container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]');
    expect(trigger).not.toBeNull();
    expect(trigger!.getAttribute("data-state")).toBe("closed");

    fireEvent.click(trigger!);

    expect(trigger!.getAttribute("data-state")).toBe("open");
    expect(screen.getAllByText("stdout").length).toBeGreaterThan(0);
    expect(
      screen.getAllByText((_, node) => Boolean(node?.textContent?.includes("/home/user"))).length,
    ).toBeGreaterThan(0);
  });

  // Recorded against the pre-receipt-fields card: legacy items (no `action`)
  // must keep rendering byte-identically as the card grows receipt props.
  it("legacy input markup is byte-identical (snapshot)", () => {
    const { container } = render(
      <TerminalCommandCard kind="input" input="git status" stdout={null} stderr={null} />,
    );
    expect(container.innerHTML).toMatchSnapshot();
  });

  it("legacy empty input markup is byte-identical (snapshot)", () => {
    const { container } = render(
      <TerminalCommandCard kind="input" input={null} stdout={null} stderr={null} />,
    );
    expect(container.innerHTML).toMatchSnapshot();
  });

  it("legacy output markup is byte-identical (snapshot)", () => {
    const { container } = render(
      <TerminalCommandCard kind="output" input={null} stdout="ok" stderr="warn" />,
    );
    expect(container.innerHTML).toMatchSnapshot();
  });

  it("legacy no-output markup is byte-identical (snapshot)", () => {
    const { container } = render(
      <TerminalCommandCard kind="output" input={null} stdout={null} stderr={null} />,
    );
    expect(container.innerHTML).toMatchSnapshot();
  });

  // `action` is the receipt discriminator: a no-`action` item that somehow
  // carries a flattened "ok"/"error" status (and even an error string) must
  // render byte-for-byte as plain legacy — no attributes, styling, or error
  // line may leak through.
  it("no-action item with status='error' renders byte-identically to plain legacy", () => {
    const legacy = render(
      <TerminalCommandCard kind="input" input="git status" stdout={null} stderr={null} />,
    );
    const withStatus = render(
      <TerminalCommandCard
        kind="input"
        input="git status"
        stdout={null}
        stderr={null}
        status="error"
        error="boom"
      />,
    );
    expect(withStatus.container.innerHTML).toBe(legacy.container.innerHTML);
    expect(withStatus.container.innerHTML).toMatchSnapshot();
  });

  it("no-action item with status='ok' renders byte-identically to plain legacy", () => {
    const legacy = render(
      <TerminalCommandCard kind="input" input="git status" stdout={null} stderr={null} />,
    );
    const withStatus = render(
      <TerminalCommandCard
        kind="input"
        input="git status"
        stdout={null}
        stderr={null}
        status="ok"
      />,
    );
    expect(withStatus.container.innerHTML).toBe(legacy.container.innerHTML);
    expect(withStatus.container.innerHTML).toMatchSnapshot();
  });

  it("no-action item with status='unknown' renders byte-identically to plain legacy", () => {
    const legacy = render(
      <TerminalCommandCard kind="input" input="git status" stdout={null} stderr={null} />,
    );
    const withStatus = render(
      <TerminalCommandCard
        kind="input"
        input="git status"
        stdout={null}
        stderr={null}
        status="unknown"
      />,
    );
    expect(withStatus.container.innerHTML).toBe(legacy.container.innerHTML);
    expect(withStatus.container.innerHTML).toMatchSnapshot();
  });

  it("kind='output' with stderr renders stderr panel", () => {
    const { container } = render(
      <TerminalCommandCard kind="output" input={null} stdout={null} stderr="command not found" />,
    );
    const trigger = container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]');
    expect(trigger).not.toBeNull();
    fireEvent.click(trigger!);
    expect(screen.getAllByText("stderr").length).toBeGreaterThan(0);
    expect(
      screen.getAllByText((_, node) => Boolean(node?.textContent?.includes("command not found")))
        .length,
    ).toBeGreaterThan(0);
  });
});

describe("TerminalCommandCard — bang receipt variants", () => {
  const receipt = {
    kind: "input" as const,
    input: "echo hi",
    stdout: null,
    stderr: null,
    terminalId: "terminal_zsh_u-ab12cd",
    terminalName: "zsh",
    sessionKey: "u-ab12cd",
  };

  it("spawn receipt renders the `→ new <name> · <key>` chip (snapshot)", () => {
    const { container } = render(<TerminalCommandCard {...receipt} action="spawn" status="ok" />);
    expect(screen.getByTestId("terminal-command-chip").textContent).toBe("→ new zsh · u-ab12cd");
    expect(screen.getByTestId("terminal-command-card").getAttribute("data-terminal-action")).toBe(
      "spawn",
    );
    expect(container.innerHTML).toMatchSnapshot();
  });

  it("send receipt renders the `→ <name> · <key>` chip (snapshot)", () => {
    const { container } = render(<TerminalCommandCard {...receipt} action="send" status="ok" />);
    expect(screen.getByTestId("terminal-command-chip").textContent).toBe("→ zsh · u-ab12cd");
    expect(container.innerHTML).toMatchSnapshot();
  });

  it("error receipt styles destructively and shows the error line (snapshot)", () => {
    const { container } = render(
      <TerminalCommandCard
        {...receipt}
        action="send"
        status="error"
        error="shell u-ab12cd is not running"
      />,
    );
    const card = screen.getByTestId("terminal-command-card");
    expect(card.getAttribute("data-terminal-status")).toBe("error");
    expect(screen.getByTestId("terminal-command-error").textContent).toBe(
      "shell u-ab12cd is not running",
    );
    expect(container.querySelector(".text-destructive")).not.toBeNull();
    // Icon AND border take the destructive palette on error receipts.
    expect(card.className).toContain("border-destructive/50");
    expect(container.innerHTML).toMatchSnapshot();
  });

  it("ok receipt renders no error line and keeps the emerald icon", () => {
    const { container } = render(<TerminalCommandCard {...receipt} action="send" status="ok" />);
    expect(screen.queryByTestId("terminal-command-error")).toBeNull();
    expect(container.querySelector(".text-emerald-500")).not.toBeNull();
    expect(container.querySelector(".text-destructive")).toBeNull();
  });

  it("unknown receipt uses a muted informational treatment, not the destructive palette", () => {
    const { container } = render(
      <TerminalCommandCard {...receipt} action="send" status="unknown" />,
    );
    const card = screen.getByTestId("terminal-command-card");
    expect(card.getAttribute("data-terminal-status")).toBe("unknown");
    expect(screen.getByTestId("terminal-command-unknown").textContent).toBe("Delivery unknown");
    expect(container.querySelector(".text-destructive")).toBeNull();
    expect(card.className).not.toContain("border-destructive");
    expect(container.innerHTML).toMatchSnapshot();
  });

  it("chip is a non-interactive span until a focus handler is wired", () => {
    render(<TerminalCommandCard {...receipt} action="send" />);
    expect(screen.getByTestId("terminal-command-chip").tagName).toBe("SPAN");
  });

  it("chip becomes a button and click focuses the shell when handler + id present", () => {
    const onFocusShell = vi.fn();
    render(<TerminalCommandCard {...receipt} action="send" onFocusShell={onFocusShell} />);
    const chip = screen.getByTestId("terminal-command-chip");
    expect(chip.tagName).toBe("BUTTON");
    fireEvent.click(chip);
    expect(onFocusShell).toHaveBeenCalledWith("terminal_zsh_u-ab12cd");
  });

  it("chip falls back to the resource id when display fields are missing", () => {
    render(
      <TerminalCommandCard
        kind="input"
        input="ls"
        stdout={null}
        stderr={null}
        action="send"
        terminalId="terminal_zsh_u-ab12cd"
      />,
    );
    expect(screen.getByTestId("terminal-command-chip").textContent).toBe("→ terminal_zsh_u-ab12cd");
  });

  it("receipt with createdBy carries authorship as a hover title", () => {
    render(
      <TerminalCommandCard {...receipt} action="send" status="ok" createdBy="bryan@example.com" />,
    );
    expect(screen.getByTestId("terminal-command-card").getAttribute("title")).toBe(
      "Run by bryan@example.com",
    );
  });

  it("legacy item ignores createdBy (renders exactly as before)", () => {
    render(
      <TerminalCommandCard
        kind="input"
        input="pwd"
        stdout={null}
        stderr={null}
        createdBy="bryan@example.com"
      />,
    );
    expect(screen.getByTestId("terminal-command-card").getAttribute("title")).toBeNull();
  });

  it("legacy item (no action) renders no chip, no error line, no data attrs", () => {
    render(<TerminalCommandCard kind="input" input="pwd" stdout={null} stderr={null} />);
    const card = screen.getByTestId("terminal-command-card");
    expect(screen.queryByTestId("terminal-command-chip")).toBeNull();
    expect(screen.queryByTestId("terminal-command-error")).toBeNull();
    expect(card.getAttribute("data-terminal-action")).toBeNull();
    expect(card.getAttribute("data-terminal-status")).toBeNull();
  });
});

describe("TerminalCommandCard — shell-state chip", () => {
  const receipt = {
    kind: "input" as const,
    input: "echo hi",
    stdout: null,
    stderr: null,
    action: "send" as const,
    terminalId: "terminal_zsh_u-ab12cd",
    terminalName: "zsh",
    sessionKey: "u-ab12cd",
  };

  it("absent shellState leaves the chip clean (no attr, no hint)", () => {
    render(<TerminalCommandCard {...receipt} onFocusShell={vi.fn()} />);
    const chip = screen.getByTestId("terminal-command-chip");
    expect(chip.getAttribute("data-shell-state")).toBeNull();
    expect(chip.textContent).toBe("→ zsh · u-ab12cd");
  });

  it("live: clickable button, label unchanged (no closed hint)", () => {
    const onFocusShell = vi.fn();
    render(<TerminalCommandCard {...receipt} shellState="live" onFocusShell={onFocusShell} />);
    const chip = screen.getByTestId("terminal-command-chip");
    expect(chip.tagName).toBe("BUTTON");
    expect(chip.getAttribute("data-shell-state")).toBe("live");
    expect(chip.textContent).toBe("→ zsh · u-ab12cd");
    fireEvent.click(chip);
    expect(onFocusShell).toHaveBeenCalledWith("terminal_zsh_u-ab12cd");
  });

  it("closed: still clickable, shows a closed hint", () => {
    const onFocusShell = vi.fn();
    render(<TerminalCommandCard {...receipt} shellState="closed" onFocusShell={onFocusShell} />);
    const chip = screen.getByTestId("terminal-command-chip");
    expect(chip.tagName).toBe("BUTTON");
    expect(chip.getAttribute("data-shell-state")).toBe("closed");
    expect(chip.textContent).toContain("closed");
    fireEvent.click(chip);
    expect(onFocusShell).toHaveBeenCalledWith("terminal_zsh_u-ab12cd");
  });

  it("gone: non-actionable span, closed hint, click does nothing", () => {
    const onFocusShell = vi.fn();
    render(<TerminalCommandCard {...receipt} shellState="gone" onFocusShell={onFocusShell} />);
    const chip = screen.getByTestId("terminal-command-chip");
    expect(chip.tagName).toBe("SPAN");
    expect(chip.getAttribute("data-shell-state")).toBe("gone");
    expect(chip.getAttribute("aria-disabled")).toBe("true");
    expect(chip.textContent).toContain("closed");
    fireEvent.click(chip);
    expect(onFocusShell).not.toHaveBeenCalled();
  });
});
