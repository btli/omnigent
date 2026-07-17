import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ResolvedProjectDefaults } from "@/hooks/projectQueries";
import { serializeBundle, type DefaultsDraftState } from "./projectDefaultsDraft";
import { ProjectDefaultsEditor } from "./ProjectDefaultsEditor";

const mocks = vi.hoisted(() => ({
  resolved: vi.fn(),
  hosts: vi.fn(),
  labels: vi.fn(),
  refetch: vi.fn(),
}));

vi.mock("@/hooks/useConversations", () => ({
  useResolvedProjectDefaults: mocks.resolved,
}));

vi.mock("@/hooks/useHosts", () => ({
  useHosts: mocks.hosts,
}));

vi.mock("@/lib/agentLabels", () => ({
  useBrainHarnessLabels: mocks.labels,
}));

const RESOLVED: ResolvedProjectDefaults = {
  host_type: "external",
  host_id: "host-1",
  workspace: "/workspace",
  git: { branch_name: "generated", base_branch: "main" },
  harness_override: "claude-native",
  model_override: "sonnet",
  reasoning_effort: "high",
  row_version: 7,
};

function setResolved(overrides: Partial<ResolvedProjectDefaults> = {}) {
  mocks.resolved.mockReturnValue({
    data: { ...RESOLVED, ...overrides },
    isLoading: false,
    isError: false,
    error: null,
    refetch: mocks.refetch,
  });
}

beforeEach(() => {
  mocks.refetch.mockReset();
  mocks.hosts.mockReturnValue({
    data: [
      { host_id: "host-1", name: "Cloud runner", owner: "owner", status: "online" },
      { host_id: "host-2", name: "Office Mac", owner: "owner", status: "offline" },
    ],
    isLoading: false,
  });
  // Empty brain labels prove native wrappers self-populate from the canonical
  // registry — /v1/harnesses deliberately excludes them.
  mocks.labels.mockReturnValue({});
  setResolved();
});

afterEach(cleanup);

