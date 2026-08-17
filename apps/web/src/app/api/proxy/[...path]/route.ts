import { NextRequest, NextResponse } from "next/server";
import { api, ApiError } from "@/lib/api";

/**
 * Read-only proxy for client-side polling.
 *
 * Server Components cover the first paint, but a live dashboard has to refetch
 * from the browser — and the browser must never hold the platform credential.
 * This route runs on the server, adds the key, and returns the payload.
 *
 * **GET only.** A general-purpose passthrough that forwarded any method would
 * hand the browser the full authenticated API surface, which is exactly the
 * thing the BFF pattern exists to prevent. Mutations go through explicit Server
 * Actions that validate their own input.
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  const query = request.nextUrl.search;

  try {
    const data = await api.get(`/${path.join("/")}${query}`);
    return NextResponse.json(data);
  } catch (error) {
    if (error instanceof ApiError) {
      return NextResponse.json(
        { code: error.code, detail: error.message },
        { status: error.status },
      );
    }
    return NextResponse.json({ code: "upstream_error", detail: "API unreachable" }, { status: 502 });
  }
}
