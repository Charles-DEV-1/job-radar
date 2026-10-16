import { describe, expect, it } from "vitest";
import { sameOrigin } from "@/lib/auth";

describe("mutation origin validation", () => {
  it.each([
    ["http://localhost:3010", "127.0.0.1:3010", "http://127.0.0.1:3010"],
    ["http://127.0.0.1:3010", "localhost:3010", "http://localhost:3010"],
    [
      "https://internal.test",
      "radar.example.test",
      "https://radar.example.test",
    ],
    ["http://localhost:3010", "[::1]:3010", "http://[::1]:3010"],
  ])("uses browser-facing Host for %s / %s", (url, host, origin) => {
    expect(sameOrigin(new Request(url, { headers: { host, origin } }))).toBe(
      true,
    );
  });

  const rejectedHeaders: Record<string, string>[] = [
    { origin: "https://attacker.test", host: "radar.example.test" },
    { origin: "http://radar.example.test", host: "radar.example.test" },
    { origin: "https://radar.example.test:9999", host: "radar.example.test" },
    { origin: "null", host: "radar.example.test" },
    { host: "radar.example.test" },
    {
      origin: "https://attacker.test",
      host: "radar.example.test",
      "x-forwarded-host": "attacker.test",
    },
    {
      origin: "https://attacker.test",
      host: "radar.example.test@attacker.test",
    },
  ];
  it.each(rejectedHeaders)(
    "rejects missing, foreign or malformed origin/host: %j",
    (headers) => {
      expect(
        sameOrigin(new Request("https://internal.test", { headers })),
      ).toBe(false);
    },
  );

  it("retains URL-origin validation when Host is absent", () => {
    expect(
      sameOrigin(
        new Request("https://radar.example.test", {
          headers: { origin: "https://radar.example.test" },
        }),
      ),
    ).toBe(true);
  });
});
