import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { PINNED_CONVERSATIONS_KEY } from "@/hooks/useConversations";
import type * as ConversationsModule from "@/hooks/useConversations";
import type * as HostsModule from "@/hooks/useHosts";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { resetReadStateForTests } from "@/hooks/useUnseenConversations";
import { ExtensionCatalogProvider } from "@/extensions/ExtensionProvider";
import * as identity from "@/lib/identity";
import { getSession } from "@/lib/sessionsApi";
import { clearOptimisticTitles } from "@/lib/optimisticTitles";
import { clearSessionDrafts } from "@/lib/sessionDrafts";
import { useChatStore } from "@/store/chatStore";
import { Sidebar } from "./Sidebar";
import { HeaderConversationMenu } from "./HeaderConversationMenu";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
vi.mock("@/hooks/useConversations", async (importOriginal) => {
  const actual = await importOriginal<typeof ConversationsModule>();
  const { conversationHooksMock, conversationPage } = await import("@/test/sidebarMockHelpers");
  return {
    ...actual,
    ...conversationHooksMock(),
    useConversations: () => conversationPage([]),
    usePinnedConversations: actual.usePinnedConversations,
    useTogglePinnedConversation: actual.useTogglePinnedConversation,
  };
});
vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof HostsModule>()),
  useHosts: () => ({
    data: [{ host_id: "host_detail", name: "Remote workstation", status: "online" }],
  }),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: () => ({ data: [] }) }));
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

beforeEach(() => {
  localStorage.clear();
  resetReadStateForTests();
  clearSessionDrafts();
  clearOptimisticTitles();
  useChatStore.setState({ conversationId: null, status: "idle", terminalPending: false });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("header-pinned detail-only session tooltip", () => {
  it.each(["running", "failed"] as const)(
    "shows cached agent, location and %s status before list reconciliation",
    async (status) => {
      const wire = {
        id: "conv_detail",
        agent_id: "ag_native",
        agent_name: "claude-native-ui",
        title: "Detail-only session",
        created_at: 100,
        status,
        harness: "claude-native",
        llm_model: "opus[1m]",
        reasoning_effort: "medium",
        host_id: "host_detail",
        runner_id: "runner_detail",
        host_online: true,
        runner_online: true,
        workspace: "/srv/remote/repo",
        git_branch: "feat/detail-pin",
        labels: { "omnigent.wrapper": "claude-code-native-ui" },
      };
      let resolvePatch!: (response: Response) => void;
      const patch = new Promise<Response>((resolve) => {
        resolvePatch = resolve;
      });
      const fetchSpy = vi.spyOn(identity, "authenticatedFetch").mockImplementation((url, init) => {
        if (init?.method === "PATCH") return patch;
        if (String(url).includes("/model-options")) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                models: [{ id: "opus[1m]", displayName: "Opus 5.5 (1M context)" }],
              }),
            ),
          );
        }
        return Promise.resolve(new Response(JSON.stringify(wire)));
      });
      const session = await getSession(wire.id);
      const client = new QueryClient({
        defaultOptions: {
          queries: { retry: false, staleTime: Infinity },
          mutations: { retry: false },
        },
      });
      client.setQueryData(["session", session.id], session);
      client.setQueryData(PINNED_CONVERSATIONS_KEY, { conversations: [], filterHonored: true });
      render(
        <QueryClientProvider client={client}>
          <SidebarDataProvider>
            <ExtensionCatalogProvider extensions={[]}>
              <TooltipProvider>
                <MemoryRouter initialEntries={[`/c/${session.id}`]}>
                  <Sidebar open onClose={vi.fn()} />
                  <HeaderConversationMenu
                    conversation={{
                      id: session.id,
                      object: "conversation",
                      title: session.title,
                      labels: {},
                      created_at: session.createdAt,
                      updated_at: session.createdAt,
                      permission_level: session.permissionLevel,
                    }}
                    currentProject={null}
                    canShare={false}
                    canFork={false}
                    onShare={vi.fn()}
                    onFork={vi.fn()}
                  />
                </MemoryRouter>
              </TooltipProvider>
            </ExtensionCatalogProvider>
          </SidebarDataProvider>
        </QueryClientProvider>,
      );
      expect(screen.queryByRole("link", { name: session.title! })).toBeNull();
      fireEvent.pointerDown(screen.getByRole("button", { name: "Conversation actions" }), {
        button: 0,
        ctrlKey: false,
      });
      fireEvent.click(screen.getByTestId("header-pin-conversation"));
      const row = await screen.findByRole("link", { name: session.title! });
      fireEvent.focus(row);
      const tooltip = await screen.findByTestId("session-tooltip-content");
      expect(within(tooltip).getAllByTestId("session-tooltip-location")[0]).toHaveTextContent(
        "Remote workstation",
      );
      const state = within(tooltip).getAllByTestId("session-tooltip-status")[0];
      expect(state).toHaveTextContent(status === "running" ? "Working" : "Error");
      expect(state).toHaveAttribute("data-state", status === "running" ? "working" : "error");
      await waitFor(() =>
        expect(within(tooltip).getAllByTestId("session-tooltip-agent")[0]).toHaveTextContent(
          /^Opus 5.5 1M Medium$/,
        ),
      );
      expect(within(tooltip).getAllByTestId("session-tooltip-cwd")[0]).toHaveTextContent(
        wire.workspace,
      );
      expect(within(tooltip).getAllByTestId("session-tooltip-branch")[0]).toHaveTextContent(
        wire.git_branch,
      );
      expect(client.getQueriesData({ queryKey: ["conversations"] })).toHaveLength(0);
      expect(client.getQueriesData({ queryKey: ["project-sessions"] })).toHaveLength(0);
      expect(fetchSpy.mock.calls.some(([url]) => String(url).startsWith("/v1/sessions?"))).toBe(
        false,
      );
      await act(async () =>
        resolvePatch(
          new Response(
            JSON.stringify({ ...wire, labels: { ...wire.labels, "omnigent.pinned": "123" } }),
          ),
        ),
      );
      client.clear();
    },
  );
});
