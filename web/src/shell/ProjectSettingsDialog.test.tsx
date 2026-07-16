import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectSettingsDialog } from "./ProjectSettingsDialog";

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
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ProjectSettingsDialog", () => {
  it("loads metadata and renders value, null, and absent bundle states", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(baseProject, { etag: '"7"' }));
    renderDialog();

    expect(await screen.findByDisplayValue("Alpha")).toBeInTheDocument();
    expect(screen.getByLabelText("Host type")).toHaveValue("managed");
    expect(screen.getByTestId("repo_url-state")).toHaveTextContent("State: value");
    expect(screen.getByTestId("model-state")).toHaveTextContent("explicit clear (null)");
    expect(screen.getByTestId("harness-state")).toHaveTextContent("inherit (absent)");
    expect(screen.getByText("proj_alpha")).toBeInTheDocument();
    expect(screen.getByText("proj-a1b2")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("saves edits with the captured If-Match and renames with the PATCH ETag", async () => {
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
      defaults_json: baseProject.defaults_json,
    });

    const [renameUrl, renameInit] = fetchMock.mock.calls[2] as [string, RequestInit];
    expect(renameUrl).toBe("/v1/projects/proj_alpha/rename");
    expect(renameInit.method).toBe("POST");
    expect(new Headers(renameInit.headers).get("If-Match")).toBe('"8"');
    expect(JSON.parse(renameInit.body as string)).toEqual({ name: "Renamed Alpha" });
  });

  it("preserves set-to-inherit and set-to-clear transitions in the saved bundle", async () => {
    const patched = { ...baseProject, row_version: 8 };
    fetchMock
      .mockResolvedValueOnce(jsonResponse(baseProject, { etag: '"7"' }))
      .mockResolvedValueOnce(jsonResponse(patched, { etag: '"8"' }));
    renderDialog();

    await screen.findByDisplayValue("Alpha");
    fireEvent.click(screen.getByLabelText("Inherit Repository URL"));
    fireEvent.click(screen.getByLabelText("Clear Workspace path"));
    expect(screen.getByTestId("repo_url-state")).toHaveTextContent("inherit (absent)");
    expect(screen.getByTestId("workspace-state")).toHaveTextContent("explicit clear (null)");

    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const body = JSON.parse(fetchMock.mock.calls[1][1]?.body as string);
    expect(body.defaults_json).not.toHaveProperty("repo_url");
    expect(body.defaults_json.workspace).toBeNull();
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

  it("reports a stale ETag and refetches the latest project", async () => {
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
});
