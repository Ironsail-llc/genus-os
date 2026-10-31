import { describe, it, expect } from "vitest";
import {
  ATTACHMENT_LIMITS_TEXT,
  MAX_ATTACHMENTS,
  MAX_ATTACHMENT_BYTES,
  attachmentMime,
  checkAttachment,
  stripAttachmentNote,
} from "../attachments";

describe("attachmentMime: the extension and the type must agree", () => {
  it.each([
    ["tool.exe", "text/plain"],
    ["evil.png", "text/plain"],
    ["notes.txt", "image/png"],
    ["archive.zip", ""],
  ])("refuses %s as %j", (name, type) => {
    expect(attachmentMime(name, type)).toBeNull();
  });

  it.each([
    ["notes.txt", "text/plain", "text/plain"],
    ["photo.jpg", "image/jpeg", "image/jpeg"],
    ["README.md", "", "text/markdown"],
    ["notes.md", "text/x-markdown", "text/markdown"],
    ["notes.md", "text/plain", "text/plain"],
    ["README", "text/plain", "text/plain"],
  ])("takes %s as %j and sends %s", (name, type, sent) => {
    expect(attachmentMime(name, type)).toBe(sent);
  });
});

describe("stripAttachmentNote", () => {
  const NOTE = "[1 file attached — kept on disk; use the paths below]\n- a.txt — document, 2 B\n   path: /inbox/a.txt";

  it("keeps the operator's words and drops the engine's note", () => {
    expect(stripAttachmentNote(`what is this?\n\n${NOTE}`)).toBe("what is this?");
  });

  it("drops a note that is the whole turn", () => {
    expect(stripAttachmentNote(NOTE)).toBe("");
  });

  it("leaves the phrase alone inside the operator's own text", () => {
    const own = "my tool prints [2 files attached — done] when it finishes";
    expect(stripAttachmentNote(own)).toBe(own);
    expect(stripAttachmentNote(`${own}\n\n${NOTE}`)).toBe(own);
  });

  it("keeps what was added while the agent worked, after the note", () => {
    const turn = `what is this?\n\n${NOTE}\n\n[added while working] and this one too`;
    expect(stripAttachmentNote(turn)).toBe("what is this?\n\n[added while working] and this one too");
  });

  it("cuts at the LAST note, so a quoted note above it survives", () => {
    const quoted = `earlier I saw:\n\n[3 files attached — kept on disk]\nlike that`;
    expect(stripAttachmentNote(`${quoted}\n\n${NOTE}`)).toBe(quoted);
  });
});

describe("the stated limits come from the numbers", () => {
  it("says the size and count the checks enforce", () => {
    const mb = MAX_ATTACHMENT_BYTES / (1024 * 1024);
    expect(ATTACHMENT_LIMITS_TEXT).toContain(`up to ${mb} MB each`);
    expect(ATTACHMENT_LIMITS_TEXT).toContain(`${MAX_ATTACHMENTS} per message`);
  });

  it("names the size limit in an oversize refusal", () => {
    const data = "A".repeat(Math.ceil((MAX_ATTACHMENT_BYTES + 3) / 3) * 4);
    expect(checkAttachment({ name: "big.txt", mime: "text/plain", data })?.error).toBe(
      `big.txt is larger than ${MAX_ATTACHMENT_BYTES / (1024 * 1024)} MB`,
    );
  });
});
