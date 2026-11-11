"use client";

import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ApiRequestError, customerApi, type Order } from "@/lib/api";
import { VoicePanel } from "../voice-panel";

type CustomerProfile = {
  id: string;
  full_name: string;
  email: string;
  phone_number: string | null;
};
type AuthMode = "login" | "register";
type RegistrationResponse = { message: string };

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

function formatDate(value: string) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(value),
  );
}

export default function CustomerPortalPage() {
  const [profile, setProfile] = useState<CustomerProfile | null>(null);
  const [orders, setOrders] = useState<Order[]>([]);
  const [mode, setMode] = useState<AuthMode>("login");
  const [form, setForm] = useState({
    organization_slug: "",
    email: "",
    password: "",
  });
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const verificationRequest = useRef<Promise<RegistrationResponse> | null>(null);

  const fetchPortalData = useCallback(async (): Promise<[CustomerProfile, Order[]]> => {
    const [customer, history] = await Promise.all([
      customerApi<CustomerProfile>("/api/v1/customer/profile"),
      customerApi<Order[]>("/api/v1/customer/orders?limit=100"),
    ]);
    return [customer, history];
  }, []);

  const applyPortalData = useCallback((customer: CustomerProfile, history: Order[]) => {
    setProfile(customer);
    setOrders(history);
  }, []);

  useEffect(() => {
    let active = true;
    const token = new URLSearchParams(window.location.search).get("token");
    if (token || verificationRequest.current) {
      const request = verificationRequest.current ?? customerApi<RegistrationResponse>(
        "/api/v1/customer/auth/verify",
        {
          method: "POST",
          body: JSON.stringify({ token }),
        },
      );
      verificationRequest.current = request;
      window.history.replaceState(null, "", "/customer");
      void request
        .then((response) => {
          if (active) setNotice(response.message);
        })
        .catch((cause: unknown) => {
          if (active) setError(messageFrom(cause));
        })
        .finally(() => {
          if (active) setLoading(false);
        });
    } else {
      void fetchPortalData()
        .then(([customer, history]) => {
          if (active) applyPortalData(customer, history);
        })
        .catch((cause: unknown) => {
          if (active && !(cause instanceof ApiRequestError && cause.status === 401)) {
            setError(messageFrom(cause));
          }
        })
        .finally(() => {
          if (active) setLoading(false);
        });
    }
    return () => {
      active = false;
    };
  }, [applyPortalData, fetchPortalData]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (mode === "register") {
        const response = await customerApi<RegistrationResponse>(
          "/api/v1/customer/auth/register",
          { method: "POST", body: JSON.stringify(form) },
        );
        setNotice(response.message);
      } else {
        await customerApi("/api/v1/customer/auth/login", {
          method: "POST",
          body: JSON.stringify(form),
        });
        const [customer, history] = await fetchPortalData();
        applyPortalData(customer, history);
      }
    } catch (cause) {
      setError(messageFrom(cause));
    } finally {
      setBusy(false);
    }
  }

  async function signOut() {
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/customer-session", {
        method: "DELETE",
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error("Unable to sign out. Please try again.");
      setProfile(null);
      setOrders([]);
      setMode("login");
    } catch (cause) {
      setError(messageFrom(cause));
    } finally {
      setBusy(false);
    }
  }

  if (loading) {
    return <main className="customer-portal-shell"><p role="status">Loading your account…</p></main>;
  }

  if (!profile) {
    return (
      <main className="customer-portal-shell">
        <header className="customer-portal-header">
          <Link className="brand" href="/customer">
            <span className="brand-mark">r</span>
            <span>resolve<span className="brand-accent">ai</span></span>
          </Link>
          <Link className="text-button" href="/">Staff workspace</Link>
        </header>
        <section className="customer-auth-card card">
          <div className="eyebrow">CUSTOMER ACCOUNT</div>
          <h1>{mode === "login" ? "Welcome back" : "Create your customer account"}</h1>
          <p className="muted">
            Sign in to view your orders and talk to the order assistant. Your account is linked
            only to a customer profile provisioned by the business.
          </p>
          <div className="auth-tabs" role="tablist" aria-label="Customer authentication">
            <button
              className={mode === "login" ? "auth-tab active" : "auth-tab"}
              type="button"
              role="tab"
              aria-selected={mode === "login"}
              onClick={() => {
                setMode("login");
                setError("");
                setNotice("");
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
                setNotice("");
              }}
            >
              Create account
            </button>
          </div>
          {error && (
            <div className="notice notice-error" role="alert">
              <span>{error}</span>
              <button
                className="button button-secondary"
                type="button"
                onClick={() => window.location.reload()}
              >
                Refresh page
              </button>
            </div>
          )}
          {notice && <div className="notice notice-success" role="status">{notice}</div>}
          <form onSubmit={(event) => void submit(event)}>
            <label className="field">
              <span>Business account code</span>
              <input
                autoComplete="organization"
                required
                minLength={3}
                maxLength={63}
                value={form.organization_slug}
                onChange={(event) =>
                  setForm({ ...form, organization_slug: event.target.value.toLowerCase() })
                }
                placeholder="For example, your-store"
              />
              <small className="field-hint">Ask the business for its account code.</small>
            </label>
            <label className="field">
              <span>Email address</span>
              <input
                autoComplete="email"
                type="email"
                required
                maxLength={254}
                value={form.email}
                onChange={(event) => setForm({ ...form, email: event.target.value })}
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
              />
              {mode === "register" && <small className="field-hint">Use at least 12 characters.</small>}
            </label>
            <button className="button button-primary button-wide" disabled={busy} type="submit">
              {busy ? "Please wait…" : mode === "login" ? "Sign in" : "Send verification email"}
            </button>
          </form>
          <p className="small-note">
            For your security, account creation works only when your email already matches a
            customer profile in this business.
          </p>
        </section>
      </main>
    );
  }

  return (
    <main className="customer-portal-shell">
      <header className="customer-portal-header">
        <Link className="brand" href="/customer">
          <span className="brand-mark">r</span>
          <span>resolve<span className="brand-accent">ai</span></span>
        </Link>
        <div className="customer-account-menu">
          <span>{profile.full_name}</span>
          <button className="button button-secondary" disabled={busy} onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </header>
      {error && <div className="notice notice-error" role="alert">{error}</div>}
      <section className="page-heading">
        <div>
          <div className="eyebrow">YOUR ACCOUNT</div>
          <h1>Hello, {profile.full_name}</h1>
          <p className="muted">{profile.email} · View your order history and place a new order by voice.</p>
        </div>
      </section>
      <div className="customer-portal-grid">
        <section className="card customer-portal-orders">
          <div className="section-card-heading">
            <div><h2>Your orders</h2><p>Orders placed from your verified customer account.</p></div>
          </div>
          {orders.length === 0 ? (
            <p className="muted">No orders are linked to your account yet.</p>
          ) : (
            <div className="customer-order-list">
              {orders.map((order) => (
                <article className="customer-order-card" key={order.id}>
                  <div>
                    <strong>{order.order_number}</strong>
                    <small>{formatDate(order.created_at)}</small>
                  </div>
                  <span className={`status status-${order.status === "delivered" ? "green" : "amber"}`}>
                    {order.status.replaceAll("_", " ")}
                  </span>
                  <strong>{order.currency} {Number(order.total_amount).toFixed(2)}</strong>
                </article>
              ))}
            </div>
          )}
        </section>
        <VoicePanel customers={[]} canCall customerMode />
      </div>
    </main>
  );
}
