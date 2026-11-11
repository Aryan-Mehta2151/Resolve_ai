"use client";

import {
  type ChangeEvent,
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  api,
  ApiRequestError,
  type BusinessAction,
  type Conversation,
  type CurrentUser,
  type Customer,
  type KnowledgeDocument,
  type Membership,
  type Order,
  type SearchResult,
  type Ticket,
  type Workflow,
} from "@/lib/api";
import { VoicePanel } from "./voice-panel";

type PageName = "overview" | "inbox" | "voice" | "knowledge" | "orders" | "customers" | "tickets" | "team";
type AuthMode = "login" | "register";
type Readiness = { status: string; dependencies: Record<string, string> };

const navigation: { id: PageName; label: string; glyph: string }[] = [
  { id: "overview", label: "Overview", glyph: "◫" },
  { id: "inbox", label: "AI conversations", glyph: "◉" },
  { id: "voice", label: "Calling", glyph: "◉" },
  { id: "knowledge", label: "Knowledge base", glyph: "▤" },
  { id: "orders", label: "Orders", glyph: "▧" },
  { id: "customers", label: "Customers", glyph: "♙" },
  { id: "tickets", label: "Support tickets", glyph: "▱" },
  { id: "team", label: "Team", glyph: "♧" },
];

const roleLabels: Record<CurrentUser["role"], string> = {
  admin: "Admin",
  supervisor: "Supervisor",
  support_agent: "Support agent",
  viewer: "Viewer",
};

function messageFrom(error: unknown) {
  if (error instanceof ApiRequestError) {
    if (typeof error.detail === "string") return error.detail;
    if (error.detail && typeof error.detail === "object" && "message" in error.detail) {
      return String(error.detail.message);
    }
    return `The request could not be completed (${error.status}).`;
  }
  return error instanceof Error ? error.message : "Something went wrong. Please try again.";
}

function formatDate(value: string | null | undefined) {
  if (!value) return "—";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(value),
  );
}

function initials(name: string) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();
}

function statusClass(status: string) {
  const normalized = status.toLowerCase().replaceAll("_", "-");
  if (["ready", "completed", "delivered", "resolved", "open"].includes(normalized)) {
    return "status status-green";
  }
  if (["queued", "processing", "in-progress", "pending", "delayed", "deleting"].includes(normalized)) {
    return "status status-amber";
  }
  if (["failed", "cancelled", "closed"].includes(normalized)) return "status status-red";
  return "status status-neutral";
}

function ErrorNotice({ children, onDismiss }: { children: string; onDismiss: () => void }) {
  return (
    <div className="notice notice-error" role="alert">
      <span>{children}</span>
      <button className="notice-close" onClick={onDismiss} aria-label="Dismiss error">
        ×
      </button>
    </div>
  );
}

