import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { JobCard } from "@/components/job-card";
import { job } from "./fixtures";
const refresh = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }) }));
describe("JobCard status persistence", () => {
  it("shows that a pending resume is not tailored and offers the master", () => {
    render(
      <JobCard
        initial={job({ resume_status: "pending" })}
        now="2026-10-14T12:00:00Z"
        silentAfter={21}
      />,
    );
    expect(
      screen.getByText(/Master resume \(not tailored yet\)/),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Download master" }),
    ).toBeVisible();
  });
  it("saves Applied immediately, updates the visible state and refreshes server counts", async () => {
    const fetcher = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          job: job({
            status: "applied",
            status_changed_at: "2026-10-14T12:00:00Z",
          }),
        }),
        { status: 200 },
      ),
    );
    render(
      <JobCard initial={job()} now="2026-10-14T12:00:00Z" silentAfter={21} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Applied" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Applied" })).toHaveAttribute(
        "aria-pressed",
        "true",
      ),
    );
    expect(JSON.parse(fetcher.mock.calls[0][1]!.body as string)).toEqual({
      status: "applied",
      expected_status: "new",
    });
    expect(refresh).toHaveBeenCalled();
  });
  it("keeps the old status visible and shows a failed update", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error: "Refresh before saving again" }), {
        status: 409,
      }),
    );
    render(
      <JobCard initial={job()} now="2026-10-14T12:00:00Z" silentAfter={21} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Applied" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Refresh before saving",
    );
    expect(screen.getByRole("button", { name: "New" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
  it("saves private notes without fabricating a status transition", async () => {
    const fetcher = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(
          JSON.stringify({ job: job({ notes: "Follow up Monday" }) }),
          { status: 200 },
        ),
      );
    render(
      <JobCard initial={job()} now="2026-10-14T12:00:00Z" silentAfter={21} />,
    );
    await userEvent.click(screen.getByText("Notes"));
    await userEvent.type(
      screen.getByLabelText("Private notes"),
      "Follow up Monday",
    );
    await userEvent.click(screen.getByRole("button", { name: "Save notes" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Notes saved");
    expect(JSON.parse(fetcher.mock.calls[0][1]!.body as string)).toEqual({
      notes: "Follow up Monday",
      expected_status: "new",
    });
  });
  it("offers master fallback and suggests Silent using a real Applied transition date", () => {
    render(
      <JobCard
        initial={job({ resume_status: "failed", status: "applied" })}
        now="2026-10-25T12:00:00Z"
        silentAfter={21}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Download master" }),
    ).toBeVisible();
    expect(screen.getByText(/Applied 24 days ago/)).toBeVisible();
  });
  it("shows the retention notice instead of a resume download for archived jobs", () => {
    render(
      <JobCard
        initial={job({ resume_status: "archived" })}
        now="2026-10-19T12:00:00Z"
        silentAfter={21}
      />,
    );
    expect(
      screen.getAllByText("Resume archived after 30 days").length,
    ).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: /Download/ })).toBeNull();
  });
});
