"use server";
import { redirect } from "next/navigation";
import { cookies } from "next/headers";
import { requireSession } from "@/lib/auth";
export async function signOut() {
  const { client } = await requireSession();
  try {
    await client.auth.signOut({ scope: "local" });
  } finally {
    // Remove this browser's session even if upstream revocation is unavailable.
    const store = await cookies();
    for (const cookie of store.getAll()) {
      if (/^sb-.+-auth-token(?:\.\d+)?$/.test(cookie.name))
        store.delete(cookie.name);
    }
  }
  redirect("/login");
}