function AuthScreen({
  onAuthenticated,
  initialError,
  onDismissInitialError,
}: {
  onAuthenticated: () => Promise<void>;
  initialError: string;
  onDismissInitialError: () => void;
}) {
  const [mode, setMode] = useState<AuthMode>("login");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [form, setForm] = useState({
    organization_name: "",
    organization_slug: "",
    full_name: "",
    email: "",
    password: "",
  });

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const body =
        mode === "register"
          ? {
              organization_name: form.organization_name,
              organization_slug: form.organization_slug,
              full_name: form.full_name,
              email: form.email,
              password: form.password,
            }
          : { email: form.email, password: form.password };
      await api(`/api/v1/auth/${mode}`, { method: "POST", body: JSON.stringify(body) });
      await onAuthenticated();
    } catch (cause) {
      setError(messageFrom(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-shell">
      <div className="auth-left">
        <div className="brand brand-light">
          <span className="brand-mark">r</span>
          <span>resolve<span className="brand-accent">ai</span></span>
        </div>
        <div className="auth-story">
          <div className="eyebrow eyebrow-light"><span className="pulse-dot" /> CUSTOMER CARE, CONNECTED</div>
          <h1>Every conversation deserves a thoughtful resolution.</h1>
          <p>
            Bring customer context, trusted knowledge, and human support together in one
            workspace.
          </p>
          <div className="auth-proof">
            <span className="proof-icon">✳</span>
            <span>Grounded answers. Clear sources. Your team in control.</span>
          </div>
        </div>
        <div className="auth-footnote">A calmer way to support your customers.</div>
      </div>
      <div className="auth-right">
        <section className="auth-card">
          <div className="auth-kicker">YOUR SUPPORT WORKSPACE</div>
          <h2>{mode === "login" ? "Welcome back" : "Create your workspace"}</h2>
          <p className="muted">
            {mode === "login"
              ? "Sign in to continue where your team left off."
              : "Start a local demo workspace with an Admin account."}
          </p>
          <div className="auth-tabs" role="tablist" aria-label="Authentication">
            <button
              className={mode === "login" ? "auth-tab active" : "auth-tab"}
              type="button"
              role="tab"
              aria-selected={mode === "login"}
              onClick={() => {
                setMode("login");
                setError("");
              }}
            >
              Sign in
            </button>
            <button
              className={mode === "register" ? "auth-tab active" : "auth-tab"}
              type="button"
              role="tab"
              aria-selected={mode === "register"}
              onClick={() => {
                setMode("register");
                setError("");
              }}
            >
              New workspace
            </button>
          </div>
          {(error || initialError) && (
            <ErrorNotice
              onDismiss={() => {
                setError("");
                onDismissInitialError();
              }}
            >
              {error || initialError}
            </ErrorNotice>
          )}
          <form className="form-stack" onSubmit={submit}>
            {mode === "register" && (
              <>
                <label className="field">
                  <span>Your name</span>
                  <input
                    autoComplete="name"
                    required
                    maxLength={120}
                    value={form.full_name}
                    onChange={(event) => setForm({ ...form, full_name: event.target.value })}
                    placeholder="Alex Morgan"
                  />
                </label>
                <label className="field">
                  <span>Organization name</span>
                  <input
                    required
                    minLength={2}
                    maxLength={120}
                    value={form.organization_name}
                    onChange={(event) =>
                      setForm({ ...form, organization_name: event.target.value })
                    }
                    placeholder="Acme Support"
                  />
                </label>
                <label className="field">
                  <span>Workspace URL name</span>
                  <input
                    required
                    minLength={3}
                    maxLength={63}
                    pattern="[a-z0-9]+(?:-[a-z0-9]+)*"
                    value={form.organization_slug}
                    onChange={(event) =>
                      setForm({
                        ...form,
                        organization_slug: event.target.value.toLowerCase().replace(/\s+/g, "-"),
                      })
                    }
                    placeholder="acme-support"
                  />
                </label>
              </>
            )}
            <label className="field">
              <span>Work email</span>
              <input
                type="email"
                autoComplete="email"
                required
                value={form.email}
                onChange={(event) => setForm({ ...form, email: event.target.value })}
                placeholder="you@company.com"
              />
            </label>
            <label className="field">
              <span>Password</span>
              <input
                type="password"
                autoComplete={mode === "login" ? "current-password" : "new-password"}
                minLength={12}
                maxLength={128}
                required
                value={form.password}
                onChange={(event) => setForm({ ...form, password: event.target.value })}
                placeholder="At least 12 characters"
              />
              {mode === "register" && (
                <small className="field-hint">Use 12 or more characters.</small>
              )}
            </label>
            <button className="button button-primary button-wide" disabled={busy} type="submit">
              {busy ? "Connecting…" : mode === "login" ? "Sign in to workspace" : "Create workspace"}
              {!busy && <span aria-hidden="true">→</span>}
            </button>
          </form>
          <p className="auth-privacy">
            Your organization’s records are isolated from other workspaces.
          </p>
        </section>
      </div>
    </main>
  );
}

export default function Home() {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [authError, setAuthError] = useState("");
  const [page, setPage] = useState<PageName>("overview");
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [businessActions, setBusinessActions] = useState<BusinessAction[]>([]);
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [readiness, setReadiness] = useState<Readiness>({
    status: "checking",
    dependencies: { api: "checking" },
  });
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [knowledgeHistory, setKnowledgeHistory] = useState<KnowledgeDocument | null>(null);
  const [members, setMembers] = useState<Membership[]>([]);
  const [conversationId, setConversationId] = useState("");
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [escalations, setEscalations] = useState<Conversation[]>([]);
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [selectedOrderId, setSelectedOrderId] = useState("");
  const [selectedCustomerId, setSelectedCustomerId] = useState("");
  const [orderDetails, setOrderDetails] = useState<{
    shipments: { tracking_number: string; carrier: string; status: string; estimated_delivery_at: string | null }[];
    payments: { status: string; amount: string; currency: string; refunds: { status: string; amount: string }[] }[];
  } | null>(null);
  const [searchResult, setSearchResult] = useState<SearchResult | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [chatMessage, setChatMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadingData, setLoadingData] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [authTick, setAuthTick] = useState(0);

  const hasKnowledgeEdit = user?.role === "admin" || user?.role === "supervisor";
  const isAdmin = user?.role === "admin";
  const activeTitle = navigation.find((item) => item.id === page)?.label ?? "Overview";

  const loadSection = useCallback(
    async (selectedPage: PageName, profile: CurrentUser) => {
      setLoadingData(true);
      setError("");
      const tasks: Promise<void>[] = [];
      if (selectedPage === "overview" || selectedPage === "orders") {
        tasks.push(api<Order[]>("/api/v1/orders?limit=100").then(setOrders));
      }
      if (selectedPage === "orders") {
        tasks.push(api<BusinessAction[]>("/api/v1/business-actions").then(setBusinessActions));
      }
      if (selectedPage === "inbox") {
        tasks.push(api<Conversation[]>("/api/v1/conversations?status=escalated").then(setEscalations));
      }
      if (
        selectedPage === "overview" ||
        selectedPage === "customers" ||
        selectedPage === "inbox" ||
        selectedPage === "tickets"
      ) {
        tasks.push(api<Customer[]>("/api/v1/customers?limit=100").then(setCustomers));
      }
      if (selectedPage === "overview" || selectedPage === "tickets") {
        tasks.push(api<Ticket[]>("/api/v1/support/tickets?limit=100").then(setTickets));
      }
      if (selectedPage === "knowledge") {
        tasks.push(api<KnowledgeDocument[]>("/api/v1/knowledge/documents?limit=100").then(setDocuments));
      }
      if (selectedPage === "team" && profile.role === "admin") {
        tasks.push(
          api<Membership[]>(`/api/v1/organizations/${profile.organization.id}/memberships`).then(
            setMembers,
          ),
        );
      }
      const results = await Promise.allSettled(tasks);
      const failed = results.find((result) => result.status === "rejected");
      if (failed?.status === "rejected") {
        setError(messageFrom(failed.reason));
      }
      setLoadingData(false);
    },
    [],
  );

  const loadReadiness = useCallback(async () => {
    try {
      const result = await api<Readiness>("/health/ready");
      setReadiness({ ...result, dependencies: { api: "ok", ...result.dependencies } });
    } catch (cause) {
      if (cause instanceof ApiRequestError && cause.detail && typeof cause.detail === "object") {
        const detail = cause.detail as { status?: string; dependencies?: Record<string, string> };
        if (detail.dependencies) {
          setReadiness({
            status: detail.status ?? "not_ready",
            dependencies: { api: "ok", ...detail.dependencies },
          });
          return;
        }
      }
      setReadiness({ status: "unavailable", dependencies: { api: "unavailable" } });
    }
  }, []);

  useEffect(() => {
    let alive = true;
    void api<CurrentUser>("/api/v1/auth/me")
      .then((profile) => {
        if (!alive) return;
        setUser(profile);
        setAuthError("");
        void loadSection("overview", profile);
        void loadReadiness();
        const savedConversation = window.sessionStorage.getItem("resolveai-active-conversation");
        if (savedConversation) {
          setConversationId(savedConversation);
          void api<Conversation>(`/api/v1/conversations/${savedConversation}`)
            .then((result) => {
              if (alive) {
                setConversation(result);
                setSelectedCustomerId(result.customer_id ?? "");
              }
            })
            .catch(() => window.sessionStorage.removeItem("resolveai-active-conversation"));
        }
        setAuthLoading(false);
      })
      .catch((cause: unknown) => {
        if (alive) {
          setUser(null);
          setAuthError(
            cause instanceof ApiRequestError && cause.status === 401 ? "" : messageFrom(cause),
          );
          setAuthLoading(false);
        }
      });
    return () => {
      alive = false;
    };
  }, [authTick, loadReadiness, loadSection]);

  const visibleOrders = useMemo(() => {
    const term = query.trim().toLowerCase();
    return term
      ? orders.filter((order) => order.order_number.toLowerCase().includes(term))
      : orders;
  }, [orders, query]);
  const visibleCustomers = useMemo(() => {
    const term = query.trim().toLowerCase();
    return term
      ? customers.filter(
          (customer) =>
            customer.full_name.toLowerCase().includes(term) ||
            customer.email.toLowerCase().includes(term),
        )
      : customers;
  }, [customers, query]);

  async function signOut() {
    await fetch("/api/session", { method: "DELETE", credentials: "same-origin" });
    setUser(null);
    setConversation(null);
    setConversationId("");
    setPage("overview");
    window.sessionStorage.removeItem("resolveai-active-conversation");
  }

  async function choosePage(nextPage: PageName) {
    setPage(nextPage);
    setQuery("");
    setError("");
    setNotice("");
    setMobileNavOpen(false);
    if (user) {
      await loadSection(nextPage, user);
      if (nextPage === "overview") void loadReadiness();
    }
  }

  async function refreshPageData() {
    if (user) await loadSection(page, user);
  }

  async function createCustomer(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const customer = await api<Customer>("/api/v1/customers", {
        method: "POST",
        body: JSON.stringify({
          full_name: data.get("full_name"),
          email: data.get("email"),
          phone_number: data.get("phone_number") || null,
        }),
      });
      form.reset();
      setSelectedCustomerId(customer.id);
      setNotice("Customer profile created. They can now create an account using this email.");
      if (user) await loadSection("customers", user);
    } catch (cause) {
      setError(messageFrom(cause));
    } finally {
      setBusy(false);
    }
  }

  async function submitBusinessAction(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const action = await api<BusinessAction>(`/api/v1/orders/${selectedOrderId}/actions`, {
        method: "POST",
        body: JSON.stringify({
          kind: data.get("kind"), reason: data.get("reason"),
          confirmed: data.get("confirmed") === "on",
        }),
      });
      setNotice(action.status === "pending"
        ? "Mock refund requested. An Admin or Supervisor must approve it. No money moved."
        : `Demo action is ${action.status}. No real money moved.`);
      setSelectedOrderId("");
      if (user) await loadSection("orders", user);
    } catch (cause) {
      setError(messageFrom(cause));
    } finally {
      setBusy(false);
    }
  }

  async function reviewBusinessAction(
    event: FormEvent<HTMLFormElement>, actionId: string,
  ) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const submitter = (event.nativeEvent as SubmitEvent).submitter;
    if (!(submitter instanceof HTMLButtonElement)) return;
    setBusy(true);
    setError("");
    try {
      await api<BusinessAction>(`/api/v1/business-actions/${actionId}/decision`, {
        method: "POST",
        body: JSON.stringify({ approve: submitter.value === "approve", note: data.get("note") }),
      });
      setNotice("Mock refund decision saved. No real money moved.");
      if (user) await loadSection("orders", user);
    } catch (cause) {
      setError(messageFrom(cause));
    } finally {
      setBusy(false);
    }
  }

  async function startConversation() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const created = await api<Conversation>("/api/v1/conversations", {
        method: "POST",
        body: JSON.stringify({ customer_id: selectedCustomerId || null }),
      });
      setConversation(created);
      setConversationId(created.id);
      setWorkflow(null);
      window.sessionStorage.setItem("resolveai-active-conversation", created.id);
      setNotice("Conversation started. Ask about an order, refund status, or support issue.");
    } catch (cause) {
      setError(messageFrom(cause));
    } finally {
      setBusy(false);
    }
  }

  async function reviewEscalation(event: FormEvent<HTMLFormElement>, id: string) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setBusy(true);
    setError("");
    try {
      await api<Conversation>(`/api/v1/conversations/${id}/review`, {
        method: "POST",
        body: JSON.stringify({ status: data.get("status"), note: data.get("note") }),
      });
      setEscalations(await api<Conversation[]>("/api/v1/conversations?status=escalated"));
      if (id === conversationId) {
        setConversation(await api<Conversation>(`/api/v1/conversations/${id}`));
      }
      setNotice("Support review saved. No phone transfer or notification was sent.");
    } catch (cause) {
      setError(messageFrom(cause));
    } finally {
      setBusy(false);
    }
  }

  async function openEscalation(id: string) {
    setBusy(true);
    setError("");
    try {
      const detail = await api<Conversation>(`/api/v1/conversations/${id}`);
      setConversation(detail);
      setConversationId(detail.id);
      setSelectedCustomerId(detail.customer_id ?? "");
      setWorkflow(null);
      window.sessionStorage.setItem("resolveai-active-conversation", detail.id);
    } catch (cause) {
      setError(messageFrom(cause));
    } finally {
      setBusy(false);
    }
  }

  async function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = chatMessage.trim();
    if (!content || !conversationId || busy) return;
    if (conversation && (conversation.customer_id ?? "") !== selectedCustomerId) {
      setError(
        'The customer selection is for a new conversation. Click "Start a new conversation" to use it; your current chat has not changed.',
      );
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    setChatMessage("");
    try {
      const accepted = await api<{ id: string }>(
        `/api/v1/conversations/${conversationId}/messages`,
        { method: "POST", body: JSON.stringify({ content }) },
      );
      const pendingMessage = {
        id: `local-${Date.now()}`,
        role: "user" as const,
        content,
        created_at: new Date().toISOString(),
      };
      setConversation((current) =>
        current ? { ...current, messages: [...current.messages, pendingMessage] } : current,
      );
      let lastWorkflow: Workflow | null = null;
      for (let attempt = 0; attempt < 30; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1200));
        const [updatedWorkflow, updatedConversation] = await Promise.all([
          api<Workflow>(`/api/v1/workflows/${accepted.id}`),
          api<Conversation>(`/api/v1/conversations/${conversationId}`),
        ]);
        lastWorkflow = updatedWorkflow;
        setWorkflow(updatedWorkflow);
        setConversation(updatedConversation);
        if (["completed", "failed"].includes(updatedWorkflow.status)) break;
      }
      if (lastWorkflow?.status === "completed") {
        await api<Order[]>("/api/v1/orders?limit=100").then(setOrders);
        setEscalations(await api<Conversation[]>("/api/v1/conversations?status=escalated"));
        setNotice("Reply ready.");
      } else if (lastWorkflow?.status === "failed") {
        setError("The workflow could not finish. Check that the worker and its providers are ready.");
      } else {
        setNotice("The reply is still processing. You can keep this conversation open.");
      }
    } catch (cause) {
      setError(messageFrom(cause));
      setChatMessage(content);
    } finally {
      setBusy(false);
    }
  }

  async function searchKnowledge(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!searchQuery.trim()) return;
    setBusy(true);
    setError("");
    setSearchResult(null);
    try {
      const result = await api<SearchResult>("/api/v1/knowledge/search", {
        method: "POST",
        body: JSON.stringify({ query: searchQuery.trim() }),
      });
      setSearchResult(result);
    } catch (cause) {
      setError(messageFrom(cause));
    } finally {
      setBusy(false);
    }
  }

  async function uploadKnowledge(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const formData = new FormData(form);
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api("/api/v1/knowledge/documents", { method: "POST", body: formData });
      form.reset();
      setNotice("Document uploaded. Ingestion is running in the background.");
      await loadSection("knowledge", user!);
    } catch (cause) {
      setError(messageFrom(cause));
    } finally {
      setBusy(false);
    }
  }

  async function uploadVersion(event: ChangeEvent<HTMLInputElement>, document: KnowledgeDocument) {
    const file = event.target.files?.[0];
    if (!file) return;
    const formData = new FormData();
    formData.set("file", file);
    setBusy(true);
    setError("");
    try {
      await api(`/api/v1/knowledge/documents/${document.id}/versions`, {
        method: "POST",
        body: formData,
      });
      setNotice(`Version uploaded for ${document.title}.`);
      await loadSection("knowledge", user!);
    } catch (cause) {
      setError(messageFrom(cause));
    } finally {
      setBusy(false);
      event.target.value = "";
    }
  }

  async function retryVersion(document: KnowledgeDocument, versionId: string) {
    setBusy(true);
    setError("");
    try {
      await api(`/api/v1/knowledge/documents/${document.id}/versions/${versionId}/retry`, {
        method: "POST",
      });
      setNotice("Ingestion retry queued.");
      await loadSection("knowledge", user!);
    } catch (cause) {
      setError(messageFrom(cause));
    } finally {
      setBusy(false);
    }
  }

  async function openKnowledgeHistory(document: KnowledgeDocument) {
    setBusy(true);
    setError("");
    try {
      const details = await api<KnowledgeDocument>(
        `/api/v1/knowledge/documents/${document.id}?version_limit=100`,
      );
      setKnowledgeHistory(details);
    } catch (cause) {
      setError(messageFrom(cause));
    } finally {
      setBusy(false);
    }
  }

  async function deleteKnowledge(document: KnowledgeDocument) {
    if (!window.confirm(`Delete "${document.title}" and all its versions?`)) return;
    setBusy(true);
    setError("");
    try {
      await api(`/api/v1/knowledge/documents/${document.id}`, { method: "DELETE" });
      setNotice("Document deletion queued.");
      await loadSection("knowledge", user!);
    } catch (cause) {
      setError(messageFrom(cause));
    } finally {
      setBusy(false);
    }
  }

  async function openOrder(order: Order) {
    setSelectedOrderId(order.id);
    setOrderDetails(null);
    setError("");
    try {
      const [shipments, payments] = await Promise.all([
        api<NonNullable<typeof orderDetails>["shipments"]>(`/api/v1/orders/${order.id}/shipments`),
        api<NonNullable<typeof orderDetails>["payments"]>(`/api/v1/orders/${order.id}/payments`),
      ]);
      setOrderDetails({ shipments, payments });
    } catch (cause) {
      setError(messageFrom(cause));
    }
  }

  async function viewCustomer(customer: Customer) {
    setSelectedCustomerId(customer.id);
    setError("");
    try {
      await api<Order[]>(`/api/v1/customers/${customer.id}/orders?limit=100`).then(setOrders);
    } catch (cause) {
      setError(messageFrom(cause));
    }
  }

  async function createTeamMember(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!user) return;
    const form = event.currentTarget;
    const formData = new FormData(form);
    setBusy(true);
    setError("");
    try {
      await api(`/api/v1/organizations/${user.organization.id}/memberships`, {
        method: "POST",
        body: JSON.stringify({
          full_name: formData.get("full_name"),
          email: formData.get("email"),
          password: formData.get("password"),
          role: formData.get("role"),
        }),
      });
      form.reset();
      setNotice("Team member added.");
      await loadSection("team", user);
    } catch (cause) {
      setError(messageFrom(cause));
    } finally {
      setBusy(false);
    }
  }

  if (authLoading) {
    return (
      <main className="loading-screen">
        <div className="loading-brand"><span className="brand-mark">r</span> resolveai</div>
        <div className="spinner" />
        <p>Opening your workspace…</p>
      </main>
    );
  }
  if (!user) {
    return (
      <AuthScreen
        initialError={authError}
        onDismissInitialError={() => setAuthError("")}
        onAuthenticated={async () => {
          setAuthError("");
          setAuthLoading(true);
          setAuthTick((value) => value + 1);
        }}
      />
    );
  }

  const currentPageTitle = activeTitle;

  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileNavOpen ? "sidebar-open" : ""}`}>
        <button className="brand sidebar-brand" onClick={() => void choosePage("overview")}>
          <span className="brand-mark">r</span>
          <span>resolve<span className="brand-accent">ai</span></span>
          <span className="brand-beta">DEMO</span>
        </button>
        <div className="workspace-switcher">
          <span className="workspace-avatar">{initials(user.organization.name)}</span>
          <span className="workspace-name">
            <strong>{user.organization.name}</strong>
            <small>Support workspace</small>
          </span>
          <span className="chevron">⌄</span>
        </div>
        <div className="nav-label">WORKSPACE</div>
        <nav className="side-nav" aria-label="Main navigation">
          {navigation.filter((item) => item.id !== "team" || isAdmin).map((item) => (
            <button
              key={item.id}
              className={`nav-item ${page === item.id ? "nav-item-active" : ""}`}
              onClick={() => void choosePage(item.id)}
              aria-current={page === item.id ? "page" : undefined}
            >
              <span className="nav-glyph" aria-hidden="true">{item.glyph}</span>
              <span>{item.label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="module-card">
            <div className="module-card-top"><span className="module-spark">✳</span> DEMO STATUS</div>
            <strong>Modules 1–7 ready</strong>
            <span>Connected support workspace</span>
            <button onClick={() => void choosePage("overview")}>Explore what’s live <span>→</span></button>
          </div>
          <div className="profile-row">
            <div className="profile-avatar">{initials(user.user.full_name)}</div>
            <div className="profile-text"><strong>{user.user.full_name}</strong><small>{roleLabels[user.role]}</small></div>
            <button className="icon-button profile-menu" onClick={() => void signOut()} title="Sign out" aria-label="Sign out">↗</button>
          </div>
        </div>
      </aside>
      {mobileNavOpen && <button className="mobile-scrim" aria-label="Close navigation" onClick={() => setMobileNavOpen(false)} />}

      <main className="main-area">
        <header className="topbar">
          <div className="topbar-title-wrap">
            <button className="mobile-menu icon-button" onClick={() => setMobileNavOpen((open) => !open)} aria-label="Toggle navigation">☰</button>
            <div className="breadcrumbs"><span>Workspace</span><span className="crumb-separator">/</span><strong>{currentPageTitle}</strong></div>
          </div>
          <div className="topbar-actions">
            <a className="docs-link" href="http://localhost:8000/docs" target="_blank" rel="noreferrer">API reference <span>↗</span></a>
            <span className="topbar-divider" />
            <div className="topbar-user"><span className="online-dot" /><span>{roleLabels[user.role]}</span></div>
            <button className="topbar-avatar" title="Sign out" onClick={() => void signOut()}>{initials(user.user.full_name)}</button>
          </div>
        </header>

        <div className="page-content">
          {error && <ErrorNotice onDismiss={() => setError("")}>{error}</ErrorNotice>}
          {notice && (
            <div className="notice notice-success" role="status">
              <span>{notice}</span>
              <button className="notice-close" onClick={() => setNotice("")} aria-label="Dismiss message">×</button>
            </div>
          )}
          {loadingData && <div className="loading-line"><span className="spinner spinner-small" /> Syncing workspace data…</div>}

          {page === "overview" && (
            <Overview
              user={user}
              customers={customers}
              orders={orders}
              tickets={tickets}
              readiness={readiness}
              onNavigate={(name) => void choosePage(name)}
              onRefresh={() => void refreshPageData()}
              onOpenOrder={(order) => {
                void openOrder(order);
                void choosePage("orders");
              }}
            />
          )}

          {page === "voice" && <VoicePanel customers={customers} canCall={user.role !== "viewer"} />}

          {page === "inbox" && (
            <section>
              <PageHeading
                eyebrow="ASSISTED SUPPORT"
                title="Conversations"
                description="Ask a support question and watch the workflow resolve with tenant-scoped tools and grounded knowledge."
                action={
                  <button className="button button-primary" disabled={busy} onClick={() => void startConversation()}>
                    <span>＋</span> New conversation
                  </button>
                }
              />
              <div className="conversation-layout">
                <aside className="conversation-setup card">
                  <div className="card-heading"><div><span className="heading-icon icon-violet">✳</span><div><h3>Start a conversation</h3><p>Choose a customer for orders and tickets.</p></div></div></div>
                  <label className="field">
                    <span>Customer for new conversation <em>optional</em></span>
                    <select value={selectedCustomerId} onChange={(event) => setSelectedCustomerId(event.target.value)}>
                      <option value="">No customer attached</option>
                      {customers.map((customer) => <option key={customer.id} value={customer.id}>{customer.full_name} · {customer.email}</option>)}
                    </select>
                  </label>
                  <p className="small-note">Selecting a customer does not change an existing chat. Start a new conversation below to apply your selection.</p>
                  <button className="button button-secondary button-wide" disabled={busy} onClick={() => void startConversation()}>
                    {conversationId ? "Start a new conversation" : "Start conversation"}
                  </button>
                  <div className="divider" />
                  <div className="mini-label">TRY A DEMO QUESTION</div>
                  <div className="prompt-list">
                    {["List products", "Buy 2 AUDIO-101", "Where is order ORD-10045?", "request refund ORD-10045", "What is the return policy?", "Please let me speak to a human agent", "Please create a support ticket for this issue."].map((prompt) => (
                      <button key={prompt} onClick={() => setChatMessage(prompt)}>{prompt}<span>↗</span></button>
                    ))}
                  </div>
                  <p className="small-note">Seed demo commerce data first. To order, attach a customer, choose a product, review the summary, then say &quot;confirm order&quot;. Demo only; no real payment is charged.</p>
                  <div className="divider" />
                  <h3>Needs human review ({escalations.length})</h3>
                  <p className="small-note">An internal support queue, not a live transfer. Refund approvals are in Orders.</p>
                  <button className="button button-quiet" disabled={busy} onClick={() => void refreshPageData()}>Refresh queue</button>
                  {escalations.map((item) => (
                    <div className="detail-block" key={item.id}>
                      <button className="row-link" disabled={busy} onClick={() => void openEscalation(item.id)}>Open conversation {item.id.slice(0, 8)}</button>
                      {user.role !== "viewer" && <form onSubmit={(event) => void reviewEscalation(event, item.id)}>
                        <label className="field"><span>Review note</span><input name="note" required minLength={3} maxLength={240} /></label>
                        <label className="field"><span>Outcome</span><select name="status"><option value="open">Return to assistant</option><option value="resolved">Mark resolved</option></select></label>
                        <button className="button button-secondary" disabled={busy}>Save review</button>
                      </form>}
                    </div>
                  ))}
                </aside>
                <section className="chat-panel card">
                  <div className="chat-header">
                    <div><span className="chat-avatar">✳</span><div><strong>{conversation ? "Support assistant" : "Your support assistant"}</strong><small>{conversation ? `Conversation ${conversation.id.slice(0, 8)} · ${conversation.status}` : "Start a conversation to begin"}</small></div></div>
                    {workflow && <span className={statusClass(workflow.status)}>{workflow.status}</span>}
                  </div>
                  {conversation && <p className="small-note">Customer attached to this chat: <strong>{conversation.customer_id ? customers.find((customer) => customer.id === conversation.customer_id)?.full_name ?? "Linked customer" : "None"}</strong></p>}
                  <div className="chat-messages">
                    {!conversation && (
                      <div className="chat-empty">
                        <div className="empty-orbit"><span>✳</span></div>
                        <h3>What can we help with?</h3>
                        <p>Ask about order status, shipment updates, refund status, or your support policies.</p>
                      </div>
                    )}
                    {conversation?.messages.map((message) => (
                      <div key={message.id} className={`chat-message ${message.role === "user" ? "message-user" : "message-assistant"}`}>
                        <div className="message-avatar">{message.role === "user" ? initials(user.user.full_name) : "✳"}</div>
                        <div><div className="message-meta">{message.role === "user" ? "You" : "ResolveAI"} <span>{formatDate(message.created_at)}</span></div><div className="message-bubble">{message.content}</div></div>
                      </div>
                    ))}
                    {busy && page === "inbox" && <div className="assistant-thinking"><span className="typing-dots"><i /><i /><i /></span> Working on a grounded response…</div>}
                  </div>
                  <form className="chat-composer" onSubmit={(event) => void sendMessage(event)}>
                    <input value={chatMessage} onChange={(event) => setChatMessage(event.target.value)} placeholder={conversationId ? "Ask a question about an order or policy…" : "Start a conversation first"} disabled={!conversationId || busy} maxLength={4000} />
                    <button type="submit" aria-label="Send message" disabled={!conversationId || busy || !chatMessage.trim()}>↑</button>
                  </form>
                  <div className="chat-disclaimer">AI responses are based on available demo data and approved knowledge documents.</div>
                </section>
              </div>
              {workflow && (
                <div className="workflow-strip card">
                  <div className="workflow-title"><span className={statusClass(workflow.status)}>{workflow.status}</span><span>Workflow {workflow.id.slice(0, 8)}</span></div>
                  <div className="workflow-events">
                    {workflow.events.map((item) => <span key={item.id}><i />{item.event_type.replaceAll(".", " ")}</span>)}
                  </div>
                </div>
              )}
            </section>
          )}

          {page === "knowledge" && (
            <KnowledgePage
              documents={documents}
              canEdit={Boolean(hasKnowledgeEdit)}
              busy={busy}
              searchQuery={searchQuery}
              searchResult={searchResult}
              history={knowledgeHistory}
              setSearchQuery={setSearchQuery}
              onSearch={(event) => void searchKnowledge(event)}
              onUpload={(event) => void uploadKnowledge(event)}
              onUploadVersion={(event, document) => void uploadVersion(event, document)}
              onRetry={(document, id) => void retryVersion(document, id)}
              onDelete={(document) => void deleteKnowledge(document)}
              onOpenHistory={(document) => void openKnowledgeHistory(document)}
              onCloseHistory={() => setKnowledgeHistory(null)}
            />
          )}

          {page === "orders" && (
            <section>
              <PageHeading eyebrow="COMMERCE" title="Orders" description="Inspect mock orders, request cancellations or refunds, and review approvals. No real money moves." />
              <div className="toolbar"><div className="searchbox"><span>⌕</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search order number…" /></div><button className="button button-quiet" onClick={() => void refreshPageData()}>↻ Refresh</button></div>
              <div className="card table-card">
                <TableHeader title="Recent orders" count={visibleOrders.length} />
                {visibleOrders.length ? (
                  <div className="table-scroll"><table><thead><tr><th>Order</th><th>Status</th><th>Amount</th><th>Placed</th><th /></tr></thead><tbody>
                    {visibleOrders.map((order) => <tr key={order.id} className="clickable-row" onClick={() => void openOrder(order)}><td><strong>{order.order_number}</strong><small>Order ID · {order.id.slice(0, 8)}</small></td><td><span className={statusClass(order.status)}>{order.status.replaceAll("_", " ")}</span></td><td>{order.currency} {Number(order.total_amount).toFixed(2)}</td><td>{formatDate(order.created_at)}</td><td><button className="row-link" onClick={(event) => { event.stopPropagation(); void openOrder(order); }}>Details →</button></td></tr>)}
                  </tbody></table></div>
                ) : <EmptyState title="No orders to show" body="Seed the demo commerce data for this organization to explore orders." />}
              </div>
              <div className="card table-card">
                <TableHeader title="Business actions · demo only" count={businessActions.length} />
                {businessActions.length ? businessActions.map((action) => (
                  <div className="detail-block" key={action.id}>
                    <h3>{action.kind} · {orders.find((order) => order.id === action.order_id)?.order_number ?? "Order"}</h3>
                    <span className={statusClass(action.status)}>{action.status}</span>
                    <p>{action.reason}</p>
                    {action.decision_note && <p>Decision: {action.decision_note}</p>}
                    <p className="small-note">Requested {formatDate(action.created_at)} · Audit includes requester and reviewer. Mock action only.</p>
                    {action.status === "pending" && hasKnowledgeEdit && (
                      <form onSubmit={(event) => void reviewBusinessAction(event, action.id)}>
                        <label className="field"><span>Decision note</span><input name="note" required minLength={3} maxLength={240} placeholder="Reason for this decision" /></label>
                        <button className="button button-primary" type="submit" value="approve" disabled={busy}>Approve mock refund</button>
                        <button className="button button-secondary" type="submit" value="reject" disabled={busy}>Reject</button>
                      </form>
                    )}
                  </div>
                )) : <EmptyState title="No business actions yet" body="Open order details to request a demo action. Refund requests await Admin or Supervisor approval." />}
              </div>
              {selectedOrderId && (
                <div className="modal-backdrop" role="presentation" onClick={() => setSelectedOrderId("")}>
                  <section className="detail-modal" role="dialog" aria-modal="true" aria-label="Order details" onClick={(event) => event.stopPropagation()}>
                    <div className="modal-title"><div><span className="eyebrow">ORDER DETAILS</span><h2>{orders.find((order) => order.id === selectedOrderId)?.order_number ?? "Order"}</h2></div><button className="icon-button" onClick={() => setSelectedOrderId("")} aria-label="Close details">×</button></div>
                    {error && <p role="alert">{error}</p>}
                    {!orderDetails ? <div className="loading-line"><span className="spinner spinner-small" /> Loading order details…</div> : <div className="detail-grid">
                      <div className="detail-block"><h3>Shipment</h3>{orderDetails.shipments.length ? orderDetails.shipments.map((shipment) => <div className="detail-item" key={shipment.tracking_number}><strong>{shipment.carrier}</strong><span>{shipment.tracking_number}</span><span className={statusClass(shipment.status)}>{shipment.status.replaceAll("_", " ")}</span><small>Estimated {formatDate(shipment.estimated_delivery_at)}</small></div>) : <p className="muted">No shipment recorded.</p>}</div>
                      <div className="detail-block"><h3>Payment & refunds</h3>{orderDetails.payments.length ? orderDetails.payments.map((payment, index) => <div className="detail-item" key={`${payment.status}-${index}`}><strong>{payment.currency} {Number(payment.amount).toFixed(2)}</strong><span>Payment {payment.status}</span>{payment.refunds.map((refund, refundIndex) => <small key={refundIndex}>Refund {refund.status} · {payment.currency} {Number(refund.amount).toFixed(2)}</small>)}</div>) : <p className="muted">No payment recorded.</p>}</div>
                    </div>}
                    {user.role !== "viewer" && (
                      <form onSubmit={(event) => void submitBusinessAction(event)}>
                        <h3>Demo business action</h3>
                        <p className="small-note">Cancellation: unpaid demo chat orders only, pending/processing and without shipments. Refund: successful mock payments only, subject to approval.</p>
                        <label className="field"><span>Action</span><select name="kind"><option value="refund">Request remaining mock refund</option><option value="cancellation">Cancel eligible demo order</option></select></label>
                        <label className="field"><span>Reason</span><input name="reason" required minLength={3} maxLength={240} /></label>
                        <label><input name="confirmed" type="checkbox" required /> I confirm this demo action for the order shown above. No real money will move.</label>
                        <button className="button button-primary" disabled={busy}>Submit demo action</button>
                      </form>
                    )}
                    <p className="small-note">Actions and approval decisions are recorded. No real payment provider is called.</p>
                  </section>
                </div>
              )}
            </section>
          )}

          {page === "customers" && (
            <section>
              <PageHeading eyebrow="CUSTOMER CONTEXT" title="Customers" description="Find a customer and see their support context and order history." />
              {user.role !== "viewer" && (
                <form className="card customer-create-form" onSubmit={(event) => void createCustomer(event)}>
                  <div><h2>Provision a customer profile</h2><p className="muted">Customers can sign up after you add their matching email here.</p></div>
                  <label className="field"><span>Full name</span><input name="full_name" required minLength={1} maxLength={120} /></label>
                  <label className="field"><span>Email</span><input name="email" type="email" required maxLength={254} /></label>
                  <label className="field"><span>Phone (optional)</span><input name="phone_number" maxLength={32} /></label>
                  <button className="button button-primary" disabled={busy}>Create profile</button>
                </form>
              )}
              <div className="toolbar"><div className="searchbox"><span>⌕</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search name or email…" /></div><button className="button button-quiet" onClick={() => void refreshPageData()}>↻ Refresh</button></div>
              <div className="customer-layout">
                <div className="card table-card"><TableHeader title="Customer directory" count={visibleCustomers.length} />
                  {visibleCustomers.length ? <div className="customer-list">{visibleCustomers.map((customer) => <button key={customer.id} className={`customer-row ${selectedCustomerId === customer.id ? "customer-selected" : ""}`} onClick={() => void viewCustomer(customer)}><span className="customer-avatar">{initials(customer.full_name)}</span><span className="customer-main"><strong>{customer.full_name}</strong><small>{customer.email}</small></span><span className="row-arrow">→</span></button>)}</div> : <EmptyState title="No customers yet" body="Seed the demo commerce data to populate this directory." />}
                </div>
                <div className="card customer-detail">
                  {selectedCustomerId ? <>
                    {(() => { const customer = customers.find((item) => item.id === selectedCustomerId); return customer ? <><span className="customer-avatar customer-avatar-large">{initials(customer.full_name)}</span><h2>{customer.full_name}</h2><p>{customer.email}</p><div className="divider" /><div className="mini-label">ORDER HISTORY</div>{orders.filter((order) => order.customer_id === customer.id).map((order) => <button className="customer-order" key={order.id} onClick={() => void openOrder(order)}><span><strong>{order.order_number}</strong><small>{formatDate(order.created_at)}</small></span><span className={statusClass(order.status)}>{order.status}</span></button>)}{orders.filter((order) => order.customer_id === customer.id).length === 0 && <p className="muted">No orders recorded for this customer.</p>}</> : null; })()}
                  </> : <EmptyState title="Select a customer" body="Choose someone from the directory to review their profile and orders." />}
                </div>
              </div>
            </section>
          )}

          {page === "tickets" && (
            <section>
              <PageHeading eyebrow="SUPPORT OPERATIONS" title="Support tickets" description="Review tickets created from support conversations and the mock commerce tools." />
              <div className="toolbar"><div className="searchbox"><span>⌕</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search ticket number or subject…" /></div><button className="button button-quiet" onClick={() => void refreshPageData()}>↻ Refresh</button></div>
              <div className="card table-card"><TableHeader title="Tickets" count={tickets.filter((ticket) => `${ticket.ticket_number} ${ticket.subject}`.toLowerCase().includes(query.toLowerCase())).length} />
                {tickets.length ? <div className="table-scroll"><table><thead><tr><th>Ticket</th><th>Priority</th><th>Status</th><th>Created</th></tr></thead><tbody>{tickets.filter((ticket) => `${ticket.ticket_number} ${ticket.subject}`.toLowerCase().includes(query.toLowerCase())).map((ticket) => <tr key={ticket.id}><td><strong>{ticket.ticket_number}</strong><small>{ticket.subject}</small></td><td><span className={`priority priority-${ticket.priority}`}>{ticket.priority}</span></td><td><span className={statusClass(ticket.status)}>{ticket.status.replaceAll("_", " ")}</span></td><td>{formatDate(ticket.created_at)}</td></tr>)}</tbody></table></div> : <EmptyState title="No support tickets yet" body="Create a support ticket from an AI conversation linked to a customer." />}
              </div>
              {user.role === "viewer" && <p className="inline-info">You have read-only access. Ticket creation is limited to Admins, Supervisors, and Support Agents.</p>}
            </section>
          )}

          {page === "team" && (
            <section>
              <PageHeading eyebrow="WORKSPACE SETTINGS" title="Team members" description="Administer your organization’s team and access roles." />
              {!isAdmin ? <div className="card"><EmptyState title="Admin access required" body="Only an organization Admin can view or add team members." /></div> : <>
                <div className="team-layout">
                  <div className="card table-card"><TableHeader title="People in this workspace" count={members.length} />
                    <div className="member-list">{members.map((member) => <div className="member-row" key={member.user.id}><span className="profile-avatar">{initials(member.user.full_name)}</span><span className="member-info"><strong>{member.user.full_name}</strong><small>{member.user.email}</small></span><span className="role-pill">{roleLabels[member.role as CurrentUser["role"]] ?? member.role}</span><span className={statusClass(member.status)}>{member.status}</span></div>)}</div>
                    {members.length === 0 && <EmptyState title="No team members" body="Add a teammate to start collaborating." />}
                  </div>
                  <form className="card add-member-card" onSubmit={(event) => void createTeamMember(event)}>
                    <div className="card-heading"><div><span className="heading-icon icon-lime">＋</span><div><h3>Add a teammate</h3><p>Create an account in this organization.</p></div></div></div>
                    <label className="field"><span>Full name</span><input name="full_name" required maxLength={120} /></label>
                    <label className="field"><span>Email address</span><input name="email" type="email" required /></label>
                    <label className="field"><span>Temporary password</span><input name="password" type="password" required minLength={12} maxLength={128} /><small className="field-hint">At least 12 characters. Share it securely.</small></label>
                    <label className="field"><span>Role</span><select name="role" defaultValue="support_agent"><option value="support_agent">Support agent</option><option value="supervisor">Supervisor</option><option value="viewer">Viewer</option><option value="admin">Admin</option></select></label>
                    <button className="button button-primary button-wide" disabled={busy}>Add team member</button>
                  </form>
                </div>
              </>}
            </section>
          )}
        </div>
      </main>
    </div>
  );
}

function PageHeading({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string;
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="page-heading">
      <div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div>
      {action}
    </div>
  );
}

function TableHeader({ title, count }: { title: string; count: number }) {
  return <div className="table-header"><h3>{title}</h3><span>{count} records</span></div>;
}

function EmptyState({ title, body }: { title: string; body: string }) {
  return <div className="empty-state"><span className="empty-icon">⌁</span><h3>{title}</h3><p>{body}</p></div>;
}

function Overview({
  user,
  customers,
  orders,
  tickets,
  readiness,
  onNavigate,
  onRefresh,
  onOpenOrder,
}: {
  user: CurrentUser;
  customers: Customer[];
  orders: Order[];
  tickets: Ticket[];
  readiness: Readiness;
  onNavigate: (page: PageName) => void;
  onRefresh: () => void;
  onOpenOrder: (order: Order) => void;
}) {
  const delayed = orders.filter((order) => order.status === "delayed").length;
  const today = new Intl.DateTimeFormat(undefined, {
    weekday: "long",
    month: "long",
    day: "numeric",
  })
    .format(new Date())
    .toUpperCase();
  return (
    <section>
      <div className="welcome-row">
        <div><div className="eyebrow">{today}</div><h1>Good to have you, {user.user.full_name.split(" ")[0]} <span className="wave">✳</span></h1><p>Here’s what’s happening in {user.organization.name} today.</p></div>
        <button className="button button-secondary" onClick={onRefresh}>↻ <span>Refresh data</span></button>
      </div>
      <div className="banner-card">
        <div className="banner-orb orb-one" /><div className="banner-orb orb-two" />
        <div className="banner-content"><span className="banner-kicker"><span className="banner-dot" /> MODULES 1–6 CONNECTED</span><h2>Your support workspace is ready.</h2><p>Explore real tenant data, AI support workflows, and grounded knowledge answers—all in one place.</p><button onClick={() => onNavigate("inbox")}>Start a conversation <span>→</span></button></div>
        <div className="banner-art" aria-hidden="true"><div className="art-ring ring-a" /><div className="art-ring ring-b" /><div className="art-ring ring-c" /><div className="art-center">r<span>✳</span></div><div className="art-node node-a">↗</div><div className="art-node node-b">▤</div><div className="art-node node-c">◉</div></div>
      </div>
      <div className="stat-grid">
        <StatCard label="Customers" value={customers.length} caption="In this workspace" glyph="♙" color="violet" />
        <StatCard label="Orders" value={orders.length} caption="Mock commerce records" glyph="▧" color="blue" />
        <StatCard label="Delayed orders" value={delayed} caption={delayed ? "May need attention" : "No delays recorded"} glyph="◷" color="amber" />
        <StatCard label="Support tickets" value={tickets.length} caption="Across the organization" glyph="▱" color="green" />
      </div>
      <div className="overview-grid">
        <div className="card recent-orders"><div className="section-card-heading"><div><h3>Recent orders</h3><p>Latest activity from your demo commerce data.</p></div><button className="text-button" onClick={() => onNavigate("orders")}>View all <span>→</span></button></div>
          {orders.length ? <div className="overview-order-list">{orders.slice(0, 5).map((order) => <button key={order.id} className="overview-order" onClick={() => onOpenOrder(order)}><span className="order-icon">▧</span><span className="order-name"><strong>{order.order_number}</strong><small>{formatDate(order.created_at)}</small></span><span className={statusClass(order.status)}>{order.status.replaceAll("_", " ")}</span><strong className="order-total">{order.currency} {Number(order.total_amount).toFixed(2)}</strong><span className="row-arrow">→</span></button>)}</div> : <EmptyState title="Your demo data is waiting" body="Seed the organization’s mock customers, orders, shipments, and tickets." />}
        </div>
        <div className="card quick-actions"><div className="section-card-heading"><div><h3>Pick up where you left off</h3><p>Jump into a workspace area.</p></div></div>
          <button className="quick-action" onClick={() => onNavigate("inbox")}><span className="quick-icon quick-violet">✳</span><span><strong>Ask the support assistant</strong><small>Check status or answer a policy question</small></span><span className="row-arrow">→</span></button>
          <button className="quick-action" onClick={() => onNavigate("knowledge")}><span className="quick-icon quick-yellow">▤</span><span><strong>Explore your knowledge base</strong><small>Upload policies and test grounded search</small></span><span className="row-arrow">→</span></button>
          <button className="quick-action" onClick={() => onNavigate("customers")}><span className="quick-icon quick-blue">♙</span><span><strong>Look up a customer</strong><small>Review customer order history</small></span><span className="row-arrow">→</span></button>
        </div>
      </div>
      <div className="readiness-card card">
        <div className="readiness-copy">
          <strong>Workspace services</strong>
          <small>
            {readiness.status === "ok"
              ? "Core dependencies are ready."
              : readiness.status === "checking"
                ? "Checking API dependency readiness…"
                : "Some dependencies are not ready; affected features may be unavailable."}
          </small>
        </div>
        <div className="readiness-items">
          {Object.entries(readiness.dependencies).map(([name, status]) => (
            <span key={name}>
              <i className={status === "ok" ? "ready-indicator" : "not-ready-indicator"} />
              {name.replaceAll("_", " ")}
              <em>{status}</em>
            </span>
          ))}
        </div>
      </div>
      <div className="bottom-note"><span>✳</span><div><strong>Built to keep support human.</strong><small>AI assistance is grounded in your data, with clear limits and sources.</small></div><button onClick={() => onNavigate("knowledge")}>Learn more <span>→</span></button></div>
    </section>
  );
}

function StatCard({
  label,
  value,
  caption,
  glyph,
  color,
}: {
  label: string;
  value: number;
  caption: string;
  glyph: string;
  color: string;
}) {
  return <div className="card stat-card"><div className="stat-top"><span>{label}</span><span className={`stat-glyph stat-${color}`}>{glyph}</span></div><div className="stat-value">{value}</div><div className="stat-caption">{caption}</div><div className="stat-sparkline"><i /><i /><i /><i /><i /><i /><i /><i /><i /></div></div>;
}

function KnowledgePage({
  documents,
  canEdit,
  busy,
  searchQuery,
  searchResult,
  history,
  setSearchQuery,
  onSearch,
  onUpload,
  onUploadVersion,
  onRetry,
  onDelete,
  onOpenHistory,
  onCloseHistory,
}: {
  documents: KnowledgeDocument[];
  canEdit: boolean;
  busy: boolean;
  searchQuery: string;
  searchResult: SearchResult | null;
  history: KnowledgeDocument | null;
  setSearchQuery: (query: string) => void;
  onSearch: (event: FormEvent<HTMLFormElement>) => void;
  onUpload: (event: FormEvent<HTMLFormElement>) => void;
  onUploadVersion: (event: ChangeEvent<HTMLInputElement>, document: KnowledgeDocument) => void;
  onRetry: (document: KnowledgeDocument, versionId: string) => void;
  onDelete: (document: KnowledgeDocument) => void;
  onOpenHistory: (document: KnowledgeDocument) => void;
  onCloseHistory: () => void;
}) {
  return (
    <section>
      <PageHeading eyebrow="YOUR TEAM’S ANSWER SOURCE" title="Knowledge base" description="Keep trusted policies close at hand. Search documents and show exactly where an answer came from." action={<span className="provider-chip"><span className="provider-dot" /> Managed retrieval</span>} />
      <div className="knowledge-hero">
        <div className="knowledge-hero-copy"><span className="knowledge-icon">✳</span><div><strong>Answers grounded in your documents</strong><p>Search across ingested policies and get citations your team can verify.</p></div></div>
        <form className="knowledge-search" onSubmit={onSearch}><span>⌕</span><input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="Ask a question about your policies…" maxLength={1000} /><button className="button button-dark" disabled={busy || searchQuery.trim().length < 3}>Search <span>→</span></button></form>
        {searchResult && <div className="search-result"><div className="result-label"><span className={searchResult.grounded ? "grounded-badge" : "ungrounded-badge"}>{searchResult.grounded ? "✓ SOURCED ANSWER" : "NO SOURCE FOUND"}</span></div><p className="result-answer">{searchResult.answer}</p>{searchResult.citations.length > 0 && <div className="citation-list">{searchResult.citations.map((citation) => <div className="citation-card" key={`${citation.document_id}-${citation.version_id}`}><span className="citation-number">↗</span><div><strong>{citation.title}</strong><small>{citation.filename}</small>{citation.excerpt && <p>{citation.excerpt}</p>}</div></div>)}</div>}</div>}
      </div>
      <div className="knowledge-columns">
        <div className="card documents-card"><div className="section-card-heading"><div><h3>Your documents</h3><p>Latest version and processing status.</p></div><span className="record-count">{documents.length} documents</span></div>
          {documents.length ? <div className="document-list">{documents.map((document) => {
            const version = document.versions[0];
            return <article className="document-row" key={document.id}><div className="document-icon">▤</div><div className="document-main"><strong>{document.title}</strong><small>{version?.original_filename ?? "No version"} · {version ? `${(version.byte_size / 1024).toFixed(0)} KB` : ""}</small>{version?.error_code && <small className="error-code">Issue: {version.error_code.replaceAll("_", " ")}</small>}</div><div className="document-actions"><span className={statusClass(document.status === "deleting" ? document.status : version?.status ?? document.status)}>{(document.status === "deleting" ? document.status : version?.status ?? document.status).replaceAll("_", " ")}</span><button className="small-action" disabled={busy} onClick={() => onOpenHistory(document)}>Versions</button>{canEdit && version?.status === "failed" && <button className="small-action" disabled={busy} onClick={() => onRetry(document, version.id)}>Retry</button>}{canEdit && <label className="small-action file-action">New version<input type="file" accept=".pdf,.docx,.md,.txt" disabled={busy} onChange={(event) => onUploadVersion(event, document)} /></label>}{canEdit && <button className="small-action danger-action" disabled={busy || document.status === "deleting"} onClick={() => onDelete(document)}>Delete</button>}</div></article>;
          })}</div> : <EmptyState title="No documents yet" body="Add an approved support policy to start asking grounded questions." />}
        </div>
        {canEdit ? <form className="card upload-card" onSubmit={onUpload}><div className="card-heading"><div><span className="heading-icon icon-lime">＋</span><div><h3>Add a document</h3><p>Admin and Supervisor access</p></div></div></div><label className="field"><span>Document title</span><input name="title" required minLength={1} maxLength={160} placeholder="Returns and exchanges" /></label><label className="field"><span>Description <em>optional</em></span><textarea name="description" rows={3} maxLength={500} placeholder="A short note about the policy…" /></label><label className="upload-drop"><input type="file" name="file" accept=".pdf,.docx,.md,.txt" required /><span className="upload-cloud">↑</span><strong>Choose a file to upload</strong><small>PDF, DOCX, Markdown, or UTF-8 text · up to 10 MB</small></label><button className="button button-primary button-wide" disabled={busy}>Upload and index <span>→</span></button><p className="small-note">Documents are sent to the configured managed retrieval provider for indexing. For a fictional test policy, <a className="sample-policy-link" href="/demo-support-policies.md" download>download the sample policy pack</a> and upload it above. Nothing is uploaded automatically.</p></form> : <div className="card upload-card"><div className="card-heading"><div><span className="heading-icon icon-lime">⌑</span><div><h3>Document management</h3><p>Read-only access</p></div></div></div><p className="muted">You can search and review sources. Ask an Admin or Supervisor to add, update, or remove knowledge documents.</p><p className="small-note">You can also <a className="sample-policy-link" href="/demo-support-policies.md" download>download the fictional sample policy pack</a> for an Admin to upload.</p></div>}
      </div>
      {history && <div className="modal-backdrop" role="presentation" onClick={onCloseHistory}><section className="detail-modal history-modal" role="dialog" aria-modal="true" aria-label={`Version history for ${history.title}`} onClick={(event) => event.stopPropagation()}><div className="modal-title"><div><span className="eyebrow">DOCUMENT VERSIONS</span><h2>{history.title}</h2></div><button className="icon-button" onClick={onCloseHistory} aria-label="Close version history">×</button></div><div className="history-list">{history.versions.map((version) => <div className="history-row" key={version.id}><div className="version-number">v{version.version_number}</div><div className="history-info"><strong>{version.original_filename}</strong><small>{formatDate(version.created_at)} · {(version.byte_size / 1024).toFixed(0)} KB</small>{version.error_code && <small className="error-code">{version.error_code.replaceAll("_", " ")}</small>}</div><span className={statusClass(version.status)}>{version.status.replaceAll("_", " ")}</span>{canEdit && version.status === "failed" && <button className="small-action" disabled={busy} onClick={() => onRetry(history, version.id)}>Retry</button>}</div>)}</div></section></div>}
    </section>
  );
}
