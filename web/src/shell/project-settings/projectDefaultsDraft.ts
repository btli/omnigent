export const DEFAULT_FIELDS = [
  "host_type",
  "repo_url",
  "default_branch",
  "host_id",
  "workspace",
  "harness",
  "model",
  "reasoning_effort",
] as const;

export type DefaultField = (typeof DEFAULT_FIELDS)[number];
export type DefaultsBundle = Partial<Record<DefaultField, string | null>>;
export type FieldProvenance = "inherited" | "overridden" | "invalid";

export interface ResolvedBaselines {
  host_type: "managed" | "external";
  host_id: string | null;
  workspace: string | null;
  default_branch: string | null;
  harness: string | null;
  model: string | null;
  reasoning_effort: string | null;
  repo_url: null;
}

export interface DefaultFieldDraft {
  persistedKind: "absent" | "value" | "legacy-null";
  resolvedAtOpen: string | null;
  value: string;
  edited: boolean;
  resetRequested: boolean;
}

export interface DefaultsDraftState {
  fields: Record<DefaultField, DefaultFieldDraft>;
  invalidHostTypeNull: boolean;
}

function normalizedValue(value: string): string {
  return value.trim();
}

export function buildDraft(
  bundle: DefaultsBundle,
  baselines: ResolvedBaselines,
): DefaultsDraftState {
  const fields = Object.fromEntries(
    DEFAULT_FIELDS.map((field) => {
      const hasValue = Object.hasOwn(bundle, field);
      const storedValue = bundle[field];
      const persistedKind = !hasValue
        ? "absent"
        : storedValue === null
          ? "legacy-null"
          : "value";

      return [
        field,
        {
          persistedKind,
          resolvedAtOpen: baselines[field],
          value: typeof storedValue === "string" ? storedValue : (baselines[field] ?? ""),
          edited: false,
          resetRequested: false,
        } satisfies DefaultFieldDraft,
      ];
    }),
  ) as Record<DefaultField, DefaultFieldDraft>;

  return {
    fields,
    invalidHostTypeNull: bundle.host_type === null,
  };
}

export function fieldProvenance(
  state: DefaultsDraftState,
  field: DefaultField,
): FieldProvenance {
  const draft = state.fields[field];

  if (
    field === "host_type" &&
    state.invalidHostTypeNull &&
    !draft.resetRequested &&
    !draft.edited
  ) {
    return "invalid";
  }

  if (
    !draft.resetRequested &&
    (draft.persistedKind === "value" ||
      (draft.edited &&
        normalizedValue(draft.value) !== "" &&
        normalizedValue(draft.value) !== (draft.resolvedAtOpen ?? "")))
  ) {
    return "overridden";
  }

  return "inherited";
}

export function fieldDisplayValue(state: DefaultsDraftState, field: DefaultField): string {
  return state.fields[field].value;
}

export function setFieldValue(
  state: DefaultsDraftState,
  field: DefaultField,
  value: string,
): DefaultsDraftState {
  return {
    ...state,
    fields: {
      ...state.fields,
      [field]: {
        ...state.fields[field],
        value,
        edited: true,
        resetRequested: false,
      },
    },
  };
}

/** What a field resolves to once its project property is removed. For a stored
 *  value the preview echoed that value back as `resolvedAtOpen`, so it cannot
 *  be the post-reset display: under schema v1 the only server default is
 *  host_type=external — every other field inherits nothing. */
function postResetValue(field: DefaultField, draft: DefaultFieldDraft): string {
  if (draft.persistedKind !== "value") return draft.resolvedAtOpen ?? "";
  return field === "host_type" ? "external" : "";
}

export function resetField(state: DefaultsDraftState, field: DefaultField): DefaultsDraftState {
  const draft = state.fields[field];

  return {
    ...state,
    fields: {
      ...state.fields,
      [field]: {
        ...draft,
        value: postResetValue(field, draft),
        edited: false,
        resetRequested: true,
      },
    },
  };
}

export function serializeBundle(state: DefaultsDraftState): DefaultsBundle {
  const bundle: DefaultsBundle = {};

  for (const field of DEFAULT_FIELDS) {
    const draft = state.fields[field];
    if (draft.resetRequested) continue;

    const value = normalizedValue(draft.value);
    if (value === "") continue;
    if (draft.persistedKind !== "value" && value === (draft.resolvedAtOpen ?? "")) continue;

    bundle[field] = value;
  }

  return bundle;
}

function normalizeBundle(bundle: DefaultsBundle): DefaultsBundle {
  const normalized: DefaultsBundle = {};

  for (const field of DEFAULT_FIELDS) {
    const value = bundle[field];
    if (value == null) continue;

    const trimmed = normalizedValue(value);
    if (trimmed !== "") normalized[field] = trimmed;
  }

  return normalized;
}

export function isDirty(state: DefaultsDraftState, originalBundle: DefaultsBundle): boolean {
  return JSON.stringify(serializeBundle(state)) !== JSON.stringify(normalizeBundle(originalBundle));
}
