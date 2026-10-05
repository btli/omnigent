import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { VideoViewer } from "./VideoViewer";
import {
  downloadWorkspaceFile,
  fetchWorkspaceFileBlob,
  usesDirectFileDownload,
} from "@/hooks/useFileContent";

vi.mock("@/hooks/useFileContent", () => ({
  workspaceFileDownloadUrl: (session: string, path: string) =>
    `/raw/${session}/${path}?download=true`,
  usesDirectFileDownload: vi.fn(() => true),
  fetchWorkspaceFileBlob: vi.fn(),
  downloadWorkspaceFile: vi.fn(),
}));

beforeEach(() => {
  vi.mocked(usesDirectFileDownload).mockReturnValue(true);
  vi.mocked(fetchWorkspaceFileBlob).mockReset();
  vi.mocked(downloadWorkspaceFile).mockReset();
  vi.mocked(downloadWorkspaceFile).mockResolvedValue(undefined);
  window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
  vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:video"), revokeObjectURL: vi.fn() });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  delete window.__OMNIGENT_BASE_PATH__;
});

describe("VideoViewer", () => {
  it("uses a direct base-prefixed stream with native controls", () => {
    const { container } = render(<VideoViewer conversationId="sess" path="clip.mp4" />);
    const video = container.querySelector("video");
    expect(video).toHaveAttribute("src", "/proxy/6767/raw/sess/clip.mp4?download=true");
    expect(video).toHaveAttribute("controls");
    expect(video).toHaveAttribute("playsinline");
    expect(video).toHaveAttribute("preload", "metadata");
    expect(fetchWorkspaceFileBlob).not.toHaveBeenCalled();
  });
  it("shows loading, uses an authenticated blob, and revokes on unmount", async () => {
    vi.mocked(usesDirectFileDownload).mockReturnValue(false);
    let finish: (blob: Blob) => void = () => {};
    vi.mocked(fetchWorkspaceFileBlob).mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const { container, unmount } = render(<VideoViewer conversationId="sess" path="clip.webm" />);
    expect(screen.getByText("Loading video…")).toBeInTheDocument();
    await act(async () => finish(new Blob(["video"])));
    expect(container.querySelector("video")).toHaveAttribute("src", "blob:video");
    expect(fetchWorkspaceFileBlob).toHaveBeenCalledWith("sess", "clip.webm");
    unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:video");
  });
  it("revokes the previous blob when the path changes", async () => {
    vi.mocked(usesDirectFileDownload).mockReturnValue(false);
    vi.mocked(fetchWorkspaceFileBlob).mockResolvedValue(new Blob(["video"]));
    const { rerender } = render(<VideoViewer conversationId="sess" path="first.webm" />);
    await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalledTimes(1));
    rerender(<VideoViewer conversationId="sess" path="next.webm" />);
    await waitFor(() => expect(fetchWorkspaceFileBlob).toHaveBeenCalledWith("sess", "next.webm"));
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:video");
  });
  it("does not create an object URL after an unmounted fetch completes", async () => {
    vi.mocked(usesDirectFileDownload).mockReturnValue(false);
    let finish: (blob: Blob) => void = () => {};
    vi.mocked(fetchWorkspaceFileBlob).mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const { unmount } = render(<VideoViewer conversationId="sess" path="clip.webm" />);
    unmount();
    await act(async () => finish(new Blob(["video"])));
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });
  it.each(["media", "fetch"])("offers a working Download after a %s error", async (failure) => {
    vi.mocked(usesDirectFileDownload).mockReturnValue(failure === "media");
    vi.mocked(fetchWorkspaceFileBlob).mockRejectedValue(new Error("offline"));
    const { container } = render(<VideoViewer conversationId="sess" path="/tmp/bad.mov" />);
    if (failure === "media") fireEvent.error(container.querySelector("video")!);
    expect(await screen.findByText("This video can't be played here.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(downloadWorkspaceFile).toHaveBeenCalledWith("sess", "/tmp/bad.mov");
  });
});
