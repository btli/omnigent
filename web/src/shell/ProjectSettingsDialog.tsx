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
import {
  invalidateProjectQueries,
  PROJECT_RESOLVED_DEFAULTS_KEY,
} from "@/hooks/projectQueries";
import { authenticatedFetch } from "@/lib/identity";

import { ProjectDefaultsEditor } from "./project-settings/ProjectDefaultsEditor";
import {
  serializeBundle,
  type DefaultsBundle,
  type DefaultsDraftState,
} from "./project-settings/projectDefaultsDraft";

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

function serverMessage(error: unknown): string {
  const fallback = "Couldn't save project settings. Try again.";
  if (error instanceof ProjectMutationError) return error.serverMessage || fallback;
  return error instanceof Error && error.message ? error.message : fallback;
}

function formatTimestamp(value: number): string {
  return new Date(value * 1_000).toLocaleString();
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
  const [defaultsDraft, setDefaultsDraft] = useState<DefaultsDraftState | null>(null);
  const [defaultsValid, setDefaultsValid] = useState(false);
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
        setDefaultsDraft(null);
        setDefaultsValid(false);
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
    if (open) {
      void loadProject();
    } else {
      // Wipe editable state on close so cancelled edits (description, draft,
      // stale ETag) can never survive into the next open and get saved there.
      setProject(null);
      setEtag(null);
      setName("");
      setDescription("");
      setDefaultsDraft(null);
      setDefaultsValid(false);
      setLoadError(null);
      setSaveError(null);
      setNameError(null);
    }
    return () => {
      loadSequence.current += 1;
    };
  }, [loadProject, open]);

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

  const handleDraftChange = useCallback((state: DefaultsDraftState) => {
    setDefaultsDraft(state);
    setSaveError(null);
  }, []);

  const handleValidityChange = useCallback((isValid: boolean) => {
    setDefaultsValid(isValid);
  }, []);

  async function handleSave() {
    if (!project || !etag || !defaultsDraft || !defaultsValid || isSaving || isLoading) return;
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
              defaults_json: serializeBundle(defaultsDraft),
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
        // Refresh the resolved baseline alongside the project: an errored
        // preview holds no data, so the editor's row-version mismatch effect
        // alone cannot recover it.
        void queryClient.invalidateQueries({
          queryKey: [...PROJECT_RESOLVED_DEFAULTS_KEY, projectId],
        });
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
        className="max-h-[85vh] overflow-hidden sm:max-w-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <DialogHeader className="shrink-0">
          <DialogTitle>Project settings</DialogTitle>
          <DialogDescription>
            Edit the defaults inherited by future sessions in this project.
          </DialogDescription>
        </DialogHeader>

        {isLoading && !project ? <p className="text-muted-foreground">Loading…</p> : null}
        {loadError ? (
          <div className="space-y-2" role="alert">
            <p className="text-destructive">{loadError}</p>
            <Button
              type="button"
              variant="outline"
              className="min-h-11"
              onClick={() => void loadProject()}
            >
              Retry
            </Button>
          </div>
        ) : null}

        {project ? (
          <form
            className="flex min-h-0 flex-1 flex-col gap-4 overflow-hidden"
            onSubmit={(event) => {
              event.preventDefault();
              void handleSave();
            }}
          >
            <div
              data-testid="project-settings-scroll-body"
              className="min-h-0 flex-1 space-y-4 overflow-y-auto pr-1"
            >
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <label className="text-xs font-medium" htmlFor="project-settings-name">
                    Name
                  </label>
                  <Input
                    id="project-settings-name"
                    value={name}
                    aria-invalid={nameError ? true : undefined}
                    aria-describedby={nameError ? "project-settings-name-error" : undefined}
                    className="h-11"
                    onChange={(event) => {
                      setName(event.target.value);
                      setNameError(null);
                    }}
                  />
                  {nameError ? (
                    <p
                      id="project-settings-name-error"
                      className="text-xs text-destructive"
                      role="alert"
                    >
                      {nameError}
                    </p>
                  ) : null}
                </div>
                <div className="space-y-1.5 sm:col-span-2">
                  <label className="text-xs font-medium" htmlFor="project-settings-description">
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
                  <p className="text-xs text-muted-foreground">
                    Resolved values stay visible. Editing creates an override; Reset restores
                    inheritance.
                  </p>
                </div>

                <ProjectDefaultsEditor
                  open={open}
                  projectId={projectId}
                  bundle={project.defaults_json ?? {}}
                  projectRowVersion={project.row_version}
                  onDraftChange={handleDraftChange}
                  onValidityChange={handleValidityChange}
                />
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
                <p className="text-sm text-destructive" role="alert">
                  {saveError}
                </p>
              ) : null}
            </div>

            <DialogFooter className="shrink-0 border-t border-border pt-3">
              <Button
                type="button"
                variant="ghost"
                className="min-h-11"
                disabled={isSaving}
                onClick={() => onOpenChange(false)}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                className="min-h-11"
                loading={isSaving}
                disabled={!defaultsDraft || !defaultsValid || isLoading}
              >
                Save settings
              </Button>
            </DialogFooter>
          </form>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
