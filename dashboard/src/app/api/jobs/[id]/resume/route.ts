import { NextResponse } from "next/server";
import { z } from "zod";
import { AccessError, privateHeaders, requireSession } from "@/lib/auth";
import { masterResumePath, publicConfig } from "@/lib/config";
import { storageClient } from "@/lib/supabase";
export const dynamic = "force-dynamic";
const uuid = "[a-fA-F0-9-]{36}";
export async function GET(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const headers = privateHeaders();
  try {
    const { client } = await requireSession();
    if (request.headers.get("sec-fetch-site") === "cross-site")
      return NextResponse.json(
        { error: "Request origin rejected" },
        { status: 403, headers },
      );
    const { id } = await context.params;
    if (!z.uuid().safeParse(id).success)
      return NextResponse.json(
        { error: "Invalid job" },
        { status: 400, headers },
      );
    // A user-scoped, RLS-protected lookup MUST precede any service-role access.
    const job = await client
      .from("jobs")
      .select("resume_status,resume_path,resume_filename")
      .eq("id", id)
      .maybeSingle();
    if (job.error) throw new Error("Job lookup unavailable");
    if (!job.data)
      return NextResponse.json(
        { error: "Job not found" },
        { status: 404, headers },
      );
    if (job.data.resume_status === "archived")
      return NextResponse.json(
        { error: "Resume archived after 30 days" },
        { status: 410, headers },
      );
    const bucket = storageClient().storage.from("radar-resumes");
    let kind: "tailored" | "master" = "master";
    let filename = "Master_Resume.docx";
    let path = masterResumePath();
    const candidate = job.data.resume_path;
    if (
      job.data.resume_status === "ready" &&
      new URL(request.url).searchParams.get("master") !== "1" &&
      typeof candidate === "string" &&
      candidate.startsWith(id + "/") &&
      new RegExp(`^${uuid}/${uuid}/[A-Za-z0-9_.-]+\\.docx$`).test(candidate) &&
      !candidate.includes("..")
    ) {
      kind = "tailored";
      path = candidate;
      filename =
        typeof job.data.resume_filename === "string" &&
        /^[A-Za-z0-9_.-]+\.docx$/.test(job.data.resume_filename)
          ? job.data.resume_filename
          : "Tailored_Resume.docx";
    }
    let signed = await bucket.createSignedUrl(path, 60, { download: filename });
    if (signed.error && kind === "tailored") {
      kind = "master";
      filename = "Master_Resume.docx";
      signed = await bucket.createSignedUrl(masterResumePath(), 60, {
        download: filename,
      });
    }
    if (signed.error || !signed.data?.signedUrl)
      return NextResponse.json(
        { error: "Resume download is unavailable. Please try again later." },
        { status: 503, headers },
      );
    const url = new URL(signed.data.signedUrl);
    if (
      url.origin !== new URL(publicConfig().url).origin ||
      !url.pathname.startsWith("/storage/v1/object/sign/radar-resumes/")
    )
      throw new Error("Invalid storage URL");
    return NextResponse.json(
      { url: url.href, filename, kind, expires_in: 60 },
      { headers },
    );
  } catch (e) {
    return NextResponse.json(
      {
        error:
          e instanceof AccessError
            ? e.message
            : "Resume download is unavailable. Please try again later.",
      },
      { status: e instanceof AccessError ? e.status : 503, headers },
    );
  }
}
