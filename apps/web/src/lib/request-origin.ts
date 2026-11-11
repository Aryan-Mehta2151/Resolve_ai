import type { NextRequest } from "next/server";

export function isSameOrigin(request: NextRequest): boolean {
  const origin = request.headers.get("origin");
  const host = request.headers.get("host");
  if (!origin || !host) return false;
  try {
    const expected = new URL(`${request.nextUrl.protocol}//${host}`);
    return origin === expected.origin;
  } catch {
    return false;
  }
}
