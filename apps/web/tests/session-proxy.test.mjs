import assert from "node:assert/strict";
import fs from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";

const require = createRequire(import.meta.url);
const { NextRequest } = await import("next/server.js");

function loadOriginCheck() {
  const source = fs.readFileSync(
    new URL("../src/lib/request-origin.ts", import.meta.url), "utf8",
  );
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  });
  const exports = {};
  new Function("require", "exports", outputText)(require, exports);
  return exports;
}

function loadProxy(fetch, route = "../src/app/api/backend/[...path]/route.ts") {
  const source = fs.readFileSync(
    new URL(route, import.meta.url),
    "utf8",
  );
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  });
  const exports = {};
  const resolve = (name) => {
    if (name === "@/lib/request-origin") return loadOriginCheck();
    if (name === "@/lib/backend-proxy") return loadProxy(fetch, "../src/lib/backend-proxy.ts");
    return require(name);
  };
  new Function("require", "exports", "fetch", outputText)(resolve, exports, fetch);
  return exports;
}

function request(cookie) {
  return new NextRequest("http://localhost:3000/api/backend/api/v1/auth/me", {
    headers: cookie ? { cookie } : {},
  });
}

const context = { params: Promise.resolve({ path: ["api", "v1", "auth", "me"] }) };
// /auth/me is protected; it is not a token-issuing authentication action.
const protectedContext = {
  params: Promise.resolve({ path: ["api", "v1", "knowledge", "documents"] }),
};
const tokens = {
  access_token: "new-access",
  refresh_token: "new-refresh",
  expires_in: 900,
  token_type: "bearer",
};

for (const host of ["localhost:3000", "127.0.0.1:3000"]) {
  test(`allows same-origin mutations behind Docker for ${host}`, async () => {
    const proxy = loadProxy(async () => Response.json({ accepted: true }));
    const req = new NextRequest("http://0.0.0.0:3000/api/backend/api/v1/voice/calls", {
      method: "POST",
      headers: { host, origin: `http://${host}` },
    });
    assert.equal((await proxy.POST(req, protectedContext)).status, 200);
    const session = loadProxy(undefined, "../src/app/api/session/route.ts");
    const logout = new NextRequest("http://0.0.0.0:3000/api/session", {
      method: "DELETE", headers: { host, origin: `http://${host}` },
    });
    assert.equal((await session.DELETE(logout)).status, 200);
  });
}

for (const origin of [undefined, "null", "http://evil.example", "http://localhost:3001",
  "https://localhost:3000"]) {
  test(`rejects unsafe mutation origin ${origin}`, async () => {
    const proxy = loadProxy(async () => assert.fail("Must not contact upstream"));
    const headers = { host: "localhost:3000" };
    if (origin !== undefined) headers.origin = origin;
    const req = new NextRequest("http://0.0.0.0:3000/api/backend/api/v1/voice/calls", {
      method: "POST", headers,
    });
    assert.equal((await proxy.POST(req, protectedContext)).status, 403);
    const session = loadProxy(undefined, "../src/app/api/session/route.ts");
    const logout = new NextRequest("http://0.0.0.0:3000/api/session", {
      method: "DELETE", headers,
    });
    assert.equal((await session.DELETE(logout)).status, 403);
  });
}

test("refreshes when the browser has already removed the expired access cookie", async () => {
  let refreshCalls = 0;
  const proxy = loadProxy(async (url, init) => {
    if (url.endsWith("/auth/refresh")) {
      refreshCalls++;
      assert.deepEqual(JSON.parse(init.body), { refresh_token: "valid-refresh" });
      return Response.json(tokens);
    }
    return init.headers.get("authorization")
      ? Response.json({ documents: [] })
      : Response.json({ detail: "Invalid or expired access token" }, { status: 401 });
  });
  const response = await proxy.GET(request("resolveai_refresh=valid-refresh"), protectedContext);
  assert.equal(response.status, 200);
  assert.equal(refreshCalls, 1);
  assert.match(response.headers.get("set-cookie"), /resolveai_access=new-access/);
  assert.deepEqual(await response.json(), { documents: [] });
});

test("refreshes the current-user endpoint as well as other protected endpoints", async () => {
  const proxy = loadProxy(async (url, init) => {
    if (url.endsWith("/auth/refresh")) return Response.json(tokens);
    return init.headers.get("authorization")
      ? Response.json({ user: {} })
      : Response.json({ detail: "Invalid or expired access token" }, { status: 401 });
  });
  const response = await proxy.GET(request("resolveai_refresh=valid-refresh"), context);
  assert.equal(response.status, 200);
});

test("parallel requests share one single-use refresh token rotation", async () => {
  let refreshCalls = 0;
  const proxy = loadProxy(async (url, init) => {
    if (url.endsWith("/auth/refresh")) {
      refreshCalls++;
      await new Promise((resolve) => setTimeout(resolve, 10));
      return refreshCalls === 1
        ? Response.json(tokens)
        : Response.json({ detail: "Refresh token already used" }, { status: 401 });
    }
    return init.headers.get("authorization") === "Bearer new-access"
      ? Response.json({ documents: [] })
      : Response.json({ detail: "Invalid or expired access token" }, { status: 401 });
  });
  const responses = await Promise.all(
    Array.from({ length: 3 }, () =>
      proxy.GET(
        request("resolveai_access=expired; resolveai_refresh=valid-refresh"),
        protectedContext,
      ),
    ),
  );
  assert.deepEqual(responses.map((response) => response.status), [200, 200, 200]);
  assert.equal(refreshCalls, 1);
});

test("invalid refresh tokens return 401 and clear session cookies", async () => {
  const proxy = loadProxy(async () =>
    Response.json({ detail: "Invalid or expired token" }, { status: 401 }),
  );
  const response = await proxy.GET(request("resolveai_refresh=invalid"), protectedContext);
  assert.equal(response.status, 401);
  assert.match(response.headers.get("set-cookie"), /Max-Age=0/);
});

test("customer login sets only customer cookies and does not expose tokens", async () => {
  const proxy = loadProxy(
    async () => Response.json(tokens),
    "../src/app/api/customer-backend/[...path]/route.ts",
  );
  const req = new NextRequest("http://localhost:3000/api/customer-backend/api/v1/customer/auth/login", {
    method: "POST",
    headers: { host: "localhost:3000", origin: "http://localhost:3000" },
  });
  const response = await proxy.POST(req, {
    params: Promise.resolve({ path: ["api", "v1", "customer", "auth", "login"] }),
  });
  assert.equal(response.status, 200);
  const cookies = response.headers.get("set-cookie");
  assert.match(cookies, /resolveai_customer_access=new-access/);
  assert.match(cookies, /Path=\/api\/customer-backend/);
  assert.doesNotMatch(cookies, /resolveai_access=/);
  assert.deepEqual(await response.json(), { token_type: "bearer", expires_in: 900 });
});

test("customer registration forwards verification response without expecting tokens", async () => {
  const message = { message: "Check your email for a verification link." };
  const proxy = loadProxy(
    async () => Response.json(message, { status: 202 }),
    "../src/app/api/customer-backend/[...path]/route.ts",
  );
  const req = new NextRequest("http://localhost:3000/api/customer-backend/api/v1/customer/auth/register", {
    method: "POST",
    headers: { host: "localhost:3000", origin: "http://localhost:3000" },
  });
  const response = await proxy.POST(req, {
    params: Promise.resolve({ path: ["api", "v1", "customer", "auth", "register"] }),
  });
  assert.equal(response.status, 202);
  assert.equal(response.headers.get("set-cookie"), null);
  assert.deepEqual(await response.json(), message);
});
