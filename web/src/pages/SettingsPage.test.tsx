// Tests for the Settings content panel. The section nav lives in the sidebar
// card (see settingsNav); the page renders only the section named by the URL.
// Covers the Appearance theme picker, the auth-gated Account section, and the
// Archived sessions list (which moved here out of the sidebar).

import { type ReactNode } from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";

const mocks = vi.hoisted(() => ({
  setTheme: vi.fn(),
  theme: "system" as string,
  archiveMutate: vi.fn(),
  deleteMutate: vi.fn(),
  accountsEnabled: true,
  // login_url: non-null for any sign-in mode (accounts OR OIDC), null in
  // header single-user mode. Gates the Account section.
  loginUrl: "/login" as string | null,
  // Identity from the mode-agnostic `/v1/me` probe (resolveIdentity returns
  // the id, getCurrentIsAdmin the flag). null → unauthenticated.
  me: { id: "alice", is_admin: false } as { id: string; is_admin: boolean } | null,
  conversations: [] as Conversation[],
  // Picker options come from useArchivedProjectNames (a dedicated scan), not
  // from the loaded rows — so tests set them independently of `conversations`.
  projectNames: [] as string[],
  hasNextPage: false,
  fetchNextPage: vi.fn(),
}));

vi.mock("next-themes", () => ({
  useTheme: () => ({ theme: mocks.theme, systemTheme: "light", setTheme: mocks.setTheme }),
}));
vi.mock("@/lib/embedded", () => ({ useIsEmbedded: () => false }));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({
    accounts_enabled: mocks.accountsEnabled,
    login_url: mocks.loginUrl,
  }),
}));
vi.mock("@/lib/accountsApi", () => ({
  logout: vi.fn(),
  changePassword: vi.fn(),
}));
vi.mock("@/lib/identity", () => ({
  resolveIdentity: () => Promise.resolve(mocks.me?.id ?? null),
  getCurrentIsAdmin: () => mocks.me?.is_admin ?? false,
}));
vi.mock("@/hooks/useConversations", () => ({
  PROJECT_LABEL_KEY: "omni_project",
  // The Archived view drives the visible list from this hook; filter on the
  // fourth (`project`) arg so the mock mirrors the server-side ?project=
  // scoping. Pagination fields back the "Load more" control.
  useConversations: (
    _searchQuery?: string,
    _includeArchived?: boolean,
    _options?: unknown,
    project?: string,
  ) => ({
    data: {
      pages: [
        {
          data: project
            ? mocks.conversations.filter((c) => c.labels?.["omni_project"] === project)
            : mocks.conversations,
        },
      ],
    },
    isLoading: false,
    hasNextPage: mocks.hasNextPage,
    isFetchingNextPage: false,
    fetchNextPage: mocks.fetchNextPage,
  }),
  // Picker options are sourced from this dedicated scan, decoupled from the
  // loaded rows so archived-only projects on later pages still appear.
  useArchivedProjectNames: () => ({ data: mocks.projectNames }),
  useArchiveConversation: () => ({ mutate: mocks.archiveMutate, isPending: false }),
  useStopAndDeleteConversation: () => ({ mutate: mocks.deleteMutate, isPending: false }),
}));
// Radix Select uses a portal + pointer events jsdom can't drive; stub it to a
// native <select> so tests can switch the archived project filter.
vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    children,
  }: {
    value: string;
    onValueChange: (v: string) => void;
    children: ReactNode;
  }) => (
    <select
      data-testid="archived-project-filter"
      value={value}
      onChange={(e) => onValueChange(e.target.value)}
    >
      {children}
    </select>
  ),
  SelectTrigger: ({ children }: { children: ReactNode }) => <>{children}</>,
  SelectValue: () => null,
  SelectContent: ({ children }: { children: ReactNode }) => <>{children}</>,
  SelectItem: ({ value, children }: { value: string; children: ReactNode }) => (
    <option value={value}>{children}</option>
  ),
}));
// The admin management surfaces are lazy-loaded and own heavy data layers of
// their own; stub them so these tests only assert SettingsPage's section
// routing (that /settings/members and /settings/policies render the right one).
vi.mock("@/pages/MembersPage", () => ({
  MembersPage: () => <div>members-page-stub</div>,
}));
vi.mock("@/pages/PoliciesPage", () => ({
  PoliciesPage: () => <div>policies-page-stub</div>,
}));

