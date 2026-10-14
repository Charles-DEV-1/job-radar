import { createRequire } from "node:module";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createClient } from "@supabase/supabase-js";
const require = createRequire(import.meta.url);
require("@next/env").loadEnvConfig(process.cwd());
async function setup() {
  const {
    SUPABASE_URL: url,
    SUPABASE_SERVICE_KEY: key,
    ALLOWED_EMAIL: email,
  } = process.env;
  if (!url || !key || !email?.includes("@"))
    throw new Error(
      "Configure SUPABASE_URL, SUPABASE_SERVICE_KEY and ALLOWED_EMAIL in .env.local",
    );
  const path = process.env.MASTER_RESUME_PATH || "master/master_resume.docx";
  if (
    !/^[A-Za-z0-9_-]+\/[A-Za-z0-9_.-]+\.docx$/.test(path) ||
    path.includes("..")
  )
    throw new Error("Invalid master resume storage path");
  const document = await readFile(
    resolve(process.env.MASTER_RESUME_FILE || "../data/master_resume.docx"),
  );
  if (
    document.length > 8388608 ||
    document.length < 4 ||
    document[0] !== 0x50 ||
    document[1] !== 0x4b
  )
    throw new Error("Master resume must be a DOCX under 8 MiB");
  const client = createClient(url, key, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  const bucket = await client.storage.getBucket("radar-resumes");
  if (bucket.error || !bucket.data || bucket.data.public)
    throw new Error(
      "Apply the Stage 4 migration; the resume bucket must be private",
    );
  const configured = await client.rpc("radar_configure_dashboard", {
    p_allowed_email: email.trim().toLowerCase(),
  });
  if (configured.error)
    throw new Error(
      "Owner configuration failed; apply the Stage 5 migration first",
    );
  const upload = await client.storage
    .from("radar-resumes")
    .upload(path, document, {
      upsert: true,
      contentType:
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      cacheControl: "0",
    });
  if (upload.error) throw new Error("Private master resume upload failed");
  console.log(
    "Dashboard owner configured. Master resume uploaded to private storage. No signup or email was sent.",
  );
}
setup().catch((e) => {
  console.error(e instanceof Error ? e.message : "Dashboard setup failed");
  process.exitCode = 1;
});
