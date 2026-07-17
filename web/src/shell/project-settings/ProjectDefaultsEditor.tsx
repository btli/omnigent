import { useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { useResolvedProjectDefaults } from "@/hooks/useConversations";
import { useHosts } from "@/hooks/useHosts";
import { useBrainHarnessLabels } from "@/lib/agentLabels";
import {
  effortOptionsForHarness,
  harnessOptionsForProject,
  modelOptionsForHarness,
} from "@/lib/harnessCatalog";

import { DefaultEffortPicker } from "./DefaultEffortPicker";
import { DefaultHarnessPicker } from "./DefaultHarnessPicker";
import { DefaultHostPicker } from "./DefaultHostPicker";
import { DefaultModelPicker } from "./DefaultModelPicker";
import { DefaultSelectField } from "./DefaultSelectField";
import { DefaultTextField } from "./DefaultTextField";
import {
  buildDraft,
  fieldDisplayValue,
  fieldProvenance,
  resetField,
  setFieldValue,
  type DefaultField,
  type DefaultsBundle,
  type DefaultsDraftState,
  type ResolvedBaselines,
} from "./projectDefaultsDraft";

const HOST_TYPE_OPTIONS = [
  { value: "external", label: "External" },
  { value: "managed", label: "Managed" },
] as const;

const FALLBACK_BASELINES: ResolvedBaselines = {
  host_type: "external",
  repo_url: null,
  default_branch: null,
  host_id: null,
  workspace: null,
  harness: null,
  model: null,
  reasoning_effort: null,
};

function stageManagedHostReset(state: DefaultsDraftState): DefaultsDraftState {
  if (fieldDisplayValue(state, "host_type") !== "managed") return state;
  if (fieldDisplayValue(state, "host_id").trim() === "") return state;
  return resetField(state, "host_id");
}

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message
    ? error.message
    : "Couldn't load resolved project defaults.";
}

