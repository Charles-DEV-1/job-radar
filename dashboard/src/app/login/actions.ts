"use server";
import { redirect } from "next/navigation";
import { ownerEmail } from "@/lib/config";
import { sessionClient } from "@/lib/supabase";

export interface LoginState {
  error: string | null;
}
export async function signIn(
  _: LoginState,
  form: FormData,
): Promise<LoginState> {
  const email = String(form.get("email") ?? "")
    .trim()
    .toLowerCase();
  const password = String(form.get("password") ?? "");
  try {
    if (email !== ownerEmail() || password.length < 1 || password.length > 256)
      return { error: "Unable to sign in. Check your email and password." };
    const client = await sessionClient();
    const result = await client.auth.signInWithPassword({ email, password });
    if (result.error || !result.data.user?.email_confirmed_at)
      return { error: "Unable to sign in. Check your email and password." };
    const owner = await client.rpc("radar_dashboard_is_owner");
    if (owner.error || owner.data !== true) {
      await client.auth.signOut();
      return { error: "Dashboard access is not configured for this account." };
    }
  } catch {
    return { error: "Sign-in is unavailable. Please try again later." };
  }
  redirect("/jobs");
}
