import { NextResponse } from "next/server";
import { z } from "zod";
import {
  AccessError,
  privateHeaders,
  requireSession,
  sameOrigin,
} from "@/lib/auth";
import { jobSchema, LIST_COLUMNS, updateSchema } from "@/lib/model";
export const dynamic = "force-dynamic";
export async function PATCH(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const headers = privateHeaders();
  try {
    const { client } = await requireSession();
    if (!sameOrigin(request))
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
    const text = await request.text();
    if (text.length > 20000)
      return NextResponse.json(
        { error: "Update is too large" },
        { status: 413, headers },
      );
    let input;
    try {
      input = updateSchema.safeParse(JSON.parse(text));
    } catch {
      return NextResponse.json(
        { error: "Invalid update" },
        { status: 400, headers },
      );
    }
    if (!input.success)
      return NextResponse.json(
        { error: "Invalid status or notes" },
        { status: 400, headers },
      );
    const { expected_status, ...values } = input.data;
    const result = await client
      .from("jobs")
      .update(values)
      .eq("id", id)
      .eq("status", expected_status)
      .select(LIST_COLUMNS)
      .maybeSingle();
    if (result.error)
      return NextResponse.json(
        { error: "Update could not be saved. Please try again." },
        { status: 503, headers },
      );
    if (!result.data)
      return NextResponse.json(
        {
          error:
            "This job changed or is unavailable. Refresh before saving again.",
        },
        { status: 409, headers },
      );
    return NextResponse.json(
      { job: jobSchema.parse(result.data) },
      { headers },
    );
  } catch (e) {
    return NextResponse.json(
      {
        error:
          e instanceof AccessError
            ? e.message
            : "Update could not be saved. Please try again.",
      },
      { status: e instanceof AccessError ? e.status : 503, headers },
    );
  }
}