export function ProjectDefaultsEditor({
  open,
  projectId,
  bundle,
  projectRowVersion,
  onDraftChange,
  onValidityChange,
}: {
  open: boolean;
  projectId: string;
  bundle: DefaultsBundle;
  projectRowVersion: number;
  onDraftChange: (state: DefaultsDraftState) => void;
  onValidityChange?: (isValid: boolean) => void;
}) {
  const preview = useResolvedProjectDefaults(open ? projectId : null);
  const labels = useBrainHarnessLabels();
  const [draft, setDraft] = useState<DefaultsDraftState | null>(null);
  const initializedKeyRef = useRef<string | null>(null);
  const lastMismatchRef = useRef<string | null>(null);
  const bundleKey = JSON.stringify(bundle);

  const matchingPreview =
    preview.data?.row_version === projectRowVersion ? preview.data : undefined;
  const baselines = useMemo<ResolvedBaselines | null>(() => {
    if (!matchingPreview) return preview.isError ? FALLBACK_BASELINES : null;
    return {
      host_type: matchingPreview.host_type,
      repo_url: null,
      default_branch:
        matchingPreview.host_type === "external"
          ? (matchingPreview.git?.base_branch ?? null)
          : null,
      host_id: matchingPreview.host_id,
      workspace: matchingPreview.workspace,
      harness: matchingPreview.harness_override,
      model: matchingPreview.model_override,
      reasoning_effort: matchingPreview.reasoning_effort,
    };
  }, [matchingPreview, preview.isError]);

  useEffect(() => {
    if (!open) {
      initializedKeyRef.current = null;
      setDraft(null);
      return;
    }
    if (!baselines) {
      initializedKeyRef.current = null;
      setDraft(null);
      return;
    }

    const baselineKey = JSON.stringify(baselines);
    const source = matchingPreview ? `matched:${matchingPreview.row_version}` : "error";
    const initializationKey = `${projectId}:${projectRowVersion}:${source}:${bundleKey}:${baselineKey}`;
    if (initializedKeyRef.current === initializationKey) return;

    // Rebasing onto new baselines for the same project row (e.g. fallback
    // baselines replaced by a recovered preview after Retry) must not discard
    // fields the user already touched — carry their edits into the rebuild.
    const isRebase = initializedKeyRef.current?.startsWith(`${projectId}:${projectRowVersion}:`);
    initializedKeyRef.current = initializationKey;
    setDraft((previous) => {
      const next = stageManagedHostReset(buildDraft(bundle, baselines));
      if (!previous || !isRebase) return next;
      const fields = { ...next.fields };
      for (const field of Object.keys(fields) as (keyof typeof fields)[]) {
        const touched = previous.fields[field];
        if (touched.edited || touched.resetRequested) {
          fields[field] = {
            ...fields[field],
            value: touched.value,
            edited: touched.edited,
            resetRequested: touched.resetRequested,
          };
        }
      }
      return { ...next, fields };
    });
  }, [baselines, bundle, bundleKey, matchingPreview, open, projectId, projectRowVersion]);

  useEffect(() => {
    if (!open || !preview.data || preview.data.row_version === projectRowVersion) return;
    const mismatchKey = `${projectId}:${projectRowVersion}:${preview.data.row_version}`;
    if (lastMismatchRef.current === mismatchKey) return;
    lastMismatchRef.current = mismatchKey;
    void preview.refetch();
  }, [open, preview, projectId, projectRowVersion]);

  const effectiveHostType = draft
    ? fieldDisplayValue(draft, "host_type") === "managed"
      ? "managed"
      : "external"
    : "external";
  // Also enabled while the preview errors: repairing a broken bundle (e.g. an
  // external default_branch without a pinned host) needs the host list.
  const hosts = useHosts({
    enabled: open && draft !== null && effectiveHostType === "external",
  });

  useEffect(() => {
    if (draft) onDraftChange(draft);
  }, [draft, onDraftChange]);

  const harness = draft ? fieldDisplayValue(draft, "harness").trim() || null : null;
  const model = draft ? fieldDisplayValue(draft, "model").trim() || null : null;
  const effort = draft ? fieldDisplayValue(draft, "reasoning_effort").trim() || null : null;
  const harnessAtOpen = draft
    ? typeof bundle.harness === "string"
      ? bundle.harness.trim()
      : (draft.fields.harness.resolvedAtOpen?.trim() ?? "")
    : "";
  const modelAtOpen = draft
    ? typeof bundle.model === "string"
      ? bundle.model.trim()
      : (draft.fields.model.resolvedAtOpen?.trim() ?? "")
    : "";
  const harnessChanged = draft !== null && (harness ?? "") !== harnessAtOpen;
  const modelChanged = draft !== null && (model ?? "") !== modelAtOpen;
  const modelCatalog = modelOptionsForHarness(harness);
  const effortCatalog = effortOptionsForHarness(harness);
  const modelIncompatible = Boolean(
    harnessChanged &&
    model &&
    modelCatalog.length > 0 &&
    !modelCatalog.some((option) => option.id === model),
  );
  const effortIncompatible = Boolean(
    (harnessChanged || modelChanged) &&
    effort &&
    effortCatalog.length > 0 &&
    !effortCatalog.some((option) => option.value === effort),
  );
  const branchWithoutHost = Boolean(
    draft &&
    effectiveHostType === "external" &&
    fieldDisplayValue(draft, "default_branch").trim() !== "" &&
    fieldDisplayValue(draft, "host_id").trim() === "",
  );
  // A workspace carried across a live External → Managed switch is a path, not
  // the repository spec managed sessions need — block Save until it is reset.
  // A managed+workspace bundle loaded as-is may be a valid repo spec, and a
  // Managed→External→Managed round-trip changes nothing, so only a host type
  // that DIFFERS from its opening value gates.
  const hostTypeAtOpen = draft
    ? typeof bundle.host_type === "string"
      ? bundle.host_type
      : (draft.fields.host_type.resolvedAtOpen ?? "external")
    : "external";
  const workspaceBlocksSave = Boolean(
    draft &&
    effectiveHostType === "managed" &&
    effectiveHostType !== hostTypeAtOpen &&
    fieldProvenance(draft, "workspace") !== "inherited",
  );
  const isValid =
    draft !== null &&
    !branchWithoutHost &&
    !workspaceBlocksSave &&
    !modelIncompatible &&
    !effortIncompatible;

  useEffect(() => {
    onValidityChange?.(isValid);
  }, [isValid, onValidityChange]);

  function change(field: DefaultField, value: string) {
    setDraft((current) => {
      if (!current) return current;
      const next = setFieldValue(current, field, value);
      return field === "host_type" && value === "managed" ? stageManagedHostReset(next) : next;
    });
  }

  function reset(field: DefaultField) {
    setDraft((current) => (current ? resetField(current, field) : current));
  }

  if (!draft) {
    return (
      <div data-testid="project-defaults-loading" className="grid gap-3 sm:grid-cols-2">
        {Array.from({ length: 6 }, (_, index) => (
          <div
            key={`project-default-skeleton-${index}`}
            className="h-24 w-full animate-pulse rounded-lg bg-muted"
          />
        ))}
      </div>
    );
  }

  const rawPreviewError = preview.isError;
  const hostError = branchWithoutHost
    ? "Choose a pinned host or reset the default branch."
    : undefined;
  const branchError = branchWithoutHost
    ? "An external default branch requires a pinned host."
    : undefined;
  const modelError = modelIncompatible
    ? "Choose a model compatible with the selected harness, or Reset this override."
    : undefined;
  const effortError = effortIncompatible
    ? "Choose an effort compatible with the selected model, or Reset this override."
    : undefined;
  const retainedRepo =
    effectiveHostType === "external" && fieldProvenance(draft, "repo_url") !== "inherited";
  const retainedWorkspace =
    effectiveHostType === "managed" && fieldProvenance(draft, "workspace") !== "inherited";
  const hostResetStaged = effectiveHostType === "managed" && draft.fields.host_id.resetRequested;

  return (
    <div className="space-y-3">
      {rawPreviewError && (
        <div
          data-testid="project-defaults-error"
          className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3"
          role="alert"
        >
          <p className="min-w-0 text-sm text-destructive">{errorMessage(preview.error)}</p>
          <Button
            type="button"
            variant="outline"
            className="min-h-11"
            aria-label="Retry resolved defaults"
            onClick={() => void preview.refetch()}
          >
            Retry
          </Button>
        </div>
      )}

      {/* Controls stay enabled during a preview error: a broken saved bundle
          (branch without host, null host type) is repaired with these very
          fields, and the fallback baselines make edits raw overrides. */}
      <fieldset className="min-w-0">
        <div className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="min-w-0 sm:col-span-2">
            <DefaultSelectField
              field="host_type"
              label="Host type"
              value={fieldDisplayValue(draft, "host_type")}
              provenance={fieldProvenance(draft, "host_type")}
              options={HOST_TYPE_OPTIONS}
              hint={
                effectiveHostType === "managed"
                  ? "Managed hosts are provisioned by the server."
                  : "External sessions run on a connected host."
              }
              error={
                fieldProvenance(draft, "host_type") === "invalid"
                  ? "Saving repairs this value by restoring the External server default."
                  : undefined
              }
              onChange={(value) => change("host_type", value)}
              onReset={() => reset("host_type")}
            />
          </div>

          {hostResetStaged && (
            <p
              data-testid="project-default-host-reset-notice"
              className="rounded-lg bg-muted p-3 text-xs text-muted-foreground sm:col-span-2"
            >
              Pinned host removed because managed hosts are provisioned by the server.
            </p>
          )}

          {(effectiveHostType === "managed" || retainedRepo) && (
            <div className="min-w-0 sm:col-span-2">
              <DefaultTextField
                field="repo_url"
                label="Repository URL"
                value={fieldDisplayValue(draft, "repo_url")}
                provenance={fieldProvenance(draft, "repo_url")}
                placeholder="No repository — managed sessions start empty"
                hint={
                  retainedRepo
                    ? "Managed-only default retained. Reset it to remove the saved value."
                    : "Repository cloned into new managed sessions."
                }
                onChange={(value) => change("repo_url", value)}
                onReset={() => reset("repo_url")}
              />
            </div>
          )}

          {effectiveHostType === "external" && (
            <div className="min-w-0 sm:col-span-2">
              <DefaultHostPicker
                value={fieldDisplayValue(draft, "host_id")}
                provenance={fieldProvenance(draft, "host_id")}
                hosts={hosts.data ?? []}
                isLoading={hosts.isLoading}
                error={hostError}
                onChange={(value) => change("host_id", value)}
                onReset={() => reset("host_id")}
              />
            </div>
          )}

          {(effectiveHostType === "external" || retainedWorkspace) && (
            <div className="min-w-0 sm:col-span-2">
              <DefaultTextField
                field="workspace"
                label="Workspace path"
                value={fieldDisplayValue(draft, "workspace")}
                provenance={fieldProvenance(draft, "workspace")}
                placeholder="No project workspace"
                hint={
                  retainedWorkspace
                    ? "Managed sessions use this as their repository spec. Reset it if it is a leftover external path."
                    : "Workspace used by new external sessions."
                }
                error={
                  workspaceBlocksSave
                    ? "Managed sessions cannot use this workspace path. Reset it to save."
                    : undefined
                }
                onChange={(value) => change("workspace", value)}
                onReset={() => reset("workspace")}
              />
            </div>
          )}

          <DefaultTextField
            field="default_branch"
            label="Default branch"
            value={fieldDisplayValue(draft, "default_branch")}
            provenance={fieldProvenance(draft, "default_branch")}
            placeholder="Repository default"
            hint={
              effectiveHostType === "managed"
                ? "Branch cloned for new managed sessions."
                : "Branch used for new external sessions on the pinned host."
            }
            error={branchError}
            onChange={(value) => change("default_branch", value)}
            onReset={() => reset("default_branch")}
          />

          <DefaultHarnessPicker
            value={fieldDisplayValue(draft, "harness")}
            provenance={fieldProvenance(draft, "harness")}
            harnessOptions={harnessOptionsForProject(labels)}
            onChange={(value) => change("harness", value)}
            onReset={() => reset("harness")}
          />

          <DefaultModelPicker
            value={fieldDisplayValue(draft, "model")}
            provenance={fieldProvenance(draft, "model")}
            harness={harness}
            catalog={modelCatalog}
            error={modelError}
            onChange={(value) => change("model", value)}
            onReset={() => reset("model")}
          />

          <DefaultEffortPicker
            value={fieldDisplayValue(draft, "reasoning_effort")}
            provenance={fieldProvenance(draft, "reasoning_effort")}
            harness={harness}
            model={model}
            catalog={effortCatalog}
            error={effortError}
            onChange={(value) => change("reasoning_effort", value)}
            onReset={() => reset("reasoning_effort")}
          />
        </div>
      </fieldset>
    </div>
  );
}
