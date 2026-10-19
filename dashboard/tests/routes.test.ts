import { beforeEach, describe, it, expect, vi } from "vitest";
import { GET } from "@/app/api/jobs/[id]/resume/route";
import { PATCH } from "@/app/api/jobs/[id]/route";
import { AccessError } from "@/lib/auth";
import { ID, job } from "./fixtures";
const stubs = vi.hoisted(() => ({
  requireSession: vi.fn(),
  sign: vi.fn(),
  storage: vi.fn(),
  single: vi.fn(),
  update: vi.fn(),
  eq: vi.fn(),
  select: vi.fn(),
  from: vi.fn(),
}));
vi.mock("@/lib/auth", async (original) => ({
  ...(await original<typeof import("@/lib/auth")>()),
  requireSession: stubs.requireSession,
}));
vi.mock("@/lib/supabase", () => ({ storageClient: stubs.storage }));
const context = { params: Promise.resolve({ id: ID }) };
beforeEach(() => {
  vi.stubEnv("SUPABASE_URL", "https://example.supabase.co");
  vi.stubEnv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test");
  const builder = {
    select: stubs.select,
    update: stubs.update,
    eq: stubs.eq,
    maybeSingle: stubs.single,
  };
  stubs.select.mockReturnValue(builder);
  stubs.update.mockReturnValue(builder);
  stubs.eq.mockReturnValue(builder);
  stubs.from.mockReturnValue(builder);
  stubs.requireSession.mockResolvedValue({ client: { from: stubs.from } });
  stubs.storage.mockReturnValue({
    storage: { from: () => ({ createSignedUrl: stubs.sign }) },
  });
  stubs.sign.mockResolvedValue({
    data: {
      signedUrl:
        "https://example.supabase.co/storage/v1/object/sign/radar-resumes/master/master_resume.docx?token=test",
    },
    error: null,
  });
});
describe("private signed downloads", () => {
  it("rejects unauthenticated access before reading jobs or touching the service client", async () => {
    stubs.requireSession.mockRejectedValue(new AccessError(401));
    const result = await GET(
      new Request(`https://radar.test/api/jobs/${ID}/resume`),
      context,
    );
    expect(result.status).toBe(401);
    expect(stubs.storage).not.toHaveBeenCalled();
    expect(stubs.from).not.toHaveBeenCalled();
  });
  it.each(["pending", "failed"])(
    "offers the private master for %s resumes with a 60 second lifetime",
    async (status) => {
      stubs.single.mockResolvedValue({
        data: {
          resume_status: status,
          resume_path: null,
          resume_filename: null,
        },
        error: null,
      });
      const result = await GET(
        new Request(`https://radar.test/api/jobs/${ID}/resume`),
        context,
      );
      expect(stubs.sign).toHaveBeenCalledWith("master/master_resume.docx", 60, {
        download: "Master_Resume.docx",
      });
      expect(await result.json()).toMatchObject({
        kind: "master",
        expires_in: 60,
      });
      expect(result.headers.get("cache-control")).toContain("no-store");
    },
  );
  it("does not offer the master file when the tailored resume has been archived", async () => {
    stubs.single.mockResolvedValue({
      data: {
        resume_status: "archived",
        resume_path: null,
        resume_filename: null,
      },
      error: null,
    });
    const result = await GET(
      new Request(`https://radar.test/api/jobs/${ID}/resume`),
      context,
    );
    expect(result.status).toBe(410);
    expect(await result.json()).toMatchObject({
      error: "Resume archived after 30 days",
    });
    expect(stubs.sign).not.toHaveBeenCalled();
  });
  it("uses the user-scoped job lookup and rejects cross-job paths", async () => {
    stubs.single.mockResolvedValue({
      data: {
        resume_status: "ready",
        resume_path: "other-user/job/resume.docx",
      },
      error: null,
    });
    await GET(new Request(`https://radar.test/api/jobs/${ID}/resume`), context);
    expect(stubs.eq).toHaveBeenCalledWith("id", ID);
    expect(stubs.sign).toHaveBeenCalledWith("master/master_resume.docx", 60, {
      download: "Master_Resume.docx",
    });
  });
  it("signs a valid tailored DOCX and never exposes a service credential", async () => {
    const path = `${ID}/00000000-0000-4000-8000-000000000002/Test_Resume.docx`;
    stubs.single.mockResolvedValue({
      data: {
        resume_status: "ready",
        resume_path: path,
        resume_filename: "Test_Resume.docx",
      },
      error: null,
    });
    const result = await GET(
      new Request(`https://radar.test/api/jobs/${ID}/resume`),
      context,
    );
    expect(stubs.sign).toHaveBeenCalledWith(path, 60, {
      download: "Test_Resume.docx",
    });
    expect(await result.json()).toMatchObject({ kind: "tailored" });
  });
  it("does not sign a job hidden by RLS", async () => {
    stubs.single.mockResolvedValue({ data: null, error: null });
    expect(
      (
        await GET(
          new Request(`https://radar.test/api/jobs/${ID}/resume`),
          context,
        )
      ).status,
    ).toBe(404);
    expect(stubs.storage).not.toHaveBeenCalled();
  });
});
describe("status and notes updates", () => {
  function request(body: unknown, origin = "https://radar.test") {
    return new Request(`https://radar.test/api/jobs/${ID}`, {
      method: "PATCH",
      headers: { origin, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }
  it("rejects unauthenticated mutations", async () => {
    stubs.requireSession.mockRejectedValue(new AccessError(401));
    expect(
      (
        await PATCH(
          request({ status: "applied", expected_status: "new" }),
          context,
        )
      ).status,
    ).toBe(401);
    expect(stubs.update).not.toHaveBeenCalled();
  });
  it("saves through the owner session with a current-status guard", async () => {
    stubs.single.mockResolvedValue({
      data: job({ status: "applied" }),
      error: null,
    });
    expect(
      (
        await PATCH(
          request({ status: "applied", expected_status: "new" }),
          context,
        )
      ).status,
    ).toBe(200);
    expect(stubs.update).toHaveBeenCalledWith({ status: "applied" });
    expect(stubs.eq).toHaveBeenCalledWith("status", "new");
  });
  it("rejects cross-origin requests and extra immutable fields", async () => {
    expect(
      (
        await PATCH(
          request(
            { status: "applied", expected_status: "new" },
            "https://other.test",
          ),
          context,
        )
      ).status,
    ).toBe(403);
    expect(
      (
        await PATCH(
          request({
            status: "applied",
            expected_status: "new",
            description_text: "edited",
          }),
          context,
        )
      ).status,
    ).toBe(400);
    expect(stubs.update).not.toHaveBeenCalled();
  });
});
