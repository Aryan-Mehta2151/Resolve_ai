import { NextRequest } from "next/server";
import { proxyBackend } from "@/lib/backend-proxy";

type RouteContext = { params: Promise<{ path: string[] }> };
const proxy = (request: NextRequest, context: RouteContext) =>
  proxyBackend(request, context, "customer");

export const GET = proxy;
export const POST = proxy;
export const DELETE = proxy;
export const PATCH = proxy;
export const PUT = proxy;
