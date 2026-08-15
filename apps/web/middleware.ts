import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

/** Jellyfin 兼容接口经 Next rewrite 反代时，保留播放器实际连接的外部地址。 */
export function middleware(request: NextRequest) {
  const pathname = request.nextUrl.pathname.toLowerCase();
  const firstSegment = pathname.split("/")[1];
  const jellyfinNamespaces = new Set([
    "system",
    "users",
    "userviews",
    "useritems",
    "userplayeditems",
    "userfavoriteitems",
    "items",
    "videos",
    "shows",
    "playingitems",
    "branding",
    "quickconnect",
    "emby",
  ]);
  const jellyfinSessionPath =
    pathname === "/sessions" ||
    pathname === "/sessions/capabilities" ||
    pathname.startsWith("/sessions/capabilities/") ||
    pathname === "/sessions/playing" ||
    pathname.startsWith("/sessions/playing/");
  if (
    !firstSegment ||
    (!jellyfinNamespaces.has(firstSegment) && !jellyfinSessionPath)
  ) {
    return NextResponse.next();
  }

  const headers = new Headers(request.headers);
  headers.set("X-Forwarded-Host", request.headers.get("Host") ?? request.nextUrl.host);
  headers.set("X-Forwarded-Proto", request.nextUrl.protocol.replace(/:$/, ""));
  return NextResponse.next({ request: { headers } });
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
