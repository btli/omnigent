import type { FilesystemAttachmentPolicy } from "./capabilities";
import { describe, expect, it } from "vitest";
import {
  ATTACHMENT_SIZE_LIMITS_MB,
  attachmentFilename,
  attachmentAccept,
  attachmentKey,
  classifyAttachment,
  validateAttachments,
} from "./attachments";

function makeFile(name: string, type: string, bytes = 10): File {
  return new File([new Uint8Array(bytes)], name, { type });
}

const defaultAttachmentPolicy = {
  allowed_extensions: [".zip", ".docx", ".xlsx", ".pptx", ".db", ".sqlite", ".sqlite3"],
  denied_extensions: [],
  max_bytes: 50 * 1024 * 1024,
  max_files: 20,
  max_total_bytes: 200 * 1024 * 1024,
  harnesses: ["claude-native", "codex-native"],
};

const policy: FilesystemAttachmentPolicy = {
  allowed_extensions: [".mp4"],
  denied_extensions: [".exe"],
  max_bytes: 100 * 1024 * 1024,
  max_files: 2,
  max_total_bytes: 150 * 1024 * 1024,
  harnesses: ["claude-native", "codex-native"],
};

const MB = 1024 * 1024;

describe("attachmentFilename", () => {
  it.each(["screenshot.png", "notes.txt", "report.pdf"])("preserves the name %s", (name) => {
    expect(attachmentFilename(makeFile(name, ""))).toBe(name);
  });

  it("uses the upload filename for an unnamed clipboard image without renaming the File", () => {
    const file = makeFile("", "image/png");
    expect(attachmentFilename(file)).toBe("image.png");
    expect(file.name).toBe("");
  });
});

describe("attachmentKey", () => {
  it("is stable per File object and distinct for equivalent files", () => {
    const first = makeFile("a.png", "image/png");
    const second = makeFile("a.png", "image/png");

    expect(attachmentKey(first)).toBe(attachmentKey(first));
    expect(attachmentKey(first)).not.toBe(attachmentKey(second));
  });
});

describe("classifyAttachment", () => {
  const classifyDefaultAttachment = (file: File) =>
    classifyAttachment(file, defaultAttachmentPolicy);
  it("classifies images by MIME", () => {
    expect(classifyDefaultAttachment(makeFile("a.png", "image/png"))).toBe("image");
    expect(classifyDefaultAttachment(makeFile("a.jpg", "image/jpeg"))).toBe("image");
  });

  it("classifies PDF by MIME or extension", () => {
    expect(classifyDefaultAttachment(makeFile("a.pdf", "application/pdf"))).toBe("pdf");
    // Some browsers report an empty type for PDFs — fall back to extension.
    expect(classifyDefaultAttachment(makeFile("a.pdf", ""))).toBe("pdf");
  });

  it("classifies text/code, including code files with empty/wrong MIME", () => {
    expect(classifyDefaultAttachment(makeFile("a.txt", "text/plain"))).toBe("text");
    expect(classifyDefaultAttachment(makeFile("a.json", "application/json"))).toBe("text");
    // .ts reports video/mp2t in some browsers; extension wins.
    expect(classifyDefaultAttachment(makeFile("a.ts", "video/mp2t"))).toBe("text");
    expect(classifyDefaultAttachment(makeFile("main.rs", ""))).toBe("text");
    expect(classifyDefaultAttachment(makeFile("notebook.ipynb", ""))).toBe("text");
    // Windows/Excel tags .csv as application/vnd.ms-excel — extension wins.
    expect(classifyDefaultAttachment(makeFile("data.csv", "application/vnd.ms-excel"))).toBe(
      "text",
    );
  });

  it("classifies archives, Office documents and databases as files", () => {
    const pptx = "application/vnd.openxmlformats-officedocument.presentationml.presentation";
    expect(classifyDefaultAttachment(makeFile("deck.pptx", pptx))).toBe("file");
    expect(classifyDefaultAttachment(makeFile("a.zip", "application/zip"))).toBe("file");
    // Office files are zip containers, so the browser often mislabels them.
    expect(classifyDefaultAttachment(makeFile("report.docx", "application/zip"))).toBe("file");
    expect(classifyDefaultAttachment(makeFile("sheet.xlsx", "application/octet-stream"))).toBe(
      "file",
    );
    expect(classifyDefaultAttachment(makeFile("app.sqlite3", ""))).toBe("file");
    // The extension wins over a text MIME, matching the server.
    expect(classifyDefaultAttachment(makeFile("a.zip", "text/plain"))).toBe("file");
  });

  it("rejects types outside the supported allowlist", () => {
    expect(classifyDefaultAttachment(makeFile("a.bin", "application/octet-stream"))).toBeNull();
    expect(classifyDefaultAttachment(makeFile("a.mp4", "video/mp4"))).toBeNull();
    expect(classifyDefaultAttachment(makeFile("song.mp3", "audio/mpeg"))).toBeNull();
    expect(classifyDefaultAttachment(makeFile("noext", ""))).toBeNull();
  });

  it("recognizes a text/code extension the MIME mislabels", () => {
    // .csv tagged as an office MIME still uses the text size limit.
    expect(classifyDefaultAttachment(makeFile("data.csv", "application/vnd.ms-excel"))).toBe(
      "text",
    );
  });
});

