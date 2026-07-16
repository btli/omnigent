import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

const mocks = vi.hoisted(() => ({
  projects: [
    { id: "proj_alpha", name: "Alpha" },
    { id: "proj_beta", name: "Beta" },
  ],
  rename: { mutate: vi.fn(), isPending: false },
  del: { mutate: vi.fn(), isPending: false, isError: false },
  showToast: vi.fn(),
}));

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
  useProjects: () => ({ data: mocks.projects }),
  useProjectSessions: () => ({
    data: undefined,
    isLoading: false,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
  }),
  useMoveToProject: () => ({ mutate: vi.fn() }),
  useCreateProject: () => ({ mutate: vi.fn(), isPending: false }),
  useDeleteProject: () => mocks.del,
  useRenameProject: () => mocks.rename,
  fetchProjectSessionIds: () => Promise.resolve([]),
}));

vi.mock("./AgentTypeFilter", () => ({ AgentTypeFilter: () => null }));
vi.mock("./ReportIssueButton", () => ({ ReportIssueButton: () => null }));
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { useConversations } from "@/hooks/useConversations";
import { __resetReadStateForTests } from "@/hooks/useUnseenConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

function mockEmptyConversations() {
  useConvMock.mockReturnValue({
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
  } as unknown as ReturnType<typeof useConversations>);
}

function renderSidebar() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/"]}>
          <Sidebar open={true} onClose={vi.fn()} />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

function projectSection(name: string): HTMLElement {
  return screen.getByText(name).closest("section") as HTMLElement;
}

function beginRename(name = "Alpha") {
  fireEvent.contextMenu(screen.getByText(name));
  fireEvent.click(screen.getByTestId("rename-project"));
  return screen.getByTestId("rename-project-input") as HTMLInputElement;
}

beforeEach(() => {
  mocks.projects = [
    { id: "proj_alpha", name: "Alpha" },
    { id: "proj_beta", name: "Beta" },
  ];
  mocks.rename.mutate.mockReset();
  mocks.del.mutate.mockReset();
  mocks.showToast.mockReset();
  useConvMock.mockReset();
  try {
    window.localStorage.clear();
  } catch {
    // Node 26's jsdom environment may not expose localStorage.
  }
  __resetReadStateForTests();
  mockEmptyConversations();
});

afterEach(cleanup);

describe("project folder actions", () => {
  it("offers New session, Rename and Delete from the kebab", () => {
    renderSidebar();

    const kebab = within(projectSection("Alpha")).getByTestId("project-actions");
    fireEvent.pointerDown(kebab, { button: 0 });

    const newSession = screen.getByTestId("project-new-session-item");
    expect(newSession).toHaveTextContent("New session");
    expect(newSession).toHaveAttribute("href", "/?project_id=proj_alpha");
    expect(screen.getByTestId("rename-project")).toHaveTextContent("Rename project");
    expect(screen.getByTestId("delete-project")).toHaveTextContent("Delete project");
  });

  it("opens the same actions from the folder header's right-click menu", () => {
    renderSidebar();

    expect(screen.queryByTestId("rename-project")).toBeNull();
    fireEvent.contextMenu(screen.getByText("Alpha"));

    expect(screen.getByTestId("project-new-session-item")).toHaveAttribute(
      "href",
      "/?project_id=proj_alpha",
    );
    expect(screen.getByTestId("rename-project")).toBeInTheDocument();
    expect(screen.getByTestId("delete-project")).toBeInTheDocument();
  });
});

describe("rename project", () => {
  it("enters inline edit and renames the project on Enter", () => {
    renderSidebar();

    const input = beginRename();
    expect(input.value).toBe("Alpha");
    expect(input).toHaveFocus();
    fireEvent.change(input, { target: { value: "Renamed" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(mocks.rename.mutate).toHaveBeenCalledWith(
      { id: "proj_alpha", name: "Renamed" },
      expect.anything(),
    );
  });

  it.each(["Enter", "blur"])("shows a 409 collision inline via %s", (commitPath) => {
    renderSidebar();

    const input = beginRename();
    fireEvent.change(input, { target: { value: "Beta" } });
    if (commitPath === "Enter") fireEvent.keyDown(input, { key: "Enter" });
    else fireEvent.blur(input);

    const options = mocks.rename.mutate.mock.calls[0][1];
    act(() => options.onError({ status: 409 }));

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/already exists/i);
    expect(screen.getByTestId("rename-project-input")).toHaveAttribute(
      "aria-describedby",
      alert.id,
    );
    expect(screen.getByTestId("rename-project-input")).toBeInTheDocument();
    expect(mocks.showToast).not.toHaveBeenCalled();
  });

  it("exits without a request when the name is unchanged or empty", () => {
    renderSidebar();

    const input = beginRename();
    fireEvent.keyDown(input, { key: "Enter" });

    expect(mocks.rename.mutate).not.toHaveBeenCalled();
    expect(screen.queryByTestId("rename-project-input")).toBeNull();

    const emptyInput = beginRename();
    fireEvent.change(emptyInput, { target: { value: "   " } });
    fireEvent.blur(emptyInput);

    expect(mocks.rename.mutate).not.toHaveBeenCalled();
    expect(screen.queryByTestId("rename-project-input")).toBeNull();
  });

  it("toasts and exits when the project changed elsewhere", () => {
    renderSidebar();

    const input = beginRename();
    fireEvent.change(input, { target: { value: "Renamed" } });
    fireEvent.keyDown(input, { key: "Enter" });
    const options = mocks.rename.mutate.mock.calls[0][1];
    act(() => options.onError({ status: 412 }));

    expect(mocks.showToast).toHaveBeenCalledWith(expect.stringMatching(/changed elsewhere/i));
    expect(screen.queryByTestId("rename-project-input")).toBeNull();
  });

  it("keeps an expanded folder open across a rename", () => {
    const { rerender } = renderSidebar();

    fireEvent.click(screen.getByText("Alpha"));
    expect(within(projectSection("Alpha")).getByText("No chats")).toBeInTheDocument();

    const input = beginRename();
    fireEvent.change(input, { target: { value: "Renamed" } });
    fireEvent.keyDown(input, { key: "Enter" });
    const options = mocks.rename.mutate.mock.calls[0][1];
    act(() => {
      mocks.projects = [
        { id: "proj_alpha", name: "Renamed" },
        { id: "proj_beta", name: "Beta" },
      ];
      options.onSuccess({ id: "proj_alpha", name: "Renamed" });
    });
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/"]}>
            <Sidebar open={true} onClose={vi.fn()} />
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>,
    );

    expect(within(projectSection("Renamed")).getByText("No chats")).toBeInTheDocument();
  });
});

describe("delete project", () => {
  it("confirms then archives the project", () => {
    renderSidebar();

    const kebab = within(projectSection("Alpha")).getByTestId("project-actions");
    fireEvent.pointerDown(kebab, { button: 0 });
    fireEvent.click(screen.getByTestId("delete-project"));

    expect(screen.getByText("Delete project?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete project" }));

    expect(mocks.del.mutate).toHaveBeenCalledWith("proj_alpha", expect.anything());
  });
});
