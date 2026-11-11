import { NextRequest, NextResponse } from "next/server";
import { isSameOrigin } from "@/lib/request-origin";

type SessionKind = "staff" | "customer";
type RouteContext = { params: Promise<{ path: string[] }> };
type AuthTokens = {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  token_type: string;
};

const sessionConfig = {
  staff: {
    accessCookie: "resolveai_access",
    refreshCookie: "resolveai_refresh",
    path: "/api/backend",
    refreshPath: ["api", "v1", "auth", "refresh"],
  },
  customer: {
    accessCookie: "resolveai_customer_access",
    refreshCookie: "resolveai_customer_refresh",
    path: "/api/customer-backend",
    refreshPath: ["api", "v1", "customer", "auth", "refresh"],
  },
} as const;

const sessionRotations = new Map<string, Promise<AuthTokens | null>>();

function cookieOptions(path: string, maxAge: number) {
  return {
    httpOnly: true,
    sameSite: "strict" as const,
    secure: process.env.NODE_ENV === "production",
    path,
    maxAge,
  };
}

function upstreamUrl(path: string[], search: string) {
  const base = (process.env.API_INTERNAL_BASE_URL ?? "http://localhost:8000").replace(/\/+$/, "");
  return `${base}/${path.map(encodeURIComponent).join("/")}${search}`;
}

function parseAuthTokens(value: unknown): AuthTokens | null {
  if (!value || typeof value !== "object") return null;
  const tokens = value as Record<string, unknown>;
  if (
    typeof tokens.access_token !== "string" ||
    typeof tokens.refresh_token !== "string" ||
    typeof tokens.expires_in !== "number" ||
    !Number.isFinite(tokens.expires_in) ||
    tokens.expires_in <= 0 ||
    typeof tokens.token_type !== "string"
  ) {
    return null;
  }
  return {
    access_token: tokens.access_token,
    refresh_token: tokens.refresh_token,
    expires_in: tokens.expires_in,
    token_type: tokens.token_type,
  };
}

function upstreamErrorCode(error: unknown): string {
  if (!(error instanceof Error)) return "unknown";
  const cause = error.cause as { code?: string } | undefined;
  return cause?.code ?? error.name;
}

const transientConnectionCodes = new Set([
  "ECONNRESET",
  "ECONNREFUSED",
  "EPIPE",
  "UND_ERR_SOCKET",
  "UND_ERR_CONNECT_TIMEOUT",
]);

async function sendUpstreamWithRetry(
  request: NextRequest,
  url: string,
  method: string,
  body: ArrayBuffer | undefined,
  accessToken: string | undefined,
): Promise<Response> {
  const idempotent = ["GET", "HEAD"].includes(method);
  for (let attempt = 1; ; attempt++) {
    try {
      return await sendUpstream(request, url, method, body, accessToken);
    } catch (error) {
      const code = upstreamErrorCode(error);
      // Non-idempotent requests only retry when the API never received them.
      const retryable = idempotent
        ? transientConnectionCodes.has(code)
        : code === "ECONNREFUSED";
      if (!retryable || attempt >= 3) throw error;
      await new Promise((resolve) => setTimeout(resolve, 250 * attempt));
    }
  }
}

async function sendUpstream(
  request: NextRequest,
  url: string,
  method: string,
  body: ArrayBuffer | undefined,
  accessToken: string | undefined,
): Promise<Response> {
  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  const accept = request.headers.get("accept");
  if (contentType) headers.set("content-type", contentType);
  if (accept) headers.set("accept", accept);
  if (accessToken) headers.set("authorization", `Bearer ${accessToken}`);
  return fetch(url, {
    method,
    headers,
    body,
    cache: "no-store",
    signal: AbortSignal.timeout(30_000),
  });
}

function setSessionCookies(response: NextResponse, tokens: AuthTokens, kind: SessionKind) {
  const config = sessionConfig[kind];
  response.cookies.set(
    config.accessCookie,
    tokens.access_token,
    cookieOptions(config.path, tokens.expires_in),
  );
  response.cookies.set(
    config.refreshCookie,
    tokens.refresh_token,
    cookieOptions(config.path, 7 * 24 * 60 * 60),
  );
}

function clearSessionCookies(response: NextResponse, kind: SessionKind) {
  const config = sessionConfig[kind];
  response.cookies.set(config.accessCookie, "", cookieOptions(config.path, 0));
  response.cookies.set(config.refreshCookie, "", cookieOptions(config.path, 0));
}

