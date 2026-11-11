import asyncio
import logging
from time import monotonic
from urllib.parse import urlsplit

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)
_REACHABLE_CACHE_SECONDS = 60.0
_UNREACHABLE_CACHE_SECONDS = 10.0
_PROBE_ATTEMPTS = 2
_PROBE_RETRY_DELAY_SECONDS = 0.5
_webhook_probe: tuple[str, float, str | None] | None = None


def configuration_errors() -> list[str]:
    errors = []
    if not settings.vapi_enabled:
        errors.append("VAPI_ENABLED is false")
    for name, value in (
        ("VAPI_PUBLIC_KEY", settings.vapi_public_key),
        ("VAPI_ASSISTANT_ID", settings.vapi_assistant_id),
    ):
        if not value.strip():
            errors.append(f"{name} is missing")
    if (
        settings.vapi_webhook_secret is None
        or len(settings.vapi_webhook_secret.get_secret_value()) < 32
    ):
        errors.append("VAPI_WEBHOOK_SECRET must contain at least 32 characters")
    url = urlsplit(settings.vapi_public_api_url)
    if url.scheme != "https" or not url.netloc or url.username or url.query or url.fragment:
        errors.append("VAPI_PUBLIC_API_URL must be a public HTTPS API URL")
    return errors


def _probe_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(6.0), follow_redirects=False)


async def _probe_public_url(base_url: str) -> str | None:
    try:
        async with _probe_client() as client:
            response = await client.get(f"{base_url}/health/live")
        if response.status_code != 200 or response.json().get("status") != "ok":
            return (
                "Voice backend is unreachable: VAPI_PUBLIC_API_URL answered with HTTP "
                f"{response.status_code}. Start the public tunnel to port 8000."
            )
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        logger.warning(
            "Public voice webhook URL is unreachable",
            extra={"error_type": type(exc).__name__},
        )
        return (
            "Voice backend is unreachable: VAPI_PUBLIC_API_URL did not respond. "
            "Start the public tunnel to port 8000, then refresh."
        )
    return None


async def public_webhook_issue() -> str | None:
    """Check that Vapi can reach this API through VAPI_PUBLIC_API_URL."""
    global _webhook_probe
    base_url = settings.vapi_public_api_url.rstrip("/")
    now = monotonic()
    if _webhook_probe is not None:
        cached_url, expires_at, cached_issue = _webhook_probe
        if cached_url == base_url and now < expires_at:
            return cached_issue
    # Tunnel requests can fail once under load; only report unreachable after every attempt fails.
    issue: str | None = None
    for attempt in range(_PROBE_ATTEMPTS):
        issue = await _probe_public_url(base_url)
        if issue is None:
            break
        if attempt + 1 < _PROBE_ATTEMPTS:
            await asyncio.sleep(_PROBE_RETRY_DELAY_SECONDS)
    ttl = _UNREACHABLE_CACHE_SECONDS if issue else _REACHABLE_CACHE_SECONDS
    _webhook_probe = (base_url, now + ttl, issue)
    return issue


async def voice_readiness_issues() -> list[str]:
    errors = configuration_errors()
    if errors or not settings.vapi_reachability_check_enabled:
        return errors
    issue = await public_webhook_issue()
    return [issue] if issue else []


