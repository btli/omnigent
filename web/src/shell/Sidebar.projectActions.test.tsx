// Tests for the sidebar project-folder actions:
//   1. A project folder's kebab and its header right-click context menu both
//      offer "Rename project" + "Delete project" from the shared
//      ProjectMenuItems body.
//   2. Rename enters an inline editor (ProjectEditRow); committing re-labels the
//      project via useRenameProject ({ from, to }).
//   3. Renaming onto an existing project name is blocked with an inline error
//      (no mutation fires).
//   4. Delete opens the confirmation dialog and archives the project.
// See ProjectFolder / ProjectFolderMenu / ProjectEditRow in Sidebar.tsx.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

// Controllable project mutations so the tests can assert what was forwarded.
// Declared via vi.hoisted so the (hoisted) vi.mock factory can reference them.
const mocks = vi.hoisted(() => ({
  rename: { mutate: vi.fn() },
  del: { mutate: vi.fn(), isPending: false, isError: false },
  showToast: vi.fn(),
}));

// Spy on the toast surface so the rename-failure path can be asserted without
// mounting a <Toaster />.
vi.mock("@/components/ui/toast", () => ({
  showToast: mocks.showToast,
  Toaster: () => null,
}));

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({
    mutate: vi.fn(),
    reset: vi.fn(),
    isPending: false,
    isError: false,
    variables: undefined,
  }),
  usePinnedConversationBackfill: () => [],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useStopSession: () => ({ mutate: vi.fn() }),
  // Two projects so a collision (rename Alpha → Beta) can be exercised.
  useProjects: () => ({ data: ["Alpha", "Beta"] }),
  // Superset the rename collision guard reads: includes an archived-only
  // project that useProjects (active-only) does not surface.
  useAllProjectNames: () => ({ data: ["Alpha", "Beta", "Gamma"] }),
  useProjectSessions: () => ({
    data: undefined,
    isLoading: false,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
  }),
  useMoveToProject: () => ({ mutate: vi.fn() }),
  useDeleteProject: () => mocks.del,
  useRenameProject: () => mocks.rename,
  fetchProjectSessionIds: () => Promise.resolve([]),
  PROJECT_LABEL_KEY: "omni_project",
}));

// Heavy sibling widgets pull their own hooks/providers; stub them so this
// test stays scoped to the sidebar's project folders.
vi.mock("./AgentTypeFilter", () => ({ AgentTypeFilter: () => null }));
vi.mock("./ReportIssueButton", () => ({ ReportIssueButton: () => null }));
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { useConversations } from "@/hooks/useConversations";
import { __resetReadStateForTests } from "@/hooks/useUnseenConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

// No conversations needed: empty projects still render as folders (the folder
// list comes from useProjects, not the loaded window).
function mockEmptyConversations() {
  const dataResult = {
    data: {
      pages: [{ data: [], first_id: null, last_id: null, has_more: false }],
      pageParams: [undefined],
    },
    isLoading: false,
    isError: false,
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  } as unknown as ReturnType<typeof useConversations>;
  useConvMock.mockImplementation(() => dataResult);
}

function renderSidebar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/"]}>
          <Sidebar open={true} onClose={vi.fn()} />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

/** The <section> wrapping a named project folder (header lives inside it). */
function projectSection(name: string): HTMLElement {
  return screen.getByText(name).closest("section") as HTMLElement;
}

beforeEach(() => {
  mocks.rename.mutate.mockReset();
  mocks.del.mutate.mockReset();
  mocks.showToast.mockReset();
  useConvMock.mockReset();
  // Section/project collapse + pin state persist in localStorage; clear it so a
  // prior test can't leave the Projects group collapsed.
  globalThis.localStorage?.clear();
  __resetReadStateForTests();
  mockEmptyConversations();
});

afterEach(cleanup);

describe("project folder kebab", () => {
  it("offers New session, Rename and Delete", () => {
    renderSidebar();

    // Open the Alpha folder's kebab (Radix DropdownMenu opens on pointerdown).
    const kebab = within(projectSection("Alpha")).getByTestId("project-actions");
    fireEvent.pointerDown(kebab, { button: 0 });

    // asChild renders the item as the <Link>'s anchor, carrying the testid.
    const newSession = screen.getByTestId("project-new-session-item");
    expect(newSession).toHaveTextContent("New session");
    // New session pre-files the composer under this project.
    expect(newSession).toHaveAttribute("href", "/?project=Alpha");
    expect(screen.getByTestId("rename-project")).toHaveTextContent("Rename project");
    expect(screen.getByTestId("delete-project")).toHaveTextContent("Delete project");
  });
});

