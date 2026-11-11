import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import ts from "typescript";

function loadApi(fetch, AbortSignalClass = AbortSignal) {
  const source = fs.readFileSync(new URL("../src/lib/api.ts", import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  });
  const exports = {};
  new Function("exports", "fetch", "AbortSignal", outputText)(
    exports, fetch, AbortSignalClass,
  );
  return exports;
}

test("customer requests use a twenty-second deadline and the customer proxy", async () => {
  const api = loadApi(async (url, init) => {
    assert.equal(url, "/api/customer-backend/api/v1/customer/profile");
    assert.ok(init.signal instanceof AbortSignal);
    return Response.json({ full_name: "Test Customer" });
  }, {
    timeout(milliseconds) {
      assert.equal(milliseconds, 20_000);
      return AbortSignal.timeout(milliseconds);
    },
  });
  assert.deepEqual(await api.customerApi("/api/v1/customer/profile"), {
    full_name: "Test Customer",
  });
});

test("stalled customer requests surface a refreshable timeout error", async () => {
  const deadline = AbortSignal.abort(new DOMException("Timed out", "TimeoutError"));
  const api = loadApi(async (_url, init) => {
    init.signal.throwIfAborted();
    assert.fail("The expired request must abort");
  }, { timeout: () => deadline });
  await assert.rejects(api.customerApi("/api/v1/customer/profile"), {
    message: "The customer service took too long to respond. Refresh the page and try again.",
  });
});

test("customer requests preserve caller cancellation and authentication errors", async () => {
  const controller = new AbortController();
  controller.abort();
  const cancelledApi = loadApi(async (_url, init) => {
    init.signal.throwIfAborted();
    assert.fail("A cancelled request must abort");
  });
  await assert.rejects(
    cancelledApi.customerApi("/api/v1/customer/profile", { signal: controller.signal }),
    { name: "AbortError" },
  );
  const unauthorizedApi = loadApi(async () =>
    Response.json({ detail: "Invalid or expired access token" }, { status: 401 }),
  );
  await assert.rejects(unauthorizedApi.customerApi("/api/v1/customer/profile"), {
    status: 401,
    message: "Invalid or expired access token",
  });
});
