import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ResolvedProjectDefaults } from "@/hooks/projectQueries";
import { ProjectSettingsDialog } from "./ProjectSettingsDialog";

const hookMocks = vi.hoisted(() => ({
  resolved: vi.fn(),
  hosts: vi.fn(),
  labels: vi.fn(),
  refetchResolved: vi.fn(),
}));

vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useConversations")>()),
  useResolvedProjectDefaults: hookMocks.resolved,
}));

vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useHosts")>()),
  useHosts: hookMocks.hosts,
}));

vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/agentLabels")>()),
  useBrainHarnessLabels: hookMocks.labels,
}));

const baseProject = {
  id: "proj_alpha",
  name: "Alpha",
  description: "Current description",
  storage_key: "proj-a1b2",
  defaults_json: {
    host_type: "managed",
    repo_url: "https://github.com/example/alpha.git",
    workspace: "/srv/alpha",
    model: null,
  },
  defaults_schema_version: 1,
  row_version: 7,
  created_at: 1_700_000_000,
  updated_at: 1_700_000_100,
  archived_at: null,
};

const baseResolved: ResolvedProjectDefaults = {
  host_type: "managed",
  host_id: null,
  workspace: "/srv/alpha",
  git: null,
  harness_override: null,
  model_override: null,
  reasoning_effort: null,
  row_version: 7,
};

function setResolved(overrides: Partial<ResolvedProjectDefaults> = {}) {
  hookMocks.resolved.mockReturnValue({
    data: { ...baseResolved, ...overrides },
    isLoading: false,
    isError: false,
    error: null,
    refetch: hookMocks.refetchResolved,
  });
}

function jsonResponse(
  body: unknown,
  { status = 200, etag }: { status?: number; etag?: string } = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    statusText: status >= 400 ? "Request failed" : "OK",
    headers: {
      "Content-Type": "application/json",
      ...(etag ? { ETag: etag } : {}),
    },
  });
}

const fetchMock = vi.fn<typeof fetch>();

function renderDialog(onOpenChange = vi.fn()) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <ProjectSettingsDialog projectId="proj_alpha" open onOpenChange={onOpenChange} />
    </QueryClientProvider>,
  );
  return { onOpenChange, queryClient };
}