describe("project folder right-click context menu", () => {
  it("opens the same actions as the kebab", () => {
    renderSidebar();

    // Nothing rendered until the header is right-clicked.
    expect(screen.queryByTestId("rename-project")).toBeNull();

    fireEvent.contextMenu(screen.getByText("Alpha"));

    // Same testids as the kebab — proves both render from ProjectMenuItems.
    expect(screen.getByTestId("project-new-session-item")).toBeInTheDocument();
    expect(screen.getByTestId("rename-project")).toBeInTheDocument();
    expect(screen.getByTestId("delete-project")).toBeInTheDocument();
  });
});

describe("rename project", () => {
  it("enters inline edit and re-labels the project on Enter", () => {
    renderSidebar();

    fireEvent.contextMenu(screen.getByText("Alpha"));
    fireEvent.click(screen.getByTestId("rename-project"));

    const input = screen.getByTestId("rename-project-input") as HTMLInputElement;
    expect(input.value).toBe("Alpha");
    fireEvent.change(input, { target: { value: "Renamed" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(mocks.rename.mutate).toHaveBeenCalledTimes(1);
    expect(mocks.rename.mutate).toHaveBeenCalledWith(
      { from: "Alpha", to: "Renamed" },
      expect.anything(),
    );
  });

  it("blocks renaming onto an existing project name with an inline error", () => {
    renderSidebar();

    fireEvent.contextMenu(screen.getByText("Alpha"));
    fireEvent.click(screen.getByTestId("rename-project"));

    const input = screen.getByTestId("rename-project-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "Beta" } });
    fireEvent.keyDown(input, { key: "Enter" });

    // Collision surfaces an error and keeps editing — no rename fires.
    expect(screen.getByRole("alert")).toHaveTextContent(/already exists/i);
    expect(mocks.rename.mutate).not.toHaveBeenCalled();
    expect(screen.getByTestId("rename-project-input")).toBeInTheDocument();
  });

  it("blocks renaming onto an archived-only project name", () => {
    renderSidebar();

    fireEvent.contextMenu(screen.getByText("Alpha"));
    fireEvent.click(screen.getByTestId("rename-project"));

    const input = screen.getByTestId("rename-project-input") as HTMLInputElement;
    // "Gamma" only exists among archived sessions (absent from useProjects);
    // renaming onto it would silently merge the two projects.
    fireEvent.change(input, { target: { value: "Gamma" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(screen.getByRole("alert")).toHaveTextContent(/already exists/i);
    expect(mocks.rename.mutate).not.toHaveBeenCalled();
    expect(screen.getByTestId("rename-project-input")).toBeInTheDocument();
  });

  it("exits without renaming when the name is unchanged", () => {
    renderSidebar();

    fireEvent.contextMenu(screen.getByText("Alpha"));
    fireEvent.click(screen.getByTestId("rename-project"));

    const input = screen.getByTestId("rename-project-input");
    fireEvent.keyDown(input, { key: "Enter" });

    expect(mocks.rename.mutate).not.toHaveBeenCalled();
    expect(screen.queryByTestId("rename-project-input")).toBeNull();
  });

  it("keeps editing and shows the error when a collision is committed via blur", () => {
    renderSidebar();

    fireEvent.contextMenu(screen.getByText("Alpha"));
    fireEvent.click(screen.getByTestId("rename-project"));

    const input = screen.getByTestId("rename-project-input");
    fireEvent.change(input, { target: { value: "Beta" } });
    // Blur is otherwise a commit path; a collision must not silently drop it.
    fireEvent.blur(input);

    expect(screen.getByRole("alert")).toHaveTextContent(/already exists/i);
    expect(screen.getByTestId("rename-project-input")).toBeInTheDocument();
    expect(mocks.rename.mutate).not.toHaveBeenCalled();
  });

  it("surfaces a toast when the rename mutation fails", () => {
    renderSidebar();

    fireEvent.contextMenu(screen.getByText("Alpha"));
    fireEvent.click(screen.getByTestId("rename-project"));

    const input = screen.getByTestId("rename-project-input");
    fireEvent.change(input, { target: { value: "Renamed" } });
    fireEvent.keyDown(input, { key: "Enter" });

    // The commit wires both handlers; a failure must not be silent.
    const [, opts] = mocks.rename.mutate.mock.calls[0];
    expect(typeof opts.onSuccess).toBe("function");
    opts.onError(new Error("some sessions could not be moved"));
    expect(mocks.showToast).toHaveBeenCalledTimes(1);
  });
});

describe("delete project", () => {
  it("confirms then archives the project", () => {
    renderSidebar();

    const kebab = within(projectSection("Alpha")).getByTestId("project-actions");
    fireEvent.pointerDown(kebab, { button: 0 });
    fireEvent.click(screen.getByTestId("delete-project"));

    // Confirmation dialog appears; confirming archives the project's sessions.
    expect(screen.getByText("Delete project?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete project" }));

    expect(mocks.del.mutate).toHaveBeenCalledTimes(1);
    expect(mocks.del.mutate).toHaveBeenCalledWith("Alpha", expect.anything());
  });
});
