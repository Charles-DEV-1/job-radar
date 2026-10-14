import { it, expect, vi } from "vitest";
import { signOut } from "@/app/(private)/actions";
const mocks = vi.hoisted(() => ({ signOut: vi.fn(), remove: vi.fn() }));
vi.mock("@/lib/auth", () => ({
  requireSession: async () => ({
    client: { auth: { signOut: mocks.signOut } },
  }),
}));
vi.mock("next/headers", () => ({
  cookies: async () => ({
    getAll: () => [
      { name: "sb-project-auth-token.0" },
      { name: "sb-project-auth-token.1" },
      { name: "preferences" },
    ],
    delete: mocks.remove,
  }),
}));
vi.mock("next/navigation", () => ({
  redirect: () => {
    throw new Error("redirected to login");
  },
}));
it("clears only session cookies when signing out, including an upstream revocation error", async () => {
  mocks.signOut.mockResolvedValue({ error: new Error("upstream unavailable") });
  await expect(signOut()).rejects.toThrow("redirected to login");
  expect(mocks.signOut).toHaveBeenCalledWith({ scope: "local" });
  expect(mocks.remove.mock.calls).toEqual([
    ["sb-project-auth-token.0"],
    ["sb-project-auth-token.1"],
  ]);
});
