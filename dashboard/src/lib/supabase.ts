import "server-only";
import { createServerClient } from "@supabase/ssr";
import { createClient } from "@supabase/supabase-js";
import { cookies } from "next/headers";
import { publicConfig } from "./config";

export async function sessionClient() {
  const store = await cookies();
  const { url, key } = publicConfig();
  return createServerClient(url, key, {
    global: {
      fetch: (input, init) => fetch(input, { ...init, cache: "no-store" }),
    },
    cookieOptions: {
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "lax",
      path: "/",
    },
    cookies: {
      getAll: () => store.getAll(),
      setAll: (items) => {
        try {
          items.forEach(({ name, value, options }) =>
            store.set(name, value, options),
          );
        } catch {
          /* Proxy refreshes cookies for read-only Server Components. */
        }
      },
    },
  });
}
export function storageClient() {
  const { url } = publicConfig();
  const key = process.env.SUPABASE_SERVICE_KEY;
  if (!key) throw new Error("Private downloads are not configured");
  return createClient(url, key, {
    auth: { persistSession: false, autoRefreshToken: false },
    global: {
      fetch: (input, init) => fetch(input, { ...init, cache: "no-store" }),
    },
  });
}