import { SettingsPage } from "./SettingsPage";

function conv(id: string, partial: Partial<Conversation> = {}): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    ...partial,
  };
}

function renderPage(path = "/settings") {
  return render(
    <TooltipProvider>
      <MemoryRouter initialEntries={[path]}>
        <SettingsPage />
      </MemoryRouter>
    </TooltipProvider>,
  );
}

beforeEach(() => {
  mocks.setTheme.mockReset();
  mocks.archiveMutate.mockReset();
  mocks.deleteMutate.mockReset();
  mocks.fetchNextPage.mockReset();
  mocks.theme = "system";
  mocks.accountsEnabled = true;
  mocks.loginUrl = "/login";
  mocks.me = { id: "alice", is_admin: false };
  mocks.conversations = [];
  mocks.projectNames = [];
  mocks.hasNextPage = false;
});
afterEach(() => {
  cleanup();
  // Reset the font-size preference + applied scale so the Appearance tests
  // don't leak persisted state or the --ui-font-scale variable into each other.
  localStorage.clear();
  document.documentElement.style.removeProperty("--ui-font-scale");
});

describe("SettingsPage", () => {
  it("renders the Appearance section and applies a theme on card click", () => {
    renderPage("/settings/appearance");
    expect(screen.getByRole("heading", { name: "Appearance" })).toBeInTheDocument();
    // System is selected (theme = "system").
    expect(screen.getByTestId("theme-system")).toHaveAttribute("aria-checked", "true");
    fireEvent.click(screen.getByTestId("theme-dark"));
    expect(mocks.setTheme).toHaveBeenCalledWith("dark");
  });

  it("shows the default UI font size and steps it up, persisting the choice", () => {
    localStorage.clear();
    renderPage("/settings/appearance");
    const input = screen.getByTestId("ui-font-size-input") as HTMLInputElement;
    // No stored preference → 16px default.
    expect(input.value).toBe("16");

    fireEvent.click(screen.getByTestId("ui-font-size-inc"));
    expect(input.value).toBe("17");
    // The choice is persisted so it survives a refresh.
    expect(localStorage.getItem("omnigent:ui-font-size")).toBe("17");
    // The scale is applied live to the document root (17 / 16).
    expect(document.documentElement.style.getPropertyValue("--ui-font-scale")).toBe("1.0625");
  });

  it("disables the steppers at the min and max bounds", () => {
    localStorage.setItem("omnigent:ui-font-size", "20");
    renderPage("/settings/appearance");
    // At the 20px max, only the increase button is disabled.
    expect(screen.getByTestId("ui-font-size-inc")).toBeDisabled();
    expect(screen.getByTestId("ui-font-size-dec")).not.toBeDisabled();

    cleanup();
    localStorage.setItem("omnigent:ui-font-size", "12");
    renderPage("/settings/appearance");
    // At the 12px min, only the decrease button is disabled.
    expect(screen.getByTestId("ui-font-size-dec")).toBeDisabled();
    expect(screen.getByTestId("ui-font-size-inc")).not.toBeDisabled();
  });

  it("shows the empty font family default and applies + persists a typed name", () => {
    localStorage.clear();
    document.documentElement.style.removeProperty("--ui-font-family");
    renderPage("/settings/appearance");
    const input = screen.getByTestId("ui-font-family-input") as HTMLInputElement;
    // No stored preference → empty input, System-default placeholder, no override.
    expect(input.value).toBe("");
    expect(input.placeholder).toBe("System default");
    expect(document.documentElement.style.getPropertyValue("--ui-font-family")).toBe("");
    // Reset has nothing to do at the default.
    expect(screen.getByTestId("ui-font-family-reset")).toBeDisabled();

    fireEvent.change(input, { target: { value: "Inter" } });
    expect(input.value).toBe("Inter");
    // The choice is persisted so it survives a refresh...
    expect(localStorage.getItem("omnigent:ui-font-family")).toBe(JSON.stringify("Inter"));
    // ...and applied live to the document root, with the system stack appended
    // so an uninstalled/partial name degrades to the default sans, not serif.
    expect(document.documentElement.style.getPropertyValue("--ui-font-family")).toBe(
      "Inter, var(--font-sans)",
    );
    expect(screen.getByTestId("ui-font-family-reset")).not.toBeDisabled();
  });

  it("reset restores the system default font family", () => {
    localStorage.setItem("omnigent:ui-font-family", JSON.stringify("Georgia"));
    renderPage("/settings/appearance");
    const input = screen.getByTestId("ui-font-family-input") as HTMLInputElement;
    // The control reflects the stored preference on mount.
    expect(input.value).toBe("Georgia");

    fireEvent.click(screen.getByTestId("ui-font-family-reset"));
    // Reset clears the field, the applied property, and the stored key.
    expect(input.value).toBe("");
    expect(document.documentElement.style.getPropertyValue("--ui-font-family")).toBe("");
    expect(localStorage.getItem("omnigent:ui-font-family")).toBeNull();
  });

  it("lets you clear and retype the font size without clamping mid-edit", () => {
    localStorage.setItem("omnigent:ui-font-size", "13");
    renderPage("/settings/appearance");
    const input = screen.getByTestId("ui-font-size-input") as HTMLInputElement;
    expect(input.value).toBe("13");

    // Deleting a digit leaves "1" — below the 12px min. The box must SHOW "1"
    // (free editing) without snapping to 12 or persisting the transient value.
    fireEvent.change(input, { target: { value: "1" } });
    expect(input.value).toBe("1");
    expect(localStorage.getItem("omnigent:ui-font-size")).toBe("13");
    expect(document.documentElement.style.getPropertyValue("--ui-font-scale")).toBe("");

    // Finishing the number to a valid size applies it live and persists it.
    fireEvent.change(input, { target: { value: "18" } });
    expect(input.value).toBe("18");
    expect(localStorage.getItem("omnigent:ui-font-size")).toBe("18");
    // 18 / 16 base = 1.125.
    expect(document.documentElement.style.getPropertyValue("--ui-font-scale")).toBe("1.125");
  });

  it("clamps a below-min entry to the minimum on blur", () => {
    localStorage.setItem("omnigent:ui-font-size", "16");
    renderPage("/settings/appearance");
    const input = screen.getByTestId("ui-font-size-input") as HTMLInputElement;

    fireEvent.change(input, { target: { value: "1" } });
    fireEvent.blur(input);
    // On blur the draft settles to the clamped minimum.
    expect(input.value).toBe("12");
    expect(localStorage.getItem("omnigent:ui-font-size")).toBe("12");
  });

  it("reverts an empty entry to the committed size on blur", () => {
    localStorage.setItem("omnigent:ui-font-size", "15");
    renderPage("/settings/appearance");
    const input = screen.getByTestId("ui-font-size-input") as HTMLInputElement;

    fireEvent.change(input, { target: { value: "" } });
    expect(input.value).toBe("");
    fireEvent.blur(input);
    // An empty field restores the last committed value rather than a bogus one.
    expect(input.value).toBe("15");
    expect(localStorage.getItem("omnigent:ui-font-size")).toBe("15");
  });

  it("defaults bare /settings to Account when a login session exists, else Appearance", async () => {
    // Login session (accounts OR OIDC) → Account leads, so /settings lands on it.
    renderPage("/settings");
    await waitFor(() => expect(screen.getByText("alice")).toBeInTheDocument());

    // Header single-user (no login_url) → no Account section; falls back to
    // Appearance.
    cleanup();
    mocks.accountsEnabled = false;
    mocks.loginUrl = null;
    renderPage("/settings");
    expect(screen.getByRole("heading", { name: "Appearance" })).toBeInTheDocument();
  });

  it("renders the Account section at /settings/account for any login session", async () => {
    renderPage("/settings/account");
    await waitFor(() => expect(screen.getByText("alice")).toBeInTheDocument());

    // Header single-user (no login_url) → the section renders nothing even at
    // its URL.
    cleanup();
    mocks.accountsEnabled = false;
    mocks.loginUrl = null;
    renderPage("/settings/account");
    expect(screen.queryByText("alice")).toBeNull();
  });

  it("renders the Account section under OIDC (accounts off, login_url set)", async () => {
    // #1489: an SSO user must be able to see their identity and sign out.
    mocks.accountsEnabled = false;
    mocks.loginUrl = "/auth/login";
    renderPage("/settings/account");
    await waitFor(() => expect(screen.getByText("alice")).toBeInTheDocument());
    // Change password is accounts-only — hidden under OIDC.
    expect(screen.queryByRole("button", { name: /Change password/ })).toBeNull();
    // Sign out is still available.
    expect(screen.getByRole("button", { name: /Sign out/ })).toBeInTheDocument();
  });

  it("renders the Members section at /settings/members when accounts is on", async () => {
    renderPage("/settings/members");
    expect(await screen.findByText("members-page-stub")).toBeInTheDocument();
    expect(screen.queryByText("policies-page-stub")).toBeNull();
  });

  it("renders the Policies section at /settings/policies when accounts is on", async () => {
    renderPage("/settings/policies");
    expect(await screen.findByText("policies-page-stub")).toBeInTheDocument();
    expect(screen.queryByText("members-page-stub")).toBeNull();
  });

  it("still renders the admin sections when accounts is off (OIDC)", async () => {
    // #1489: Members / Policies are admin surfaces valid under OIDC too. The
    // page itself self-gates to admins (and runs read-only under OIDC); the
    // SettingsPage no longer withholds the section based on accounts_enabled.
    mocks.accountsEnabled = false;
    renderPage("/settings/members");
    expect(await screen.findByText("members-page-stub")).toBeInTheDocument();
  });

  it("no longer links to Members / Policies from the Account section", async () => {
    // They moved to the sidebar nav (Admin group); the Account section — even
    // for an admin — must not re-link to them, or we'd be back to navigating
    // away from /settings.
    mocks.me = { id: "alice", is_admin: true };
    renderPage("/settings/account");
    await waitFor(() => expect(screen.getByText("alice")).toBeInTheDocument());
    expect(screen.queryByRole("link", { name: /Members/ })).toBeNull();
    expect(screen.queryByRole("link", { name: /Policies/ })).toBeNull();
  });

  it("lists archived sessions and unarchives on click", () => {
    mocks.conversations = [
      conv("conv_active"),
      conv("conv_archived", { archived: true, title: "Old chat" }),
    ];
    renderPage("/settings/archived");

    const rows = screen.getAllByTestId("archived-row");
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText("Old chat")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("unarchive-conversation"));
    expect(mocks.archiveMutate).toHaveBeenCalledWith({ id: "conv_archived", archived: false });
  });

  it("deletes an archived session after confirming, with no row-click navigation", () => {
    mocks.conversations = [conv("conv_archived", { archived: true, title: "Old chat" })];
    renderPage("/settings/archived");

    // The row text isn't a link/button target — there's nothing to click into.
    expect(screen.queryByRole("link", { name: /Old chat/ })).toBeNull();

    // Trash → confirm dialog → Delete fires the delete mutation.
    fireEvent.click(screen.getByTestId("delete-archived"));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(mocks.deleteMutate).toHaveBeenCalledWith({ id: "conv_archived" });
  });

  it("scopes the archived list to the project picked in the filter", () => {
    mocks.projectNames = ["Alpha", "Beta"];
    mocks.conversations = [
      conv("conv_a", { archived: true, title: "Alpha chat", labels: { omni_project: "Alpha" } }),
      conv("conv_b", { archived: true, title: "Beta chat", labels: { omni_project: "Beta" } }),
      conv("conv_active"),
    ];
    renderPage("/settings/archived");

    // "All projects" (default) lists every archived session.
    expect(screen.getAllByTestId("archived-row")).toHaveLength(2);
    const select = screen.getByTestId("archived-project-filter");
    expect(within(select).getByRole("option", { name: "All projects" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "Alpha" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "Beta" })).toBeInTheDocument();

    // Picking a project narrows the list to that project's archived sessions.
    // Select values are discriminated (`project:<name>`), never the raw name.
    fireEvent.change(select, { target: { value: "project:Alpha" } });
    const rows = screen.getAllByTestId("archived-row");
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText("Alpha chat")).toBeInTheDocument();

    // Back to "All projects" restores the full list.
    fireEvent.change(select, { target: { value: "all" } });
    expect(screen.getAllByTestId("archived-row")).toHaveLength(2);
  });

  it("hides the project filter when no archived session belongs to a project", () => {
    mocks.conversations = [conv("conv_archived", { archived: true, title: "Old chat" })];
    renderPage("/settings/archived");

    expect(screen.queryByTestId("archived-project-filter")).toBeNull();
    expect(screen.getByTestId("archived-row")).toBeInTheDocument();
  });

  it("shows the empty state (and no filter) when there are no archived sessions", () => {
    mocks.conversations = [conv("conv_active")];
    renderPage("/settings/archived");

    expect(screen.getByText("No archived sessions.")).toBeInTheDocument();
    expect(screen.queryByTestId("archived-project-filter")).toBeNull();
  });

  it("shows a project-scoped empty state when the picked project has no rows", () => {
    mocks.projectNames = ["Alpha"];
    mocks.conversations = [
      conv("conv_a", { archived: true, title: "Alpha chat", labels: { omni_project: "Alpha" } }),
    ];
    renderPage("/settings/archived");

    const select = screen.getByTestId("archived-project-filter");
    // Drop Alpha's only session so the filtered fetch returns nothing, then
    // pick Alpha (still an option because it's in the scanned name set).
    mocks.conversations = [];
    fireEvent.change(select, { target: { value: "project:Alpha" } });
    expect(screen.getByText("No archived sessions in this project.")).toBeInTheDocument();
  });

  it("offers archived-only projects whose sessions are beyond the first loaded page", () => {
    // The visible list's first page has no Gamma row, but the option scan
    // (useArchivedProjectNames, which pages through everything) found Gamma —
    // this is the gotcha the feature exists for.
    mocks.projectNames = ["Gamma"];
    mocks.conversations = [conv("p1", { archived: true, title: "Page-one chat" })];
    renderPage("/settings/archived");

    const select = screen.getByTestId("archived-project-filter");
    // Gamma is offered even though no Gamma row is in the loaded page.
    expect(within(select).getByRole("option", { name: "Gamma" })).toBeInTheDocument();
  });

  it("treats a project literally named __all__ as a real project, not the clear-filter sentinel", () => {
    mocks.projectNames = ["Other", "__all__"];
    mocks.conversations = [
      conv("x1", { archived: true, title: "Edge chat", labels: { omni_project: "__all__" } }),
      conv("o1", { archived: true, title: "Other chat", labels: { omni_project: "Other" } }),
    ];
    renderPage("/settings/archived");

    const select = screen.getByTestId("archived-project-filter");
    // Picking the "__all__" project must FILTER to it (discriminated value
    // `project:__all__`), not clear the filter.
    fireEvent.change(select, { target: { value: "project:__all__" } });
    const rows = screen.getAllByTestId("archived-row");
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText("Edge chat")).toBeInTheDocument();
  });

  it("loads the next page of archived sessions on demand", () => {
    mocks.conversations = [conv("a1", { archived: true, title: "Old chat" })];
    mocks.hasNextPage = true;
    renderPage("/settings/archived");

    fireEvent.click(screen.getByTestId("archived-load-more"));
    expect(mocks.fetchNextPage).toHaveBeenCalled();
  });
});
