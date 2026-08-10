import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

/** Jellyfin 兼容接口经 Next rewrite 反代时，保留播放器实际连接的外部地址。 */
export function middleware(request: NextRequest) {
  const firstSegment = request.nextUrl.pathname.split("/")[1]?.toLowerCase();
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
    "sessions",
    "playingitems",
    "branding",
    "quickconnect",
    "emby",
  ]);
  if (!firstSegment || !jellyfinNamespaces.has(firstSegment)) {
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