describe("validateAttachments", () => {
  const validateDefaultAttachments = (files: File[]) =>
    validateAttachments(files, defaultAttachmentPolicy);
  it("accepts supported files within their size limit", () => {
    const files = [makeFile("a.png", "image/png"), makeFile("a.pdf", "application/pdf")];
    const { accepted, errors } = validateDefaultAttachments(files);
    expect(accepted).toHaveLength(2);
    expect(errors).toHaveLength(0);
  });

  it("rejects unsupported types with a message", () => {
    const { accepted, errors } = validateDefaultAttachments([makeFile("clip.mp4", "video/mp4")]);
    expect(accepted).toHaveLength(0);
    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain("clip.mp4");
  });

  it("accepts an archive up to its larger limit", () => {
    const zip = makeFile("bundle.zip", "application/zip", 30 * MB);
    const { accepted, errors } = validateDefaultAttachments([zip]);
    expect(accepted).toEqual([zip]);
    expect(errors).toHaveLength(0);
  });

  it("rejects an archive over its limit", () => {
    const huge = makeFile("bundle.zip", "application/zip", defaultAttachmentPolicy.max_bytes + 1);
    const { accepted, errors } = validateDefaultAttachments([huge]);
    expect(accepted).toHaveLength(0);
    expect(errors[0]).toContain("too large");
  });

  it("rejects files over their per-type size limit", () => {
    const bigImage = makeFile("huge.png", "image/png", ATTACHMENT_SIZE_LIMITS_MB.image * MB + 1);
    const { accepted, errors } = validateDefaultAttachments([bigImage]);
    expect(accepted).toHaveLength(0);
    expect(errors[0]).toContain("too large");
  });

  it("caps non-compressible images (SVG) at the smaller limit", () => {
    // Under 5 MB: accepted. A raster PNG the same size would be fine too, but
    // SVG can't be compressed server-side, so it keeps the small cap.
    const smallSvg = makeFile("icon.svg", "image/svg+xml", 4 * MB);
    expect(validateDefaultAttachments([smallSvg]).accepted).toHaveLength(1);

    // Between the SVG cap (5 MB) and the raster image cap (50 MB): rejected.
    const bigSvg = makeFile("big.svg", "image/svg+xml", 6 * MB);
    const { accepted, errors } = validateDefaultAttachments([bigSvg]);
    expect(accepted).toHaveLength(0);
    expect(errors[0]).toContain("too large");
  });

  it("partitions a mixed batch into accepted + errors", () => {
    const ok = makeFile("a.png", "image/png");
    const zip = makeFile("a.zip", "application/zip");
    const badType = makeFile("a.mp4", "video/mp4");
    const tooBig = makeFile("big.pdf", "application/pdf", ATTACHMENT_SIZE_LIMITS_MB.pdf * MB + 1);
    const { accepted, errors } = validateDefaultAttachments([ok, zip, badType, tooBig]);
    expect(accepted).toEqual([ok, zip]);
    expect(errors).toHaveLength(2);
  });
});

