import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  fetchProjectWithEtag,
  mutateProjectWithIfMatch,
  projectMutationStatus,
  ProjectMutationError,
} from "@/hooks/useConversations";
import { invalidateProjectQueries } from "@/hooks/projectQueries";
import { authenticatedFetch } from "@/lib/identity";
import { cn } from "@/lib/utils";

// Hand-kept mirror of ProjectDefaultsBundle (omnigent/projects/defaults.py).
const DEFAULT_FIELDS = [
  "host_type",
  "repo_url",
  "default_branch",
  "host_id",
  "workspace",
  "harness",
  "model",
  "reasoning_effort",
] as const;

type DefaultField = (typeof DEFAULT_FIELDS)[number];
type DefaultsBundle = Partial<Record<DefaultField, string | null>>;
type FieldState = "inherit" | "clear" | "value";

interface FieldDraft {
  state: FieldState;
  value: string;
}

type DefaultsDraft = Record<DefaultField, FieldDraft>;

interface ProjectDetails {
  id: string;
  name: string;
  description: string | null;
  storage_key: string;
  defaults_json: DefaultsBundle;
  created_at: number;
  updated_at: number;
  archived_at: number | null;
  row_version: number;
}

const FIELD_LABELS: Record<Exclude<DefaultField, "host_type">, string> = {
  repo_url: "Repository URL",
  default_branch: "Default branch",
  host_id: "Host ID",
  workspace: "Workspace path",
  harness: "Harness",
  model: "Model",
  reasoning_effort: "Reasoning effort",
};

function draftFromBundle(bundle: DefaultsBundle): DefaultsDraft {
  return Object.fromEntries(
    DEFAULT_FIELDS.map((field) => {
      if (!Object.hasOwn(bundle, field)) return [field, { state: "inherit", value: "" }];
      const value = bundle[field];
      return [
        field,
        value === null ? { state: "clear", value: "" } : { state: "value", value: value ?? "" },
      ];
    }),
  ) as DefaultsDraft;
}

function bundleFromDraft(draft: DefaultsDraft): DefaultsBundle {
  const bundle: DefaultsBundle = {};
  for (const field of DEFAULT_FIELDS) {
    const current = draft[field];
    if (current.state === "inherit") continue;
    bundle[field] = current.state === "clear" ? null : current.value;
  }
  return bundle;
}

function stateLabel(state: FieldState): string {
  if (state === "inherit") return "State: inherit (absent)";
  if (state === "clear") return "State: explicit clear (null)";
  return "State: value";
}

function serverMessage(error: unknown): string {
  const fallback = "Couldn't save project settings. Try again.";
  if (error instanceof ProjectMutationError) return error.serverMessage || fallback;
  return error instanceof Error && error.message ? error.message : fallback;
}

function formatTimestamp(value: number): string {
  return new Date(value * 1_000).toLocaleString();
}