def assistant_configuration(credential_id: str) -> dict:
    return {
        "name": "ResolveAI demo support",
        "firstMessage": (
            "Hello! I'm ResolveAI, an AI support assistant in a demo. "
            "This call uses AI voice processing and is not recorded by ResolveAI. "
            "Browser callers have already consented. For phone calls, please say I agree "
            "before sharing account details; otherwise you may hang up. How can I help?"
        ),
        "maxDurationSeconds": 600,
        "silenceTimeoutSeconds": 120,
        "artifactPlan": {"recordingEnabled": False},
        "endCallMessage": "Thanks for calling ResolveAI. Goodbye for now.",
        "endCallPhrases": ["Thanks for calling ResolveAI. Goodbye for now."],
        "server": {
            "url": f"{settings.vapi_public_api_url.rstrip('/')}/api/v1/voice/webhook",
            "credentialId": credential_id,
            "timeoutSeconds": 20,
        },
        "serverMessages": ["status-update", "end-of-call-report", "tool-calls"],
        "model": {
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "messages": [{
                "role": "system",
                "content": (
                    "You are the voice interface for ResolveAI's mock support demo. "
                    "For every customer support or commerce request, including product lists, "
                    "order requests and confirmations, call resolve_support before giving facts. "
                    "Vapi speaks exactly one fixed Request Start message before each tool runs. "
                    "Do not generate an acknowledgment, narration, or progress update with a "
                    "tool call or after it. When the result arrives, go straight to the completed "
                    "answer without repeating a status message. Do not promise a completion time "
                    "or claim work is done before the result arrives. The server reads the actual "
                    "user transcript; "
                    "never use tool arguments as customer requests. "
                    "Use only the server's completed answer for order, policy or action facts. "
                    "Never invent products, SKUs, stock, prices or order numbers. "
                    "Never say an order was placed, noted or saved unless the completed "
                    "backend answer explicitly confirms creation. If a tool call fails or "
                    "returns no answer, say you could not complete that request right now and "
                    "have not placed an order; do not claim the backend is inaccessible. "
                    "For a new order, never ask the caller for an order number; the backend "
                    "generates that reference after creating the order. A customer must be "
                    "attached to place an order; if the backend says none is attached, explain "
                    "that staff must select a customer before starting the call. Ask only for "
                    "missing product or quantity details, and do not guess either value. Each "
                    "demo order contains one product, but the caller may place multiple separate "
                    "orders in the same call. Treat every new order as a new request, prepare a "
                    "fresh quote, and get explicit confirmation for each order; never reuse an "
                    "earlier confirmation. If the caller says 'that', 'those', 'it', or 'the same' "
                    "in direct response to a product question, use the product just identified "
                    "when there is only one possible match; do not ask them to repeat its code. "
                    "If the caller confirms a product code you just asked them to verify, accept "
                    "a clear 'yes' as confirmation of that product, not as the final order "
                    "authorization. A clear request such as 'yes, give me five of those' means "
                    "prepare the quote for that product and quantity without another code "
                    "confirmation. If multiple products are possible, ask which one. "
                    "When saying a product code aloud, say each digit separately: AUDIO-100 is "
                    "'audio, one zero zero'. Never speak the hyphen or punctuation as a pause, "
                    "or group the digits as 'ten zero'. State the code once and only repeat it "
                    "if the caller asks. When a quote is returned, read the customer name, "
                    "product name, quantity, and total; do not repeat the SKU unless asked. Its "
                    "quote already requests confirmation; communicate that request exactly once. "
                    "After confirmation, "
                    "submit it to the backend and give the caller the generated order number "
                    "and customer name only after the backend confirms creation. "
                    "A request to list SKUs means list the products and their product codes, "
                    "not explain the term SKU. Read product codes slowly, for example "
                    "AUDIO-101 as audio, one zero one. If a request is garbled or a number "
                    "is ambiguous, ask the caller to repeat the full product and quantity. "
                    "Do not guess a product from a bare number or submit an invented request. "
                    "Never repeat literal dashboard instructions such as Buy two angle-bracket "
                    "SKU; instead ask for the product code and quantity in plain speech. "
                    "If status is pending, call check_support_result. Its fixed Request Start "
                    "message tells the caller you are still checking; do not add another status "
                    "message. Do not call resolve_support again to poll. "
                    "Check at most three times; if still pending explain that it is not complete "
                    "and ask whether the caller wants to keep waiting or needs anything else. "
                    "Never claim an action succeeded while pending "
                    "or failed. Read the backend quote, quantity and total accurately. "
                    "A quote is NOT an order. After reading the quote, ask the caller to say "
                    "confirm order. A generic yes after the quote does not authorize an order; "
                    "ask for the exact phrase. "
                    "Submit the caller's next actual confirmation using resolve_support, "
                    "then report only the returned outcome and order number. "
                    "All commerce is mock. Human review is an internal queue, not a live "
                    "phone transfer. Never claim to charge money or transfer a call. "
                    "For inbound phone calls, the server may require caller verification. "
                    "Obtain explicit verbal consent before processing a phone call; the backend "
                    "enforces this before account workflows. Call resolve_support after every "
                    "phone caller response until the server confirms consent and identity "
                    "verification, including the caller's consent and verification-code turns. "
                    "A six-digit email verification code is verification only, never an order "
                    "number or support lookup. After resolve_support returns consent_granted "
                    "or verified, do not call resolve_support again for that same caller turn; "
                    "acknowledge the result and wait for the next caller turn. If the caller "
                    "says yes to a direct offer to list products, call resolve_support once and "
                    "provide the list without asking them to confirm the request again. "
                    "Read the server's verification instruction without revealing account "
                    "details. Ask callers to say each verification-code digit separately. "
                    "Never ask for a password or payment information. When the server confirms "
                    "verification, ask the caller to repeat their support request. "
                    "Do not say goodbye or end the call after answering one question or "
                    "completing one order. After every completed request, ask whether the caller "
                    "needs anything else and wait for their answer. Continue on the same call "
                    "for follow-up questions and additional orders. End only after the caller "
                    "clearly says they are finished or asks to hang up; if that is ambiguous, "
                    "ask before ending. Only then say exactly: Thanks for calling ResolveAI. "
                    "Goodbye for now. Never use that closing phrase while work is pending."
                ),
            }],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": {"type": "object", "properties": {}},
                    },
                    "messages": [{
                        "type": "request-start",
                        "content": request_start_message,
                        "blocking": True,
                    }],
                }
                for name, description, request_start_message in (
                    (
                        "resolve_support",
                        "Process the latest actual user utterance.",
                        "One moment while I check that for you.",
                    ),
                    (
                        "check_support_result",
                        "Read the result of the latest support workflow.",
                        "I'm still checking the result for you.",
                    ),
                )
            ],
        },
    }
