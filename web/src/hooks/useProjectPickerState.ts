import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useCreateProject, type Project } from "./useConversations";

function projectCreateErrorMessage(error: unknown): string {
  const detail = error instanceof Error ? error.message : "Please try again.";
  return `Couldn't create project. ${detail}`;
}

/** Shared search and inline-create state for the two project pickers. */
export function useProjectPickerState(
  projects: readonly Project[],
  onSelect: (projectId: string) => void,
) {
  const createProject = useCreateProject();
  const [search, setSearch] = useState("");
  const [creatingNew, setCreatingNew] = useState(false);
  const [newProjectName, setNewProjectNameState] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const newInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (creatingNew) newInputRef.current?.focus();
  }, [creatingNew]);

  const filteredProjects = search
    ? projects.filter((project) => project.name.toLowerCase().includes(search.toLowerCase()))
    : projects;

  function resetCreateState() {
    setCreatingNew(false);
    setNewProjectNameState("");
    setCreateError(null);
  }

  function selectProject(projectId: string) {
    onSelect(projectId);
    setSearch("");
    resetCreateState();
  }

  function beginCreatingProject() {
    setCreatingNew(true);
    setCreateError(null);
  }

  function cancelCreatingProject() {
    resetCreateState();
  }

  function setNewProjectName(name: string) {
    setNewProjectNameState(name);
    setCreateError(null);
  }

  function commitNewProject() {
    const name = newProjectName.trim();
    if (!name || createProject.isPending) return;
    setCreateError(null);
    createProject.mutate(name, {
      onSuccess: (project) => selectProject(project.id),
      onError: (error) => setCreateError(projectCreateErrorMessage(error)),
    });
  }

  function handleNewProjectKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    event.stopPropagation();
    if (event.key === "Enter") {
      event.preventDefault();
      commitNewProject();
    } else if (event.key === "Escape") {
      event.preventDefault();
      cancelCreatingProject();
    }
  }

  return {
    search,
    setSearch,
    filteredProjects,
    creatingNew,
    newProjectName,
    setNewProjectName,
    createError,
    newInputRef,
    beginCreatingProject,
    cancelCreatingProject,
    commitNewProject,
    handleNewProjectKeyDown,
    selectProject,
    isCreatingProject: createProject.isPending,
  };
}