describe("ProjectDefaultsEditor", () => {
  it("populates matched resolved values while preserving raw provenance", async () => {
    render(
      <ProjectDefaultsEditor
        open
        projectId="project-1"
        bundle={{ model: "sonnet" }}
        projectRowVersion={7}
        onDraftChange={vi.fn()}
      />,
    );

    expect(await screen.findByTestId("project-default-host_type-control")).toHaveValue(
      "external",
    );
    expect(screen.getByTestId("project-default-host_type-provenance")).toHaveAttribute(
      "data-provenance",
      "inherited",
    );
    expect(screen.getByTestId("project-default-model-control")).toHaveTextContent("Sonnet 4.6");
    expect(screen.getByTestId("project-default-model-provenance")).toHaveAttribute(
      "data-provenance",
      "overridden",
    );
  });

  it("switches host-type fields live and stages a pinned host for reset", async () => {
    const states: DefaultsDraftState[] = [];
    render(
      <ProjectDefaultsEditor
        open
        projectId="project-1"
        bundle={{ host_id: "host-1" }}
        projectRowVersion={7}
        onDraftChange={(state) => {
          states.push(state);
        }}
      />,
    );

    expect(await screen.findByTestId("project-default-host_id-field")).toBeInTheDocument();
    expect(screen.queryByTestId("project-default-repo_url-field")).toBeNull();
    fireEvent.change(screen.getByTestId("project-default-host_type-control"), {
      target: { value: "managed" },
    });

    expect(screen.getByTestId("project-default-repo_url-field")).toBeInTheDocument();
    expect(screen.queryByTestId("project-default-host_id-field")).toBeNull();
    expect(screen.getByTestId("project-default-host-reset-notice")).toHaveTextContent(
      "Pinned host removed",
    );
    await waitFor(() => expect(states.length).toBeGreaterThan(1));
    const latest = states.at(-1);
    expect(latest).toBeDefined();
    if (!latest) throw new Error("Expected the managed draft state");
    expect(serializeBundle(latest)).not.toHaveProperty("host_id");
  });

  it("updates model and effort catalogs after dependency changes", async () => {
    const onValidityChange = vi.fn();
    render(
      <ProjectDefaultsEditor
        open
        projectId="project-1"
        bundle={{ harness: "codex-native", model: "legacy-model" }}
        projectRowVersion={7}
        onDraftChange={vi.fn()}
        onValidityChange={onValidityChange}
      />,
    );

    expect(await screen.findByTestId("project-default-model-control")).toBeDisabled();
    fireEvent.pointerDown(screen.getByTestId("project-default-harness-control"), {
      button: 0,
    });
    fireEvent.click(screen.getByTestId("project-default-harness-option-claude-native"));
    expect(screen.getByTestId("project-default-model-control")).not.toBeDisabled();
    expect(screen.getByTestId("project-default-model-error")).toHaveTextContent(
      "compatible with the selected harness",
    );
    await waitFor(() => expect(onValidityChange).toHaveBeenLastCalledWith(false));
    fireEvent.pointerDown(screen.getByTestId("project-default-model-control"), { button: 0 });
    expect(screen.getByTestId("project-default-model-option-opus")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("project-default-model-option-opus"));
    expect(screen.queryByTestId("project-default-model-error")).toBeNull();
    await waitFor(() => expect(onValidityChange).toHaveBeenLastCalledWith(true));
    fireEvent.pointerDown(screen.getByTestId("project-default-reasoning_effort-control"), {
      button: 0,
    });
    expect(
      screen.getByTestId("project-default-reasoning_effort-option-xhigh"),
    ).toBeInTheDocument();
  });

  it("restores legacy-value preservation when a changed harness returns to its opening value", async () => {
    render(
      <ProjectDefaultsEditor
        open
        projectId="project-1"
        bundle={{ harness: "codex-native", model: "legacy-model" }}
        projectRowVersion={7}
        onDraftChange={vi.fn()}
      />,
    );

    // Opening state: gated catalog preserves the stored model without error.
    expect(await screen.findByTestId("project-default-model-control")).toBeInTheDocument();
    expect(screen.queryByTestId("project-default-model-error")).toBeNull();
    fireEvent.pointerDown(screen.getByTestId("project-default-harness-control"), {
      button: 0,
    });
    fireEvent.click(screen.getByTestId("project-default-harness-option-claude-native"));
    expect(screen.getByTestId("project-default-model-error")).toBeInTheDocument();

    fireEvent.pointerDown(screen.getByTestId("project-default-harness-control"), {
      button: 0,
    });
    fireEvent.click(screen.getByTestId("project-default-harness-option-codex-native"));
    expect(screen.queryByTestId("project-default-model-error")).toBeNull();
  });

  it("defers draft construction and refetches once for a row-version mismatch", async () => {
    setResolved({ row_version: 6 });
    const { rerender } = render(
      <ProjectDefaultsEditor
        open
        projectId="project-1"
        bundle={{}}
        projectRowVersion={7}
        onDraftChange={vi.fn()}
      />,
    );

    expect(screen.getByTestId("project-defaults-loading")).toBeInTheDocument();
    await waitFor(() => expect(mocks.refetch).toHaveBeenCalledTimes(1));
    rerender(
      <ProjectDefaultsEditor
        open
        projectId="project-1"
        bundle={{}}
        projectRowVersion={7}
        onDraftChange={vi.fn()}
      />,
    );
    expect(mocks.refetch).toHaveBeenCalledTimes(1);
  });

  it("clears a previous draft while a newer project row awaits its resolved preview", async () => {
    const { rerender } = render(
      <ProjectDefaultsEditor
        open
        projectId="project-1"
        bundle={{ model: "sonnet" }}
        projectRowVersion={7}
        onDraftChange={vi.fn()}
      />,
    );

    expect(await screen.findByTestId("project-default-model-control")).toBeInTheDocument();
    rerender(
      <ProjectDefaultsEditor
        open
        projectId="project-1"
        bundle={{ model: "opus" }}
        projectRowVersion={8}
        onDraftChange={vi.fn()}
      />,
    );

    await waitFor(() =>
      expect(screen.getByTestId("project-defaults-loading")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("project-default-model-control")).toBeNull();
  });

  it("shows a skeleton while the resolved preview is loading", () => {
    mocks.resolved.mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
      error: null,
      refetch: mocks.refetch,
    });
    render(
      <ProjectDefaultsEditor
        open
        projectId="project-1"
        bundle={{}}
        projectRowVersion={7}
        onDraftChange={vi.fn()}
      />,
    );

    expect(screen.getByTestId("project-defaults-loading")).toBeInTheDocument();
  });

  it("keeps raw overrides visible and editable on preview error, with retry", async () => {
    mocks.resolved.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error("Preview unavailable"),
      refetch: mocks.refetch,
    });
    render(
      <ProjectDefaultsEditor
        open
        projectId="project-1"
        bundle={{ model: "legacy-model" }}
        projectRowVersion={7}
        onDraftChange={vi.fn()}
      />,
    );

    expect(await screen.findByTestId("project-defaults-error")).toHaveTextContent(
      "Preview unavailable",
    );
    expect(screen.getByTestId("project-default-model-control")).toHaveTextContent(
      "legacy-model",
    );
    // Controls stay enabled: a preview error is often caused by the very
    // bundle these fields repair (e.g. a branch without a host). The model
    // picker stays gated by its own missing-catalog rule (no harness), which
    // is independent of the preview error.
    expect(screen.getByTestId("project-default-host_type-control")).not.toBeDisabled();
    expect(screen.getByTestId("project-default-model-reset")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry resolved defaults" }));
    expect(mocks.refetch).toHaveBeenCalledTimes(1);
  });

  it("surfaces null host type as a saveable repair state", async () => {
    mocks.resolved.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error("Invalid host type"),
      refetch: mocks.refetch,
    });
    const onValidityChange = vi.fn();
    render(
      <ProjectDefaultsEditor
        open
        projectId="project-1"
        bundle={{ host_type: null }}
        projectRowVersion={7}
        onDraftChange={vi.fn()}
        onValidityChange={onValidityChange}
      />,
    );

    expect(await screen.findByTestId("project-default-host_type-provenance")).toHaveAttribute(
      "data-provenance",
      "invalid",
    );
    expect(screen.getByTestId("project-default-host_type-control")).toHaveValue("external");
    await waitFor(() => expect(onValidityChange).toHaveBeenLastCalledWith(true));
  });

  it("lets a broken branch-without-host bundle be repaired under an errored preview", async () => {
    // The real resolver rejects an external default_branch without a pinned
    // host, so the preview arrives as an error — the editor must still expose
    // enabled host/branch controls to fix exactly that.
    mocks.resolved.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error("External project default_branch requires a pinned host_id"),
      refetch: mocks.refetch,
    });
    const onValidityChange = vi.fn();
    render(
      <ProjectDefaultsEditor
        open
        projectId="project-1"
        bundle={{ host_type: "external", default_branch: "feature" }}
        projectRowVersion={7}
        onDraftChange={vi.fn()}
        onValidityChange={onValidityChange}
      />,
    );

    expect(await screen.findByTestId("project-default-host_id-error")).toBeInTheDocument();
    expect(screen.getByTestId("project-default-default_branch-error")).toBeInTheDocument();
    await waitFor(() => expect(onValidityChange).toHaveBeenLastCalledWith(false));

    // The repair controls are usable: the host picker is enabled and offers
    // the loaded hosts, and resetting the branch is possible too.
    const hostControl = screen.getByTestId("project-default-host_id-control");
    expect(hostControl).not.toBeDisabled();
    fireEvent.pointerDown(hostControl, { button: 0 });
    fireEvent.click(screen.getByTestId("project-default-host_id-option-host-1"));
    await waitFor(() => expect(onValidityChange).toHaveBeenLastCalledWith(true));
  });

  it("invalidates a reset of the pinned host that strands a default branch", async () => {
    // The stored host equals its own resolved echo, so the post-reset display
    // must drop to empty — validation then catches the stranded branch.
    setResolved({ host_id: "host-1", git: { branch_name: "generated", base_branch: "feature" } });
    const onValidityChange = vi.fn();
    render(
      <ProjectDefaultsEditor
        open
        projectId="project-1"
        bundle={{ host_id: "host-1", default_branch: "feature" }}
        projectRowVersion={7}
        onDraftChange={vi.fn()}
        onValidityChange={onValidityChange}
      />,
    );

    expect(await screen.findByTestId("project-default-host_id-control")).toBeInTheDocument();
    await waitFor(() => expect(onValidityChange).toHaveBeenLastCalledWith(true));
    fireEvent.click(screen.getByTestId("project-default-host_id-reset"));
    expect(await screen.findByTestId("project-default-host_id-error")).toBeInTheDocument();
    await waitFor(() => expect(onValidityChange).toHaveBeenLastCalledWith(false));
  });

  it("blocks saving a workspace carried across an External to Managed switch", async () => {
    const onValidityChange = vi.fn();
    render(
      <ProjectDefaultsEditor
        open
        projectId="project-1"
        bundle={{ workspace: "/Users/me/repo" }}
        projectRowVersion={7}
        onDraftChange={vi.fn()}
        onValidityChange={onValidityChange}
      />,
    );

    expect(await screen.findByTestId("project-default-workspace-field")).toBeInTheDocument();
    await waitFor(() => expect(onValidityChange).toHaveBeenLastCalledWith(true));
    fireEvent.change(screen.getByTestId("project-default-host_type-control"), {
      target: { value: "managed" },
    });

    expect(screen.getByTestId("project-default-workspace-error")).toHaveTextContent(
      "Reset it to save",
    );
    await waitFor(() => expect(onValidityChange).toHaveBeenLastCalledWith(false));
    fireEvent.click(screen.getByTestId("project-default-workspace-reset"));
    await waitFor(() => expect(onValidityChange).toHaveBeenLastCalledWith(true));
  });
});
