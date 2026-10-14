import { describe, it, expect, vi, beforeEach } from "vitest";
import { NextRequest } from "next/server";
import { proxy } from "@/proxy";
const getUser = vi.hoisted(() => vi.fn());
vi.mock("@supabase/ssr", () => ({
  createServerClient: () => ({ auth: { getUser } }),
}));
beforeEach(() => {
  vi.stubEnv("SUPABASE_URL", "https://example.supabase.co");
  vi.stubEnv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test");
  vi.stubEnv("ALLOWED_EMAIL", "owner@example.test");
});
describe("private route authentication", () => {
  it("redirects an unauthenticated page request to login without private data", async () => {
    getUser.mockResolvedValue({ data: { user: null }, error: null });
    const response = await proxy(new NextRequest("https://radar.example/jobs"));
    expect(response.status).toBe(307);
    expect(response.headers.get("location")).toBe(
      "https://radar.example/login",
    );
    expect(response.headers.get("cache-control")).toContain("no-store");
  });
  it("rejects an unauthenticated resume API with 401", async () => {
    getUser.mockResolvedValue({ data: { user: null }, error: null });
    const response = await proxy(
      new NextRequest("https://radar.example/api/jobs/id/resume"),
    );
    expect(response.status).toBe(401);
  });
  it("rejects another account even with a valid session", async () => {
    getUser.mockResolvedValue({
      data: {
        user: { email: "other@example.test", email_confirmed_at: "2026-01-01" },
      },
      error: null,
    });
    expect(
      (await proxy(new NextRequest("https://radar.example/api/jobs/id")))
        .status,
    ).toBe(403);
  });
  it("allows the public login without inspecting job data", async () => {
    getUser.mockReset();
    expect(
      (await proxy(new NextRequest("https://radar.example/login"))).status,
    ).toBe(200);
    expect(getUser).not.toHaveBeenCalled();
  });
  it("fails closed when owner configuration is missing", async () => {
    vi.stubEnv("ALLOWED_EMAIL", "");
    expect(
      (await proxy(new NextRequest("https://radar.example/api/jobs/id")))
        .status,
    ).toBe(503);
  });
});