function makeResponse(upstream: Response, body: ArrayBuffer) {
  const headers = new Headers();
  const contentType = upstream.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  return new NextResponse(body, { status: upstream.status, headers });
}

async function requestSessionRotation(
  refreshToken: string,
  refreshPath: string[],
): Promise<AuthTokens | null> {
  const response = await fetch(upstreamUrl(refreshPath, ""), {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
    cache: "no-store",
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) return null;
  return parseAuthTokens(await response.json());
}

async function rotateSession(request: NextRequest, kind: SessionKind): Promise<AuthTokens | null> {
  const config = sessionConfig[kind];
  const refreshToken = request.cookies.get(config.refreshCookie)?.value;
  if (!refreshToken) return null;
  const key = `${kind}:${refreshToken}`;
  const pending = sessionRotations.get(key);
  if (pending) return pending;

  const rotation = requestSessionRotation(refreshToken, [...config.refreshPath]);
  sessionRotations.set(key, rotation);
  try {
    const tokens = await rotation;
    if (tokens) {
      setTimeout(() => sessionRotations.delete(key), 5_000).unref();
    } else {
      sessionRotations.delete(key);
    }
    return tokens;
  } catch (error) {
    sessionRotations.delete(key);
    throw error;
  }
}

function authActions(path: string[], kind: SessionKind): { action: boolean; tokens: boolean } {
  if (kind === "staff") {
    const action =
      path[0] === "api" &&
      path[1] === "v1" &&
      path[2] === "auth" &&
      ["login", "register", "refresh"].includes(path[3] ?? "");
    return { action, tokens: action };
  }
  const action =
    path[0] === "api" &&
    path[1] === "v1" &&
    path[2] === "customer" &&
    path[3] === "auth" &&
    ["login", "register", "verify", "refresh"].includes(path[4] ?? "");
  const tokens =
    action && ["login", "refresh"].includes(path[4] ?? "");
  return { action, tokens };
}

export async function proxyBackend(
  request: NextRequest,
  context: RouteContext,
  kind: SessionKind,
) {
  const method = request.method.toUpperCase();
  if (!["GET", "HEAD"].includes(method) && !isSameOrigin(request)) {
    return NextResponse.json({ detail: "Cross-origin requests are not allowed" }, { status: 403 });
  }

  const startedAt = Date.now();
  const { path } = await context.params;
  const url = upstreamUrl(path, request.nextUrl.search);
  const auth = authActions(path, kind);
  const accessToken = request.cookies.get(sessionConfig[kind].accessCookie)?.value;
  const body = ["GET", "HEAD"].includes(method) ? undefined : await request.arrayBuffer();

  let upstream: Response;
  try {
    upstream = await sendUpstreamWithRetry(request, url, method, body, accessToken);
    if (upstream.status === 401 && !auth.action) {
      const tokens = await rotateSession(request, kind);
      if (tokens) {
        upstream = await sendUpstream(request, url, method, body, tokens.access_token);
        const response = makeResponse(upstream, await upstream.arrayBuffer());
        if (upstream.status === 401) clearSessionCookies(response, kind);
        else setSessionCookies(response, tokens, kind);
        return response;
      }
    }
  } catch (error) {
    console.error("[backend-proxy] upstream request failed", {
      method,
      upstream: url,
      code: upstreamErrorCode(error),
      elapsedMs: Date.now() - startedAt,
    });
    return NextResponse.json(
      { detail: "ResolveAI API is unavailable. Check that the local stack is running." },
      { status: 502 },
    );
  }

  const responseBody = await upstream.arrayBuffer();
  if (upstream.ok && auth.tokens) {
    let tokens: AuthTokens | null = null;
    try {
      tokens = parseAuthTokens(JSON.parse(new TextDecoder().decode(responseBody)));
    } catch {
      tokens = null;
    }
    if (!tokens) {
      const invalidResponse = NextResponse.json(
        { detail: "Authentication service returned an invalid token response." },
        { status: 502 },
      );
      clearSessionCookies(invalidResponse, kind);
      return invalidResponse;
    }
    const sanitizedBody = new TextEncoder().encode(
      JSON.stringify({ token_type: tokens.token_type, expires_in: tokens.expires_in }),
    );
    const sanitizedResponse = new NextResponse(sanitizedBody, {
      status: upstream.status,
      headers: { "content-type": "application/json" },
    });
    setSessionCookies(sanitizedResponse, tokens, kind);
    return sanitizedResponse;
  }

  const response = makeResponse(upstream, responseBody);
  if (upstream.status === 401 && !auth.action) clearSessionCookies(response, kind);
  return response;
}
