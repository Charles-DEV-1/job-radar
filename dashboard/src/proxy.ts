import { createServerClient } from "@supabase/ssr";
import { NextRequest, NextResponse } from "next/server";
import { ownerEmail, publicConfig } from "@/lib/config";

export async function proxy(request: NextRequest) {
  let response = NextResponse.next({ request });
  const protect = request.nextUrl.pathname !== "/login";
  const denied = (status: number) => {
    const result = request.nextUrl.pathname.startsWith("/api/")
      ? NextResponse.json(
          {
            error:
              status === 503
                ? "Dashboard login is not configured"
                : "Authentication required",
          },
          { status },
        )
      : NextResponse.redirect(new URL("/login", request.url));
    response.cookies.getAll().forEach((cookie) => result.cookies.set(cookie));
    result.headers.set("Cache-Control", "private, no-store, max-age=0");
    return result;
  };
  // Login is the sole public page; private paths fail closed when setup is missing.
  if (!protect) {
    response.headers.set("Cache-Control", "private, no-store, max-age=0");
    return response;
  }
  try {
    const { url, key } = publicConfig();
    const allowed = ownerEmail();
    const supabase = createServerClient(url, key, {
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
        getAll: () => request.cookies.getAll(),
        setAll: (items) => {
          items.forEach(({ name, value }) => request.cookies.set(name, value));
          response = NextResponse.next({ request });
          items.forEach(({ name, value, options }) =>
            response.cookies.set(name, value, options),
          );
        },
      },
    });
    const { data, error } = await supabase.auth.getUser();
    if (error || !data.user) return denied(401);
    if (
      !data.user.email_confirmed_at ||
      data.user.email?.toLowerCase() !== allowed
    )
      return denied(403);
    response.headers.set("Cache-Control", "private, no-store, max-age=0");
    return response;
  } catch {
    return denied(503);
  }
}
export const config = {
  matcher: ["/((?!_next/static|_next/image|icon.svg|favicon.ico).*)"],
};