describe("published filesystem policy", () => {
  it.each(["", "text/plain", "image/png"])(
    "uses extension-first admission with MIME %s",
    (type) => {
      const file = makeFile("clip.MP4. ", type, 60 * MB);
      expect(classifyAttachment(file, policy)).toBe("file");
      expect(validateAttachments([file], policy).accepted).toEqual([file]);
    },
  );
  it("uses replacement semantics including an empty list", () => {
    const file = makeFile("bundle.zip", "application/zip");
    expect(validateAttachments([file], { ...policy, allowed_extensions: [] }).errors[0]).toContain(
      "server policy",
    );
  });
  it.each(["bad.EXE.txt", "bad.txt.exe. ", "bad.ExE .mp4"])(
    "denies normalized suffix segments of %s",
    (name) => {
      expect(
        validateAttachments([makeFile(name, "text/plain")], { ...policy, allowed_extensions: "*" })
          .errors[0],
      ).toContain("denied");
    },
  );
  it.each(["bad\u0000.mp4", "bad\n.txt", "../a.txt", "a\\b.txt", ".", ".."])(
    "rejects unsafe filename %j",
    (name) => {
      expect(validateAttachments([makeFile(name, "text/plain")], policy).errors[0]).toContain(
        "filename",
      );
    },
  );
  it("wildcard admits unknown and extensionless files but preserves inline categories", () => {
    const wildcard = { ...policy, allowed_extensions: "*" as const };
    expect(classifyAttachment(makeFile("clip.mp4", "video/mp4"), wildcard)).toBe("file");
    expect(classifyAttachment(makeFile("noext", ""), wildcard)).toBe("file");
    expect(classifyAttachment(makeFile("a.png", "image/png"), wildcard)).toBe("image");
    expect(classifyAttachment(makeFile("a.txt", "text/plain"), wildcard)).toBe("text");
  });
  it("uses server byte, count and aggregate limits including previous files", () => {
    const small = { ...policy, max_bytes: 12, max_files: 1, max_total_bytes: 15 };
    const file = makeFile("clip.mp4", "video/mp4");
    expect(validateAttachments([makeFile("big.mp4", "", 13)], small).errors[0]).toContain(
      "too large",
    );
    expect(validateAttachments([file], small, [file]).errors[0]).toContain("1 files");
    expect(validateAttachments([file, file], { ...small, max_files: 3 }).errors[0]).toContain(
      "total",
    );
  });
  it("leaves unknown types and their size to older servers", () => {
    const file = makeFile("large.mp4", "video/mp4", 60 * MB);
    expect(validateAttachments([file]).accepted).toEqual([file]);
    expect(attachmentAccept()).toBeUndefined();
  });
  it("builds policy picker filters and removes them under wildcard", () => {
    expect(attachmentAccept(policy)).toContain(".mp4");
    expect(attachmentAccept(policy)).not.toContain(".zip");
    expect(attachmentAccept({ ...policy, allowed_extensions: "*" })).toBeUndefined();
  });
});

describe("portable attachment filenames", () => {
  it.each([
    "bad.exe:stream.mp4",
    "bad.mp4:stream.exe",
    "clip.mp4:stream",
    "clip%00.mp4",
    "clip%0a.mp4",
    "clip%3Astream.mp4",
    "clip\u0085.mp4",
  ])("rejects server-invalid filename %j", (name) => {
    expect(validateAttachments([makeFile(name, "text/plain")], policy).errors[0]).toContain(
      "filename",
    );
  });
  it("keeps images with missing MIME inline under wildcard", () => {
    expect(
      classifyAttachment(makeFile("photo.png", ""), { ...policy, allowed_extensions: "*" }),
    ).toBe("image");
  });
});

describe("filename-based inline eligibility", () => {
  it.each(["text/plain", "image/png"])(
    "keeps mislabeled ZIP filesystem under wildcard: %s",
    (type) => {
      const file = makeFile("bundle.zip", type, 60 * MB);
      const wildcard = { ...policy, allowed_extensions: "*" as const };
      expect(classifyAttachment(file, wildcard)).toBe("file");
      expect(validateAttachments([file], wildcard).accepted).toEqual([file]);
    },
  );
  it.each(["text/plain", "image/png"])("rejects unlisted video despite MIME: %s", (type) => {
    expect(
      validateAttachments([makeFile("clip.mp4", type)], { ...policy, allowed_extensions: [] })
        .errors[0],
    ).toContain("server policy");
  });
});

it.each([[], [".mp4"], "*"] as const)("uses published inline formats under %j", (allowed) => {
  const inlinePolicy = {
    allowed_extensions: allowed === "*" ? ("*" as const) : [...allowed],
    denied_extensions: [],
    max_bytes: 10,
    max_files: 2,
    max_total_bytes: 20,
    harnesses: ["claude-native"],
    inline_extensions: { ".ics": "text" as const },
  };
  const calendar = new File(["BEGIN:VCALENDAR"], "calendar.ics", { type: "image/png" });
  expect(classifyAttachment(calendar, inlinePolicy)).toBe("text");
  expect(validateAttachments([calendar], inlinePolicy).accepted).toEqual([calendar]);
  expect(
    validateAttachments([calendar], { ...inlinePolicy, denied_extensions: [".ics"] }).accepted,
  ).toEqual([]);
});
