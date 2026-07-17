// Layout regression tests for the project-folder header's icon/chevron.
// Desired behaviour: a project folder shows its folder icon (open when
// expanded, closed when collapsed) and NO chevron — the folder icon itself is
// the expand/collapse cue, so clicking the folder toggles it. Plain section
// headers with no leading icon keep a trailing chevron revealed on desktop
// hover/focus (always shown on mobile). These tests lock that structure in:
//   1. A project header renders the folder icon and no chevron.
//   2. A header without a leading icon (the "Projects" group header) keeps a
//      hover-revealed trailing chevron and renders no folder icon.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({
    mutate: vi.fn(),
    reset: vi.fn(),
    isPending: false,
    isError: false,
  }),
  usePinnedConversationBackfill: () => [],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useStopSession: () => ({ mutate: vi.fn() }),
  // One project so a folder header renders. Empty projects are not filtered
  // out, so no conversations are needed to exercise the header layout.
  useProjects: () => ({ data: [{ id: "My Project", name: "My Project" }] }),
  useProjectSessions: () => ({
    data: undefined,
    isLoading: false,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
  }),
  useMoveToProject: () => ({ mutate: vi.fn() }),
  useDeleteProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useRenameProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  fetchProjectSessionIds: () => Promise.resolve([]),
  PROJECT_LABEL_KEY: "omni_project",
}));

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

function mockConversations(conversations: Conversation[]) {
  const withData = {
    data: {
      pages: [
        {
          data: conversations,
          first_id: conversations[0]?.id ?? null,
          last_id: conversations.at(-1)?.id ?? null,
          has_more: false,
        },
      ],
      pageParams: [undefined],
    },
    isLoading: false,
    isError: false,
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  } as unknown as ReturnType<typeof useConversations>;
  useConvMock.mockImplementation(() => withData);
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

/** The <button> header for a section/folder, found by its accessible name. */
function headerButton(name: string): HTMLElement {
  return screen.getByRole("button", { name });
}

/** SVG elements expose `className` as an SVGAnimatedString, not a string;
 *  read the raw class attribute instead. */
function classOf(el: Element): string {
  return el.getAttribute("class") ?? "";
}

beforeEach(() => {
  mockConversations([]);
});

afterEach(() => {
  cleanup();
});

describe("project folder header icon/chevron", () => {
  it("shows the folder icon and no chevron on a project folder header", () => {
    renderSidebar();
    const header = headerButton("My Project");

    // The folder icon is present (open/closed folder conveys expand state).
    expect(header.querySelector(".lucide-folder")).not.toBeNull();

    // No chevron on a project folder — clicking the folder toggles it.
    expect(header.querySelectorAll(".lucide-chevron-right")).toHaveLength(0);
  });

  it("leaves iconless section headers with a hover-revealed trailing chevron and no folder icon", () => {
    renderSidebar();
    // The "Projects" group header carries no leading icon.
    const header = headerButton("Projects");

    expect(header.querySelector(".lucide-folder")).toBeNull();

    const chevrons = Array.from(header.querySelectorAll(".lucide-chevron-right"));
    // Exactly one chevron: the classic desktop-hover-revealed trailing caret.
    expect(chevrons).toHaveLength(1);
    const [chevron] = chevrons;
    expect(classOf(chevron)).not.toMatch(/\babsolute\b/);
    expect(classOf(chevron)).not.toMatch(/md:hidden/);
    expect(classOf(chevron)).toMatch(/md:group-hover:opacity-100/);
  });
});
