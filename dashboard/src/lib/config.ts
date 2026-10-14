import "server-only";

export function ownerEmail(): string {
  const email = process.env.ALLOWED_EMAIL?.trim().toLowerCase();
  if (!email || !email.includes("@"))
    throw new Error("Dashboard login is not configured");
  return email;
}
export function publicConfig(): { url: string; key: string } {
  const url = process.env.SUPABASE_URL ?? "";
  const key =
    process.env.SUPABASE_PUBLISHABLE_KEY || process.env.SUPABASE_ANON_KEY || "";
  if (!url || !key || new URL(url).protocol !== "https:")
    throw new Error("Dashboard login is not configured");
  if (!key.startsWith("sb_publishable_")) {
    try {
      const payload = JSON.parse(
        Buffer.from(key.split(".")[1], "base64url").toString(),
      );
      if (payload.role !== "anon") throw new Error();
    } catch {
      throw new Error(
        "Use a publishable or anon key for dashboard authentication",
      );
    }
  }
  return { url, key };
}
export function silentThreshold(): number {
  const value = Number(process.env.SILENT_AFTER_DAYS ?? "21");
  if (!Number.isInteger(value) || value < 1 || value > 3650)
    throw new Error("Invalid SILENT_AFTER_DAYS");
  return value;
}
export function masterResumePath(): string {
  const value = process.env.MASTER_RESUME_PATH || "master/master_resume.docx";
  if (
    !/^[A-Za-z0-9_-]+\/[A-Za-z0-9_.-]+\.docx$/.test(value) ||
    value.includes("..")
  )
    throw new Error("Invalid master resume path");
  return value;
}
