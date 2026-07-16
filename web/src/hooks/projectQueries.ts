import type { QueryClient } from "@tanstack/react-query";
import { removeIdsFromPages, type ConversationsInfiniteData } from "@/lib/sessionListCache";

export const PROJECTS_KEY = ["projects"] as const;
export const PROJECT_SESSIONS_KEY = ["project-sessions"] as const;
export const PROJECT_NEWEST_KEY = ["project-newest-session"] as const;
export const ARCHIVED_PROJECTS_KEY = ["archived-projects"] as const;

/**
 * Refresh project identities and session-derived project state.
 *
 * Cache-splice callers pass `sessions: false`: they have already removed the
 * affected rows locally and must not race the session index with a refetch.
 */
export function invalidateProjectQueries(
  queryClient: QueryClient,
  { sessions }: { sessions: boolean },
): void {
  void queryClient.invalidateQueries({ queryKey: PROJECTS_KEY });
  if (sessions) void queryClient.invalidateQueries({ queryKey: PROJECT_SESSIONS_KEY });
  void queryClient.invalidateQueries({ queryKey: PROJECT_NEWEST_KEY });
  void queryClient.invalidateQueries({ queryKey: ARCHIVED_PROJECTS_KEY });
}

/** Remove sessions from the global and per-project paginated list caches. */
export function removeSessionsFromListCaches(
  queryClient: QueryClient,
  ids: Iterable<string>,
): boolean {
  const idSet = ids instanceof Set ? ids : new Set(ids);
  let removedAny = false;
  for (const queryKey of [["conversations"], PROJECT_SESSIONS_KEY] as const) {
    for (const [key, data] of queryClient.getQueriesData<ConversationsInfiniteData>({
      queryKey,
    })) {
      const { data: next, removed } = removeIdsFromPages(data, idSet);
      if (removed) {
        queryClient.setQueryData(key, next);
        removedAny = true;
      }
    }
  }
  return removedAny;
}
