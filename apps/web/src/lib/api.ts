export type ApiError = {
  detail?: string | { message?: string; [key: string]: unknown };
};

export class ApiRequestError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    const message =
      typeof detail === "string"
        ? detail
        : detail && typeof detail === "object" && "message" in detail
          ? String(detail.message)
          : `Request failed (${status})`;
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

async function requestApi<T>(basePath: string, path: string, init: RequestInit): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData) && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  const response = await fetch(`${basePath}${path}`, {
    ...init,
    headers,
    credentials: "same-origin",
    cache: "no-store",
  });
  const contentType = response.headers.get("content-type") ?? "";
  const data: unknown = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    throw new ApiRequestError(response.status, (data as ApiError)?.detail ?? data);
  }
  return data as T;
}

export function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  return requestApi("/api/backend", path, init);
}

export async function customerApi<T>(path: string, init: RequestInit = {}): Promise<T> {
  const timeout = AbortSignal.timeout(20_000);
  const signal = init.signal ? AbortSignal.any([init.signal, timeout]) : timeout;
  try {
    return await requestApi<T>("/api/customer-backend", path, { ...init, signal });
  } catch (error) {
    if (timeout.aborted && !(init.signal?.aborted)) {
      throw new Error(
        "The customer service took too long to respond. Refresh the page and try again.",
        { cause: error },
      );
    }
    throw error;
  }
}

export type Organization = { id: string; name: string; slug: string };
export type CurrentUser = {
  user: { id: string; email: string; full_name: string };
  organization: Organization;
  role: "admin" | "supervisor" | "support_agent" | "viewer";
  status: string;
};
export type Customer = {
  id: string;
  full_name: string;
  email: string;
  phone_number: string | null;
};
export type Order = {
  id: string;
  customer_id: string;
  order_number: string;
  status: string;
  total_amount: string;
  currency: string;
  created_at: string;
};
export type Ticket = {
  id: string;
  ticket_number: string;
  subject: string;
  status: string;
  priority: string;
  created_at: string;
};
export type Conversation = {
  id: string;
  customer_id: string | null;
  status: string;
  created_at: string;
  messages: { id: string; role: "user" | "assistant"; content: string; created_at: string }[];
};
export type Workflow = {
  id: string;
  status: string;
  error_code: string | null;
  created_at: string;
  completed_at: string | null;
  events: { id: string; event_type: string; payload: Record<string, unknown>; created_at: string }[];
};
export type KnowledgeVersion = {
  id: string;
  version_number: number;
  original_filename: string;
  byte_size: number;
  status: string;
  error_code: string | null;
  created_at: string;
  ingested_at: string | null;
};
export type KnowledgeDocument = {
  id: string;
  title: string;
  description: string | null;
  status: string;
  updated_at: string;
  versions: KnowledgeVersion[];
};
export type SearchResult = {
  answer: string;
  grounded: boolean;
  citations: {
    document_id: string;
    version_id: string;
    title: string;
    filename: string;
    excerpt: string | null;
  }[];
};
export type Membership = {
  user: { id: string; email: string; full_name: string };
  role: string;
  status: string;
};

export type BusinessAction = {
  id: string;
  order_id: string;
  kind: "cancellation" | "refund";
  status: "pending" | "succeeded" | "rejected";
  reason: string;
  requester_id: string;
  reviewer_id: string | null;
  decision_note: string | null;
  created_at: string;
};
