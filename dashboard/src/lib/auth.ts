import "server-only";
import { cache } from "react";
import { redirect } from "next/navigation";
import { ownerEmail } from "./config";
import { sessionClient } from "./supabase";

export class AccessError extends Error {
  constructor(public status: 401 | 403 | 503) {
    super(
      status === 503
        ? "Dashboard login is not configured"
        : "Authentication required",
    );
  }
}
export const requireSession = cache(async () => {
  let client;
  try {
    client = await sessionClient();
  } catch {
    throw new AccessError(503);
  }
  const { data, error } = await client.auth.getUser();
  if (error || !data.user) throw new AccessError(401);
  if (
    !data.user.email_confirmed_at ||
    data.user.email?.trim().toLowerCase() !== ownerEmail()
  )
    throw new AccessError(403);
  const owner = await client.rpc("radar_dashboard_is_owner");
  if (owner.error) throw new AccessError(503);
  if (owner.data !== true) throw new AccessError(403);
  return { client, user: data.user };
});
export async function requirePageSession() {
  try {
    return await requireSession();
  } catch (e) {
    if (e instanceof AccessError) redirect("/login");
    throw e;
  }
}
export function privateHeaders(): Record<string, string> {
  return {
    "Cache-Control": "private, no-store, max-age=0",
    Pragma: "no-cache",
    "Referrer-Policy": "no-referrer",
  };
}
export function sameOrigin(request: Request): boolean {
  const origin = request.headers.get("origin");
  if (!origin || origin === "null") return false;
  try {
    const url = new URL(request.url);
    // Next.js may use its internal listening hostname in request.url. The Host
    // header identifies the browser-facing authority, including its port.
    // Do not trust arbitrary X-Forwarded-Host values as an additional allowlist.
    const host = request.headers.get("host");
    if (host && !/^[a-z\d.:[\]-]+$/i.test(host)) return false;
    const expected = host
      ? new URL(`${url.protocol}//${host}`).origin
      : url.origin;
    return origin === expected;
  } catch {
    return false;
  }
}
