import { Brand } from "@/components/brand";
import { requirePageSession } from "@/lib/auth";
import { lastCheck } from "@/lib/jobs";
import { relativeTime } from "@/lib/model";
import { signOut } from "./actions";
export const dynamic = "force-dynamic";
export default async function PrivateLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { client } = await requirePageSession();
  let checked: string | null = null;
  try {
    checked = await lastCheck(client);
  } catch {
    /* Only the check indicator is optional; list errors remain visible. */
  }
  return (
    <>
      <header className="topbar">
        <div className="shell topbar-inner">
          <Brand />
          <div className="top-actions">
            <span className="last-check">
              {checked
                ? "Last check " +
                  relativeTime(checked, new Date().toISOString())
                : "Last check unavailable"}
            </span>
            <form action={signOut}>
              <button className="button small">Sign out</button>
            </form>
          </div>
        </div>
      </header>
      <main className="shell main">{children}</main>
    </>
  );
}
