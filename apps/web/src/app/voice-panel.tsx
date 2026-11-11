"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type Vapi from "@vapi-ai/web";
import { api, customerApi, type Conversation, type Customer } from "@/lib/api";

type VoiceCall = {
  id: string;
  conversation_id: string;
  channel: "browser" | "phone";
  status: string;
  ended_reason: string | null;
  created_at: string;
};
type VoiceConfig = { ready: boolean; issues: string[]; phone_calls_enabled: boolean };
type Started = {
  call: VoiceCall;
  public_key: string;
  assistant_id: string;
  session_token: string;
};

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Voice connection failed. Check microphone access and Vapi logs.";
}

// Vapi reports an assistant hang-up (e.g. its end-call tool) as a Daily "ejected" error.
function isAssistantHangup(error: unknown) {
  let detail: string;
  try {
    detail = JSON.stringify(error) ?? "";
  } catch {
    return false;
  }
  return /ejected|ejection|meeting has ended/i.test(detail);
}

export function VoicePanel({
  customers,
  canCall,
  customerMode = false,
}: {
  customers: Customer[];
  canCall: boolean;
  customerMode?: boolean;
}) {
  const [config, setConfig] = useState<VoiceConfig | null>(null);
  const [calls, setCalls] = useState<VoiceCall[]>([]);
  const [customerId, setCustomerId] = useState("");
  const [consent, setConsent] = useState(false);
  const [state, setState] = useState("idle");
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<Conversation | null>(null);
  const sdk = useRef<Vapi | null>(null);
  const activeId = useRef<string | null>(null);
  const starting = useRef(false);
  const mounted = useRef(true);
  const request = customerMode ? customerApi : api;
  const voiceBase = customerMode ? "/api/v1/customer/voice" : "/api/v1/voice";

  const refresh = useCallback(async () => {
    const [configuration, history] = await Promise.all([
      request<VoiceConfig>(`${voiceBase}/config`),
      request<VoiceCall[]>(`${voiceBase}/calls`),
    ]);
    if (mounted.current) {
      setConfig(configuration);
      setCalls(history);
    }
  }, [request, voiceBase]);

  const finish = useCallback(async () => {
    const id = activeId.current;
    activeId.current = null;
    if (id) await request(`${voiceBase}/calls/${id}/end`, { method: "POST" });
    if (mounted.current) {
      setState("idle");
      await refresh();
    }
  }, [refresh, request, voiceBase]);

  useEffect(() => {
    mounted.current = true;
    void refresh().catch((cause: unknown) => {
      if (mounted.current) setError(errorMessage(cause));
    });
    return () => {
      mounted.current = false;
      void (async () => {
        try {
          await sdk.current?.stop();
          await finish();
        } catch (cause) {
          console.error("Unable to close voice demo session", errorMessage(cause));
        }
      })();
    };
  }, [finish, refresh]);

  async function start() {
    if (starting.current || activeId.current) return;
    starting.current = true;
    setError("");
    setSelected(null);
    setState("connecting");
    try {
      if (!navigator.mediaDevices?.getUserMedia) {
        throw new Error("Microphone access requires localhost or HTTPS and a supported browser.");
      }
      const microphone = await navigator.mediaDevices.getUserMedia({ audio: true });
      microphone.getTracks().forEach((track) => track.stop());
      if (!mounted.current) return;
      const { default: VapiClient } = await import("@vapi-ai/web");
      const created = await request<Started>(`${voiceBase}/calls`, {
        method: "POST",
        body: JSON.stringify(
          customerMode ? { consent } : { customer_id: customerId || null, consent },
        ),
      });
      activeId.current = created.call.id;
      if (!mounted.current) {
        await finish();
        return;
      }
      const client = new VapiClient(created.public_key);
      sdk.current = client;
      client.on("call-start", () => {
        if (mounted.current) setState("connected");
      });
      client.on("call-end", () => {
        void finish().catch((cause: unknown) => {
          if (mounted.current) {
            setState("idle");
            setError(errorMessage(cause));
          }
        });
      });
      client.on("error", (cause: unknown) => {
        if (mounted.current && !isAssistantHangup(cause)) setError(errorMessage(cause));
        void (async () => {
          await client.stop();
          await finish();
        })().catch((failure: unknown) => {
          if (mounted.current) setError(errorMessage(failure));
        });
      });
      const call = await client.start(created.assistant_id, {
        variableValues: { resolve_session: created.session_token },
      });
      if (!call) throw new Error("Vapi did not create a call. Check the assistant configuration.");
    } catch (cause) {
      await sdk.current?.stop();
      try {
        await finish();
      } catch (cleanupError) {
        console.error("Unable to close failed voice session", errorMessage(cleanupError));
      }
      if (mounted.current) {
        setState("idle");
        setError(errorMessage(cause));
      }
    } finally {
      starting.current = false;
    }
  }

  async function stop() {
    setState("ending");
    try {
      await sdk.current?.stop();
      await finish();
    } catch (cause) {
      setError(errorMessage(cause));
      setState("idle");
    }
  }

  async function showConversation(call: VoiceCall) {
    try {
      setSelected(
        await api<Conversation>(`/api/v1/conversations/${call.conversation_id}`),
      );
    } catch (cause) {
      setError(errorMessage(cause));
    }
  }

  return (
    <section className="voice-panel">
      <div className="page-heading">
        <div>
          <div className="eyebrow">VOICE SUPPORT DEMO</div>
          <h1>Calling</h1>
          <p className="muted">Use browser microphone calls for testing. {config?.phone_calls_enabled ? "Inbound calls to the configured Vapi number are also enabled." : "Inbound phone calls are not configured yet."}</p>
        </div>
        <button className="button button-secondary" onClick={() => void refresh().catch((cause: unknown) => setError(errorMessage(cause)))}>Refresh calls</button>
      </div>
      {error && <div className="notice notice-error" role="alert">{error}</div>}
      {!config && !error && <p role="status">Checking voice setup...</p>}
      {config && !config.ready && (
        <div className="card voice-card">
          <h3>Vapi setup required</h3>
          <p>Calling stays disabled until the provider is configured. Keep private keys and webhook credentials out of chat and source control.</p>
          <ul>{config.issues.map((issue) => <li key={issue}>{issue}</li>)}</ul>
          <p>Use the browser-calling setup instructions in the project README: configure a saved Vapi assistant, a Custom Credential and a public HTTPS webhook.</p>
        </div>
      )}
      <div className="card voice-card">
        <h3>Start a voice conversation</h3>
        {!customerMode && (
          <>
            <label className="field">
              <span>Customer for this demo call</span>
              <select value={customerId} disabled={state !== "idle"} onChange={(event) => setCustomerId(event.target.value)}>
                <option value="">No customer attached</option>
                {customers.map((customer) => <option key={customer.id} value={customer.id}>{customer.full_name}</option>)}
              </select>
            </label>
            <p className="muted">Choose Jamie Rivera for seeded order questions. Customer selection applies only to a new call.</p>
          </>
        )}
        <label>
          <input type="checkbox" checked={consent} disabled={state !== "idle"} onChange={(event) => setConsent(event.target.checked)} />{" "}
          I agree to send my microphone audio and demo support context to Vapi and its configured voice/model providers. I will not share sensitive information.
        </label>
        <p className="muted">Recording is disabled in the supplied assistant template. Support requests and answers are saved in this workspace; Vapi may retain provider logs. Commerce actions remain mock-only. Human review is not a live phone transfer.</p>
        <p role="status">Call: {state}</p>
        {!canCall && <p>Only Admins, Supervisors and Support Agents can start calls.</p>}
        <div className="voice-actions">
          <button className="button button-primary" disabled={!config?.ready || !consent || !canCall || state !== "idle"} onClick={() => void start()}>Start browser call</button>
          <button className="button button-secondary" disabled={state !== "connected"} onClick={() => void stop()}>End call</button>
        </div>
      </div>
      <div className="card voice-card">
        <h3>Recent calls</h3>
        {calls.length === 0 ? <p>No voice calls yet. Setup and start a browser call above.</p> : (
          <ul>{calls.map((call) => (
            <li key={call.id}>
              {new Date(call.created_at).toLocaleString()} - {call.channel} - {call.status}
              {call.ended_reason && ` (${call.ended_reason})`}{" "}
              {!customerMode && <button className="button button-secondary" onClick={() => void showConversation(call)}>View support transcript</button>}
            </li>
          ))}</ul>
        )}
        {selected && (
          <div>
            <h3>Support transcript</h3>
            <p className="muted">Stored support-tool turns, not a full audio transcript. Refresh to see later completed requests.</p>
            {selected.messages.length === 0 ? <p>No support requests stored yet.</p> : selected.messages.map((message) => (
              <p key={message.id}><strong>{message.role === "user" ? "Caller" : "ResolveAI"}:</strong> {message.content}</p>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
