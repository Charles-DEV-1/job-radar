import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { signIn } from "@/app/login/actions";

const mocks = vi.hoisted(() => ({
  signIn: vi.fn(),
  signOut: vi.fn(),
  rpc: vi.fn(),
}));
vi.mock("@/lib/supabase", () => ({
  sessionClient: async () => ({
    auth: { signInWithPassword: mocks.signIn, signOut: mocks.signOut },
    rpc: mocks.rpc,
  }),
}));
vi.mock("next/navigation", () => ({
  redirect: (path: string) => {
    throw new Error(`redirect:${path}`);
  },
}));

function credentials(email = "owner@example.test") {
  const form = new FormData();
  form.set("email", email);
  form.set("password", "test-password-only");
  return form;
}

beforeEach(() => {
  vi.stubEnv("ALLOWED_EMAIL", "owner@example.test");
  vi.spyOn(console, "error").mockImplementation(() => {});
  mocks.signIn.mockResolvedValue({
    data: { user: { email_confirmed_at: "2026-10-16" } },
    error: null,
  });
  mocks.rpc.mockResolvedValue({ data: true, error: null });
  mocks.signOut.mockResolvedValue({ error: null });
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

describe("dashboard sign-in", () => {
  it("signs in the configured confirmed owner and redirects to jobs", async () => {
    await expect(
      signIn({ error: null }, credentials(" Owner@Example.test ")),
    ).rejects.toThrow("redirect:/jobs");
    expect(mocks.signIn).toHaveBeenCalledWith({
      email: "owner@example.test",
      password: "test-password-only",
    });
    expect(mocks.rpc).toHaveBeenCalledWith("radar_dashboard_is_owner");
  });

  it("rejects another email before authenticating", async () => {
    const result = await signIn(
      { error: null },
      credentials("other@example.test"),
    );
    expect(result.error).toContain("Check your email and password");
    expect(mocks.signIn).not.toHaveBeenCalled();
  });

  it("keeps invalid credentials distinct from a service outage", async () => {
    mocks.signIn.mockResolvedValue({
      data: { user: null },
      error: { status: 400, code: "invalid_credentials" },
    });
    const result = await signIn({ error: null }, credentials());
    expect(result.error).toContain("Check your email and password");
    expect(console.error).not.toHaveBeenCalled();
    expect(mocks.rpc).not.toHaveBeenCalled();
  });

  it.each([0, 503])(
    "reports an unavailable service for status %s without logging credentials",
    async (status) => {
      mocks.signIn.mockResolvedValue({
        data: { user: null },
        error: { status, code: undefined },
      });
      const result = await signIn({ error: null }, credentials());
      expect(result.error).toContain("Sign-in is unavailable");
      expect(console.error).toHaveBeenCalledWith(
        "Dashboard cannot reach the authentication service",
        { code: undefined, status },
      );
      expect(mocks.rpc).not.toHaveBeenCalled();
    },
  );

  it("rejects a confirmed account without owner permission and signs it out", async () => {
    mocks.rpc.mockResolvedValue({ data: false, error: null });
    const result = await signIn({ error: null }, credentials());
    expect(result.error).toContain("Dashboard access is not configured");
    expect(mocks.signOut).toHaveBeenCalled();
  });

  it("does not log raw exceptions that may contain request credentials", async () => {
    mocks.signIn.mockRejectedValue(new Error("test-password-only"));
    const result = await signIn({ error: null }, credentials());
    expect(result.error).toContain("Sign-in is unavailable");
    expect(console.error).toHaveBeenCalledWith(
      "Dashboard sign-in could not be completed",
    );
  });
});