beforeEach(() => {
  fetchMock.mockReset();
  hookMocks.refetchResolved.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  setResolved();
  hookMocks.hosts.mockReturnValue({
    data: [
      { host_id: "host-1", name: "Cloud runner", owner: "owner", status: "online" },
      { host_id: "host-2", name: "Office Mac", owner: "owner", status: "offline" },
    ],
    isLoading: false,
  });
  hookMocks.labels.mockReturnValue({
    "claude-native": "Claude Code",
    "codex-native": "Codex",
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ProjectSettingsDialog", () => {
  it("loads metadata and renders value, null, and absent provenance", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(baseProject, { etag: '"7"' }));
    renderDialog();

    expect(await screen.findByDisplayValue("Alpha")).toBeInTheDocument();
    expect(screen.getByTestId("project-default-host_type-control")).toHaveValue("managed");
    expect(screen.getByTestId("project-default-repo_url-provenance")).toHaveAttribute(
      "data-provenance",
      "overridden",
    );
    expect(screen.getByTestId("project-default-model-provenance")).toHaveAttribute(
      "data-provenance",
      "inherited",
    );
    expect(screen.getByTestId("project-default-model-control")).toHaveTextContent(
      "Harness default",
    );
    expect(screen.getByTestId("project-default-harness-provenance")).toHaveAttribute(
      "data-provenance",
      "inherited",
    );
    expect(screen.getByText("proj_alpha")).toBeInTheDocument();
    expect(screen.getByText("proj-a1b2")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("saves normalized edits with the captured If-Match and renames with the PATCH ETag", async () => {
    const patched = {
      ...baseProject,
      description: "Updated description",
      row_version: 8,
    };
    const renamed = { ...patched, name: "Renamed Alpha", row_version: 9 };
    fetchMock
      .mockResolvedValueOnce(jsonResponse(baseProject, { etag: '"7"' }))
      .mockResolvedValueOnce(jsonResponse(patched, { etag: '"8"' }))
      .mockResolvedValueOnce(jsonResponse(renamed, { etag: '"9"' }));
    const { onOpenChange } = renderDialog();

    fireEvent.change(await screen.findByLabelText("Name"), {
      target: { value: "Renamed Alpha" },
    });
    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Updated description" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(fetchMock).toHaveBeenCalledTimes(3);

    const [patchUrl, patchInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(patchUrl).toBe("/v1/projects/proj_alpha");
    expect(patchInit.method).toBe("PATCH");
    expect(new Headers(patchInit.headers).get("If-Match")).toBe('"7"');
    expect(JSON.parse(patchInit.body as string)).toMatchObject({
      description: "Updated description",
      defaults_json: {
        host_type: "managed",
        repo_url: "https://github.com/example/alpha.git",
        workspace: "/srv/alpha",
      },
    });
    expect(JSON.parse(patchInit.body as string).defaults_json).not.toHaveProperty("model");

    const [renameUrl, renameInit] = fetchMock.mock.calls[2] as [string, RequestInit];
    expect(renameUrl).toBe("/v1/projects/proj_alpha/rename");
    expect(renameInit.method).toBe("POST");
    expect(new Headers(renameInit.headers).get("If-Match")).toBe('"8"');
    expect(JSON.parse(renameInit.body as string)).toEqual({ name: "Renamed Alpha" });
  });

  it("removes an override with Reset and creates one by editing", async () => {
    const patched = { ...baseProject, row_version: 8 };
    fetchMock
      .mockResolvedValueOnce(jsonResponse(baseProject, { etag: '"7"' }))
      .mockResolvedValueOnce(jsonResponse(patched, { etag: '"8"' }));
    renderDialog();

    await screen.findByDisplayValue("Alpha");
    fireEvent.click(screen.getByTestId("project-default-repo_url-reset"));
    fireEvent.change(screen.getByTestId("project-default-default_branch-control"), {
      target: { value: "release" },
    });
    expect(screen.getByTestId("project-default-repo_url-provenance")).toHaveAttribute(
      "data-provenance",
      "inherited",
    );
    expect(screen.getByTestId("project-default-default_branch-provenance")).toHaveAttribute(
      "data-provenance",
      "overridden",
    );

    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const body = JSON.parse(fetchMock.mock.calls[1][1]?.body as string);
    expect(body.defaults_json).not.toHaveProperty("repo_url");
    expect(body.defaults_json.default_branch).toBe("release");
    expect(Object.values(body.defaults_json)).not.toContain(null);
  });

  it("shows a rename collision inline on the name field", async () => {
    const patched = { ...baseProject, row_version: 8 };
    fetchMock
      .mockResolvedValueOnce(jsonResponse(baseProject, { etag: '"7"' }))
      .mockResolvedValueOnce(jsonResponse(patched, { etag: '"8"' }))
      .mockResolvedValueOnce(
        jsonResponse({ error: { message: "Project name already exists" } }, { status: 409 }),
      );
    renderDialog();

    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "Beta" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("A project with this name already exists.");
    expect(screen.getByLabelText("Name")).toHaveAttribute("aria-describedby", alert.id);
  });

  it("reports a stale ETag, reloads the project, and refreshes the resolved baseline", async () => {
    const latest = { ...baseProject, name: "Alpha from server", row_version: 8 };
    fetchMock
      .mockResolvedValueOnce(jsonResponse(baseProject, { etag: '"7"' }))
      .mockResolvedValueOnce(
        jsonResponse({ error: { message: "Precondition failed" } }, { status: 412 }),
      )
      .mockResolvedValueOnce(jsonResponse(latest, { etag: '"8"' }));
    renderDialog();

    await screen.findByDisplayValue("Alpha");
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/changed elsewhere/i);
    expect(await screen.findByDisplayValue("Alpha from server")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await waitFor(() => expect(hookMocks.refetchResolved).toHaveBeenCalledTimes(1));
  });

  it("surfaces the server validation message for a 422", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(baseProject, { etag: '"7"' }))
      .mockResolvedValueOnce(
        jsonResponse(
          { error: { message: "Invalid project defaults: managed defaults prohibit host_id" } },
          { status: 422 },
        ),
      );
    renderDialog();

    await screen.findByDisplayValue("Alpha");
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Invalid project defaults: managed defaults prohibit host_id",
    );
  });

  it("repairs a null host type by omitting it on save", async () => {
    const invalidProject = {
      ...baseProject,
      defaults_json: { host_type: null, model: null },
    };
    hookMocks.resolved.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error("Invalid host type"),
      refetch: hookMocks.refetchResolved,
    });
    fetchMock
      .mockResolvedValueOnce(jsonResponse(invalidProject, { etag: '"7"' }))
      .mockResolvedValueOnce(jsonResponse({ ...invalidProject, row_version: 8 }, { etag: '"8"' }));
    renderDialog();

    expect(await screen.findByTestId("project-default-host_type-provenance")).toHaveAttribute(
      "data-provenance",
      "invalid",
    );
    const save = screen.getByRole("button", { name: "Save settings" });
    expect(save).toBeEnabled();
    fireEvent.click(save);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const body = JSON.parse(fetchMock.mock.calls[1][1]?.body as string);
    expect(body.defaults_json).toEqual({});
  });

  it("switches host type and conditional defaults live", async () => {
    const externalProject = {
      ...baseProject,
      defaults_json: { host_type: "external", host_id: "host-1" },
    };
    setResolved({
      host_type: "external",
      host_id: "host-1",
      workspace: null,
      git: null,
    });
    fetchMock.mockResolvedValueOnce(jsonResponse(externalProject, { etag: '"7"' }));
    renderDialog();

    expect(await screen.findByTestId("project-default-host_id-field")).toBeInTheDocument();
    expect(screen.queryByTestId("project-default-repo_url-field")).toBeNull();
    fireEvent.change(screen.getByTestId("project-default-host_type-control"), {
      target: { value: "managed" },
    });
    expect(screen.queryByTestId("project-default-host_id-field")).toBeNull();
    expect(screen.getByTestId("project-default-repo_url-field")).toBeInTheDocument();
    expect(screen.getByTestId("project-default-host_type-provenance")).toHaveAttribute(
      "data-provenance",
      "overridden",
    );
  });

  it("updates dependent model and effort catalogs inside the dialog", async () => {
    const project = {
      ...baseProject,
      defaults_json: { host_type: "managed", harness: "codex-native", model: "legacy-model" },
    };
    setResolved({
      harness_override: "codex-native",
      model_override: "legacy-model",
    });
    fetchMock.mockResolvedValueOnce(jsonResponse(project, { etag: '"7"' }));
    renderDialog();

    expect(await screen.findByTestId("project-default-model-control")).toBeDisabled();
    fireEvent.pointerDown(screen.getByTestId("project-default-harness-control"), { button: 0 });
    fireEvent.click(screen.getByTestId("project-default-harness-option-claude-native"));
    fireEvent.pointerDown(screen.getByTestId("project-default-model-control"), { button: 0 });
    fireEvent.click(screen.getByTestId("project-default-model-option-opus"));
    fireEvent.pointerDown(screen.getByTestId("project-default-reasoning_effort-control"), {
      button: 0,
    });
    expect(screen.getByTestId("project-default-reasoning_effort-option-max")).toBeInTheDocument();
  });

  it("preserves unknown catalog values when saved untouched", async () => {
    const project = {
      ...baseProject,
      defaults_json: {
        host_type: "managed",
        harness: "legacy-harness",
        model: "legacy-model",
      },
    };
    setResolved({
      harness_override: "legacy-harness",
      model_override: "legacy-model",
    });
    fetchMock
      .mockResolvedValueOnce(jsonResponse(project, { etag: '"7"' }))
      .mockResolvedValueOnce(jsonResponse({ ...project, row_version: 8 }, { etag: '"8"' }));
    renderDialog();

    expect(await screen.findByTestId("project-default-harness-control")).toHaveTextContent(
      "legacy-harness (not in current catalog)",
    );
    expect(screen.getByTestId("project-default-model-control")).toHaveTextContent(
      "legacy-model (not in current catalog)",
    );
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const body = JSON.parse(fetchMock.mock.calls[1][1]?.body as string);
    expect(body.defaults_json).toEqual(project.defaults_json);
  });

  it("disables Save for an external branch without a host", async () => {
    const project = {
      ...baseProject,
      defaults_json: { host_type: "external", default_branch: "feature" },
    };
    setResolved({
      host_type: "external",
      host_id: null,
      workspace: null,
      git: { branch_name: "generated", base_branch: "feature" },
    });
    fetchMock.mockResolvedValueOnce(jsonResponse(project, { etag: '"7"' }));
    renderDialog();

    expect(await screen.findByTestId("project-default-host_id-error")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save settings" })).toBeDisabled();
  });
});