function TriStateTextField({
  field,
  draft,
  onChange,
  highlighted = false,
  hint,
}: {
  field: Exclude<DefaultField, "host_type">;
  draft: FieldDraft;
  onChange: (draft: FieldDraft) => void;
  highlighted?: boolean;
  hint?: string;
}) {
  const label = FIELD_LABELS[field];
  return (
    <div
      className={cn(
        "space-y-1.5 rounded-lg border border-border p-2.5",
        highlighted && "border-primary/40 bg-primary/5",
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label className="font-medium text-xs" htmlFor={`project-default-${field}`}>
          {label}
        </label>
        <div className="flex items-center gap-2 text-xs">
          <label className="flex items-center gap-1.5 text-muted-foreground">
            <input
              type="checkbox"
              aria-label={`Inherit ${label}`}
              checked={draft.state === "inherit"}
              onChange={(event) =>
                onChange({
                  state: event.target.checked ? "inherit" : "value",
                  value: draft.value,
                })
              }
            />
            Inherit
          </label>
          <Button
            type="button"
            variant="ghost"
            size="xs"
            aria-label={`Clear ${label}`}
            disabled={draft.state === "clear"}
            onClick={() => onChange({ state: "clear", value: "" })}
          >
            Clear
          </Button>
        </div>
      </div>
      <Input
        id={`project-default-${field}`}
        aria-label={`${label} value`}
        value={draft.value}
        disabled={draft.state === "inherit"}
        placeholder={draft.state === "clear" ? "Explicitly cleared" : undefined}
        onFocus={() => {
          if (draft.state === "clear") onChange({ ...draft, state: "value" });
        }}
        onChange={(event) => onChange({ state: "value", value: event.target.value })}
      />
      <div className="flex flex-wrap justify-between gap-1 text-muted-foreground text-xs">
        <span data-testid={`${field}-state`}>{stateLabel(draft.state)}</span>
        {hint && <span>{hint}</span>}
      </div>
    </div>
  );
}

export function ProjectSettingsDialog({
  projectId,
  open,
  onOpenChange,
}: {
  projectId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [project, setProject] = useState<ProjectDetails | null>(null);
  const [etag, setEtag] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [defaults, setDefaults] = useState<DefaultsDraft>(() => draftFromBundle({}));
  const [isLoading, setIsLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [nameError, setNameError] = useState<string | null>(null);
  const loadSequence = useRef(0);

  const loadProject = useCallback(
    async (preserveSaveError = false) => {
      const sequence = ++loadSequence.current;
      setIsLoading(true);
      setLoadError(null);
      if (!preserveSaveError) {
        setSaveError(null);
        setNameError(null);
      }
      try {
        const { response, etag: nextEtag } = await fetchProjectWithEtag(projectId);
        const nextProject = (await response.json()) as ProjectDetails;
        if (sequence !== loadSequence.current) return;
        setProject(nextProject);
        setEtag(nextEtag);
        setName(nextProject.name);
        setDescription(nextProject.description ?? "");
        setDefaults(draftFromBundle(nextProject.defaults_json ?? {}));
      } catch (error) {
        if (sequence !== loadSequence.current) return;
        setLoadError(error instanceof Error ? error.message : "Couldn't load project settings.");
      } finally {
        if (sequence === loadSequence.current) setIsLoading(false);
      }
    },
    [projectId],
  );

  useEffect(() => {
    if (open) void loadProject();
    return () => {
      loadSequence.current += 1;
    };
  }, [loadProject, open]);

  const hostType = defaults.host_type;
  const activeHostType = hostType.state === "value" ? hostType.value : null;
  const metadata = useMemo(
    () =>
      project
        ? [
            ["ID", project.id],
            ["Storage key", project.storage_key],
            ["Created", formatTimestamp(project.created_at)],
            ["Updated", formatTimestamp(project.updated_at)],
            ["Archived state", project.archived_at === null ? "Active" : "Archived"],
          ]
        : [],
    [project],
  );

  function updateDefault(field: DefaultField, draft: FieldDraft) {
    setDefaults((current) => ({ ...current, [field]: draft }));
    setSaveError(null);
  }

  async function handleSave() {
    if (!project || !etag || isSaving) return;
    const trimmedName = name.trim();
    if (!trimmedName) {
      setNameError("Project name is required.");
      return;
    }

    setIsSaving(true);
    setSaveError(null);
    setNameError(null);
    let phase: "update" | "rename" = "update";
    try {
      const updated = await mutateProjectWithIfMatch(
        projectId,
        (projectUrl, currentEtag) =>
          authenticatedFetch(projectUrl, {
            method: "PATCH",
            headers: {
              "Content-Type": "application/json",
              "If-Match": currentEtag,
            },
            body: JSON.stringify({
              description: description.trim() ? description : null,
              defaults_json: bundleFromDraft(defaults),
            }),
          }),
        etag,
      );
      const updatedProject = (await updated.json()) as ProjectDetails;
      const updatedEtag = updated.headers.get("ETag") ?? `"${updatedProject.row_version}"`;
      setProject(updatedProject);
      setEtag(updatedEtag);

      if (trimmedName !== project.name) {
        phase = "rename";
        const renamed = await mutateProjectWithIfMatch(
          projectId,
          (projectUrl, currentEtag) =>
            authenticatedFetch(`${projectUrl}/rename`, {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                "If-Match": currentEtag,
              },
              body: JSON.stringify({ name: trimmedName }),
            }),
          updatedEtag,
        );
        setProject((await renamed.json()) as ProjectDetails);
        setEtag(renamed.headers.get("ETag"));
      }

      invalidateProjectQueries(queryClient, { sessions: true });
      onOpenChange(false);
    } catch (error) {
      const status = projectMutationStatus(error);
      if (phase === "rename" && status === 409) {
        setNameError("A project with this name already exists.");
        invalidateProjectQueries(queryClient, { sessions: true });
      } else if (status === 412) {
        setSaveError("This project changed elsewhere. Latest settings have been loaded.");
        await loadProject(true);
      } else {
        setSaveError(serverMessage(error));
      }
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-h-[85vh] overflow-y-auto sm:max-w-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <DialogHeader>
          <DialogTitle>Project settings</DialogTitle>
          <DialogDescription>
            Edit the defaults inherited by future sessions in this project.
          </DialogDescription>
        </DialogHeader>

        {isLoading && !project ? <p className="text-muted-foreground">Loading…</p> : null}
        {loadError ? (
          <div className="space-y-2" role="alert">
            <p className="text-destructive">{loadError}</p>
            <Button type="button" variant="outline" size="sm" onClick={() => void loadProject()}>
              Retry
            </Button>
          </div>
        ) : null}

        {project ? (
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              void handleSave();
            }}
          >
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <label className="font-medium text-xs" htmlFor="project-settings-name">
                  Name
                </label>
                <Input
                  id="project-settings-name"
                  value={name}
                  aria-invalid={nameError ? true : undefined}
                  aria-describedby={nameError ? "project-settings-name-error" : undefined}
                  onChange={(event) => {
                    setName(event.target.value);
                    setNameError(null);
                  }}
                />
                {nameError ? (
                  <p
                    id="project-settings-name-error"
                    className="text-destructive text-xs"
                    role="alert"
                  >
                    {nameError}
                  </p>
                ) : null}
              </div>
              <div className="space-y-1.5 sm:col-span-2">
                <label className="font-medium text-xs" htmlFor="project-settings-description">
                  Description
                </label>
                <Textarea
                  id="project-settings-description"
                  value={description}
                  onChange={(event) => {
                    setDescription(event.target.value);
                    setSaveError(null);
                  }}
                />
              </div>
            </div>

            <section className="space-y-3" aria-labelledby="project-defaults-heading">
              <div>
                <h3 id="project-defaults-heading" className="font-medium">
                  Session defaults
                </h3>
                <p className="text-muted-foreground text-xs">
                  Inherit omits a field; Clear sends an explicit null; Value overrides it.
                </p>
              </div>

              <div className="space-y-1.5 rounded-lg border border-border p-2.5">
                <label className="font-medium text-xs" htmlFor="project-default-host-type">
                  Host type
                </label>
                <select
                  id="project-default-host-type"
                  className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm"
                  value={
                    hostType.state === "inherit"
                      ? "inherit"
                      : hostType.state === "clear"
                        ? "clear"
                        : hostType.value
                  }
                  onChange={(event) => {
                    const value = event.target.value;
                    if (value === "inherit") {
                      updateDefault("host_type", { state: "inherit", value: hostType.value });
                    } else if (value === "clear") {
                      updateDefault("host_type", { state: "clear", value: "" });
                    } else {
                      updateDefault("host_type", { state: "value", value });
                    }
                  }}
                >
                  <option value="inherit">Inherit (absent)</option>
                  <option value="clear">Explicit clear (null)</option>
                  <option value="managed">Managed</option>
                  <option value="external">External</option>
                </select>
                <p className="text-muted-foreground text-xs" data-testid="host_type-state">
                  {stateLabel(hostType.state)}
                </p>
              </div>

              {activeHostType === "managed" ? (
                <p className="rounded-lg bg-muted p-2.5 text-xs">
                  Managed hosts clone the repository and branch below. Host ID is not applicable.
                </p>
              ) : null}
              {activeHostType === "external" ? (
                <p className="rounded-lg bg-muted p-2.5 text-xs">
                  External hosts use the workspace path and optional pinned host ID below.
                </p>
              ) : null}

              <div className="grid gap-2 sm:grid-cols-2">
                <TriStateTextField
                  field="repo_url"
                  draft={defaults.repo_url}
                  highlighted={activeHostType === "managed"}
                  onChange={(draft) => updateDefault("repo_url", draft)}
                />
                <TriStateTextField
                  field="default_branch"
                  draft={defaults.default_branch}
                  highlighted={activeHostType === "managed"}
                  onChange={(draft) => updateDefault("default_branch", draft)}
                />
                <TriStateTextField
                  field="host_id"
                  draft={defaults.host_id}
                  highlighted={activeHostType === "external"}
                  hint={
                    activeHostType === "managed" ? "Not applicable for managed hosts" : undefined
                  }
                  onChange={(draft) => updateDefault("host_id", draft)}
                />
                <TriStateTextField
                  field="workspace"
                  draft={defaults.workspace}
                  highlighted={activeHostType === "external"}
                  onChange={(draft) => updateDefault("workspace", draft)}
                />
                <TriStateTextField
                  field="harness"
                  draft={defaults.harness}
                  onChange={(draft) => updateDefault("harness", draft)}
                />
                <TriStateTextField
                  field="model"
                  draft={defaults.model}
                  onChange={(draft) => updateDefault("model", draft)}
                />
                <TriStateTextField
                  field="reasoning_effort"
                  draft={defaults.reasoning_effort}
                  onChange={(draft) => updateDefault("reasoning_effort", draft)}
                />
              </div>
            </section>

            <section className="space-y-2" aria-labelledby="project-metadata-heading">
              <h3 id="project-metadata-heading" className="font-medium">
                Metadata
              </h3>
              <dl className="grid gap-x-4 gap-y-1 rounded-lg bg-muted/50 p-3 text-xs sm:grid-cols-[auto_1fr]">
                {metadata.map(([label, value]) => (
                  <div className="contents" key={label}>
                    <dt className="text-muted-foreground">{label}</dt>
                    <dd className="break-all font-mono">{value}</dd>
                  </div>
                ))}
              </dl>
            </section>

            {saveError ? (
              <p className="text-destructive text-sm" role="alert">
                {saveError}
              </p>
            ) : null}

            <DialogFooter>
              <Button
                type="button"
                variant="ghost"
                disabled={isSaving}
                onClick={() => onOpenChange(false)}
              >
                Cancel
              </Button>
              <Button type="submit" loading={isSaving}>
                Save settings
              </Button>
            </DialogFooter>
          </form>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
