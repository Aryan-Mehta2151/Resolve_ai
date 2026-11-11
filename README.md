# ResolveAI

ResolveAI is an AI customer-support platform built as a modular monolith. Development proceeds one approved module at a time. This repository contains **Modules 1–7 plus Module 8 browser and inbound Vapi calling**: project foundation, authentication, mock commerce data/APIs, LangGraph workflows, an in-process MCP tool catalog, tenant-scoped managed knowledge retrieval, demo business workflows, and their dashboard UI. Voice requires external Vapi setup; live phone transfers are not enabled.

## Architecture

- `apps/api`: FastAPI application, configuration, health checks, async SQLAlchemy models, Alembic migrations, authentication, tenant-scoped commerce APIs, LangGraph conversation workflows, and MCP tools.
- `apps/worker`: Celery worker using Redis as its broker and result backend to execute agent workflows.
- `apps/web`: Next.js App Router support dashboard and same-origin API proxy.
- `infra/compose.yaml`: local PostgreSQL with pgvector, Redis, optional MinIO, API, worker, and web services.
- `infra/docker`: application container definitions and PostgreSQL initialization.

PostgreSQL is the source of truth. Redis is used for background-job transport. Knowledge files can use local MinIO or AWS S3. No paid API or external provider credentials are needed for Module 1.

## Prerequisites

- Docker Desktop with the Linux container engine running
- Node.js 22.14+ or 24+ and npm (required by the Vapi browser SDK)
- Python 3.12 or newer for local backend tests

## Start the local stack

From the repository root in PowerShell:

```powershell
Copy-Item .env.example .env
docker compose --profile minio --env-file .env -f infra/compose.yaml up --build
```

The `.env.example` values are for local development only. Do not use them outside a local machine. `.env` and the MinIO license file are ignored by Git. Before using authentication, generate a private JWT key and opt in to local organization registration:

```powershell
$secret = python -c "import secrets; print(secrets.token_urlsafe(48))"
(Get-Content .env) -replace '^JWT_SECRET_KEY=.*$', "JWT_SECRET_KEY=$secret" -replace '^ALLOW_SELF_REGISTRATION=.*$', 'ALLOW_SELF_REGISTRATION=true' | Set-Content .env
```

Self-registration creates a new organization and its first Admin. Disable it again after initial setup or in any shared environment. Organization Admins can create Support Agent, Supervisor, Viewer, or additional Admin accounts through the membership API. Account invitation emails are not implemented yet.

The local MinIO service is optional and requires a license file. Request the free-tier license from [MinIO AIStor](https://min.io/pricing), save it as `infra/docker/minio.license`, and keep it out of source control. In `.env`, `MINIO_LICENSE_FILE=./docker/minio.license` is resolved relative to `infra/compose.yaml`. The Compose file pins the official AIStor release documented as stable; see the [container installation guide](https://docs.min.io/aistor/installation/container/install/). The previous community image is no longer available from Docker Hub.

For an externally hosted demo, AWS S3 is the recommended object-storage backend. Create the bucket in advance, set `KNOWLEDGE_STORAGE_BACKEND=aws_s3`, leave `S3_ENDPOINT_URL` empty, and set `S3_REGION_NAME` and `KNOWLEDGE_BUCKET` in `.env`. The API and worker use boto3's standard AWS credential chain, so prefer an attached IAM role in AWS; for local testing, use `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in the ignored `.env` file, never in chat or source control. The IAM identity needs `s3:ListBucket`, `s3:GetObject`, `s3:PutObject`, and `s3:DeleteObject` for the configured bucket. Do not start the `minio` Compose profile when using AWS S3. Readiness checks verify access to the configured bucket, and AWS buckets are not created automatically.

Once services are healthy:

- Dashboard: <http://localhost:3000>
- API health: <http://localhost:8000/health/live>
- Dependency readiness: <http://localhost:8000/health/ready>
- OpenAPI docs: <http://localhost:8000/docs>
- MinIO console (when the `minio` profile is enabled): <http://localhost:9001>
- Mailpit email inbox (local verification links): <http://localhost:8025>

The readiness endpoint checks PostgreSQL, Redis, and the selected object-storage bucket. It returns HTTP 503 with per-dependency status until all are available. The liveness endpoint does not require dependencies. After PostgreSQL starts, apply the schema migration once:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic -c /workspace/apps/api/alembic.ini upgrade head
```

## Dashboard

The dashboard at <http://localhost:3000> is the interactive UI for Modules 1–8 and browser calling, with inbound phone calls listed in call history when configured. It uses the real authenticated API and shows explicit empty, error, and dependency-readiness states rather than substituting sample records.

- Register or sign in, then explore the overview, customer directory, mock orders and read-only payment/refund details, and support tickets.
- Start an AI conversation, optionally attach a customer, submit a question, and follow workflow status and events while the worker responds.
- Search knowledge and inspect citations. Admins and Supervisors can upload documents, view version history, upload a new version, retry failed ingestion, and request deletion.
- A fictional sample policy pack is available from the Knowledge Base screen for an Admin or Supervisor to download and upload manually. It is never ingested automatically; use it only for local demonstration and replace it with approved policies before real use.
- Admins can list workspace memberships and add team members. Role restrictions remain enforced by the API.
- Staff with customer-support access can provision a customer profile from the Customers screen. The customer can then create a separate portal account using that exact email.
- The UI remembers the active conversation in the browser session; the human-review queue is loaded
from the tenant-scoped conversation-list endpoint.

The web app proxies API calls through same-origin Next.js route handlers. Access and refresh tokens are kept in HTTP-only cookies; browser JavaScript does not receive bearer tokens. In Compose, the proxy connects to `http://api:8000` through `API_INTERNAL_BASE_URL`. For host-based local development, the default is `http://localhost:8000`.

Mutating proxy requests and sign-out require an `Origin` matching the incoming `Host`
and request protocol. This uses the browser-facing host rather than Next.js's internal
Docker bind address (`0.0.0.0`); missing origins and other sites, ports or protocols remain
blocked. Forwarded-host headers are not used to authorize requests.

## Customer account portal

Customers use <http://localhost:3000/customer>, separate from the staff dashboard. A staff member must first provision the customer profile in the dashboard's **Customers** screen. Customer sign-up requires that profile's email address and the organization's slug; a verification link is sent before the account can sign in. The customer account is linked by the verified email to that existing profile, and the customer role can access only its own profile, orders, and voice calls. Customer voice sessions derive the customer ID from the authenticated account, never from the browser request.

Local Compose starts Mailpit for verification email. Open <http://localhost:8025> to retrieve the link; SMTP is reachable by the API container on `mailpit:1025` and its host port is bound to localhost. For production, configure `CUSTOMER_EMAIL_SMTP_HOST`, `CUSTOMER_EMAIL_SMTP_PORT`, `CUSTOMER_EMAIL_SMTP_USERNAME`, `CUSTOMER_EMAIL_SMTP_PASSWORD`, `CUSTOMER_EMAIL_SMTP_STARTTLS`, and `CUSTOMER_EMAIL_FROM_ADDRESS` for your mail provider. Set `CUSTOMER_EMAIL_VERIFICATION_URL` to the public HTTPS customer portal URL ending in `/customer`. Do not expose Mailpit or use its local mail settings in production.

The worker health check sends a targeted Celery control ping to the container's own worker
and requires a `pong`. It avoids CLI task-discovery overhead and waiting for other workers,
which can otherwise exceed the health-check deadline on local Docker installations.

Knowledge upload and search require `OPENAI_API_KEY`, and ingestion requires a running worker. If those services or credentials are unavailable, the UI reports the API's error instead of presenting mock success. The commerce lists are empty until the organization has been seeded as described above.

Run frontend checks from `apps/web`:

```powershell
npm run lint
npm run typecheck
npm test
npm run build
```

Future approved modules should be delivered as backend-and-UI vertical slices: each module should include its usable UI, role-aware interactions, clear empty/error states, and focused verification before moving on.

### Browser voice calling (Vapi)

Open **Calling** in the dashboard. This is a real microphone/voice integration, not a simulated
chat animation. It remains disabled until configuration is present. No call is placed on page
load; starting requires a staff role, microphone permission and explicit processing consent.
Paid provider usage may apply. Only use fictional demo data.

1. Create a [Vapi account](https://dashboard.vapi.ai), obtain its **public** web key, and restrict
   that key to the intended dashboard origins in Vapi. Do not put a Vapi private key in the
   browser or paste credentials into chat. This integration does not need a Vapi private key.
2. Expose the local API on port 8000 through an HTTPS tunnel or deploy it behind HTTPS.
   Set `VAPI_PUBLIC_API_URL` to its public base URL in the ignored `.env`. A VS Code port
   forward stops whenever VS Code closes; prefer a persistent [Dev Tunnel](https://aka.ms/devtunnels/docs),
   whose URL stays the same across restarts:

   ```powershell
   devtunnel user login -g                       # once
   devtunnel create resolveai-voice --allow-anonymous
   devtunnel port create resolveai-voice -p 8000 --protocol http
   devtunnel host resolveai-voice                # run this again after every reboot
   ```

   The Calling panel checks `VAPI_PUBLIC_API_URL/health/live` before allowing a call and shows
   "Voice backend is unreachable" while the tunnel is down. Persistent dev tunnels expire after
   30 days of inactivity; run `devtunnel host resolveai-voice` regularly or recreate it.
   Expose only the webhook path if the tunnel supports path restrictions; do not publish local
   registration, documentation or development infrastructure to the Internet.
3. Generate a random token of at least 32 characters and save it as `VAPI_WEBHOOK_SECRET` in
   `.env`. In Vapi **Settings / Integrations / Server Configuration**, create a **Custom
   Credential** with that same token, header `Authorization`, and the `Bearer` prefix enabled.
   Note the credential ID; it is not the secret itself.
4. While signed into the dashboard as staff, open
   `http://localhost:3000/api/backend/api/v1/voice/assistant-template?credential_id=YOUR_CREDENTIAL_ID`.
   Use the returned JSON to configure a **saved** Vapi assistant (via the Vapi dashboard/API).
   Keep both function tools, the system instructions, `serverMessages`, the 20-second server
   timeout, and `artifactPlan.recordingEnabled=false`. The saved assistant's server URL must be
   `https://YOUR_PUBLIC_API/api/v1/voice/webhook` and reference your Custom Credential.
   Select available transcriber/voice providers in Vapi. Do not override the server URL from
   the browser: Vapi withholds stored credentials for request-supplied server URLs.
5. Set `VAPI_ASSISTANT_ID` to that saved assistant ID, `VAPI_PUBLIC_KEY` to the public web key,
   and `VAPI_ENABLED=true`. Restart API/worker after environment changes:

   ```powershell
   docker compose --env-file .env -f infra/compose.yaml up -d --force-recreate api worker
   docker compose --env-file .env -f infra/compose.yaml exec -T api alembic -c /workspace/apps/api/alembic.ini upgrade head
   ```

6. Refresh **Calling**, choose Jamie Rivera, accept the consent checkbox, and click **Start
   browser call**. Ask "Where is order ORD-10045?" or a policy question. Click **End call** when
   finished. Calls and stored support-tool turns appear in the tenant-scoped call history.
   The supplied template disables recordings; independently verify that setting in your saved
   assistant. Vapi and its speech/model vendors may retain logs according to their settings.
   ResolveAI does not download recordings or persist the complete provider end-of-call transcript.

The browser passes a short-lived, random `resolve_session` template variable. Authenticated
webhooks bind that session to one Vapi call and its server-selected tenant, customer and staff
actor. The token is stored only as a hash, is not placed in the assistant prompt, and expires
after 15 minutes; the template limits calls to 10 minutes. Vapi must include
`call.assistantOverrides.variableValues` in browser webhooks and `artifact.messages` for tool
requests. Missing binding/transcript data fails closed rather than trusting model-supplied text.
Inbound calls use the Vapi `phoneNumberId` to resolve a registered tenant number; caller ID
alone never identifies or authorizes a customer.

`resolve_support` uses the actual latest provider user transcript, not tool arguments. It
queues the same durable LangGraph workflow and MCP tools as chat. Duplicate tool IDs return
the original workflow result; overlapping requests do not start parallel turns.
Vapi speaks one fixed Request Start message before each tool request, so callers hear that a lookup
or order is being handled instead of waiting in silence. The assistant must not generate a second
progress message around the tool call. It resolves a direct reference such as "those" to the single
product just discussed, but asks which one if the conversation contains multiple possible products.
After each finished request, the assistant asks whether the caller needs anything else; it only uses
the configured specific closing phrase after the caller confirms they are finished.
The webhook waits at most eight seconds (configurable up to fifteen), then returns an explicit
`pending` status. `check_support_result` reads the latest result without repeating the action.
The voice assistant must not claim success while work is pending or failed. Stock, refunds,
permissions, ownership and explicit-confirmation rules remain enforced in backend code.
Voice transcription may add punctuation or misrecognize commands: if confirmation is rejected,
use the chat dashboard rather than weakening confirmation validation.
Explicit spoken catalog requests such as "list the SKUs", "show S K U codes", or
"what products do you have?" route directly
to the product-list tool without a model classification call. The voice assistant is instructed
to read product codes clearly and clarify garbled speech or ambiguous bare numbers rather than
guessing a product or quantity. This does not make speech recognition error-free or infer an
order from an incomplete utterance.
An affirmative reply to a direct offer to list products routes to the catalog tool. A six-digit
phone verification code is never treated as an order number; replayed tool calls for the
already-verified code return the verification result instead of starting an order lookup.

After publishing an assistant, reload it in Vapi and verify the published version still has
both `resolve_support` and `check_support_result` attached and the complete system prompt
from the assistant template, not the blank-template prompt. A connected call alone does not
prove backend tool access. Product lists and order outcomes must come from completed tool
results; if a tool fails, the assistant must say the request could not be completed and must
not claim the backend is inaccessible or that an action succeeded.
Changes to ResolveAI's assistant template are not pushed to Vapi automatically. Apply the
latest template to the saved assistant and publish a new version before expecting updated
call behavior. Verify both tools' static Request Start messages and the specific end-call message
and phrase remain in the published version.
For a voice order, select the customer before starting the call; a customer is required by the
order schema. Tell the assistant which product and quantity you want; it should carry an
unambiguous product reference such as "those" forward without asking for the product code again.
It should ask only for any missing detail, then read the resolved product and total and request
confirmation once. Say `confirm order` to authorize creation. The demo supports one product per order, but the
caller can place multiple separate orders in the same call; each needs its own quote and explicit
confirmation. The customer does not need an existing order number to place a new order: the
backend generates the order number after creation, and the assistant gives it as the receipt/reference.
A generic "yes" is not authorization. Verify creation using the returned order number and the
dashboard Orders list, not a spoken acknowledgement.
Product quotes first match exact SKUs or full names. If neither matches, a contiguous
whole-word phrase such as "wireless headphones" can match an active product name only
when unique within the workspace. Ambiguous descriptions require the exact SKU; the
quote still shows the resolved product and requires explicit confirmation before creation.
The template uses a 120-second silence timeout (rather than the provider's 30-second default)
to allow for backend requests and polling; the 10-minute call limit still applies. Idle time
can still incur provider usage charges. Model classification and response generation each
have an overall `AGENT_MODEL_TIMEOUT_SECONDS` deadline, in addition to transport timeouts,
so a stalled model invocation fails explicitly rather than leaving a workflow running.

Human escalation remains a dashboard review queue, not a live phone handoff. Ending a browser
session revokes new tool requests but does not cancel a workflow already accepted. Closing
the page disconnects audio; an abrupt browser crash may leave the local history entry stale
until a provider lifecycle event arrives or the session expires. Recording download, outbound
dialing, automatic transfer, full spoken transcript archival and a production retention policy
are not implemented in this slice.

Voice API routes:

- `GET /api/v1/voice/config`: local configuration readiness (not a provider connectivity test).
- `GET /api/v1/voice/phone-numbers` and `POST /api/v1/voice/phone-numbers`: Admin-only,
  tenant-scoped inbound-number mapping; the selected service user must be active support staff.
- `DELETE /api/v1/voice/phone-numbers/{id}`: Admin-only soft deactivation.
- `GET /api/v1/voice/assistant-template?credential_id=...`: saved-assistant JSON, staff only.
- `POST /api/v1/voice/calls`: `{customer_id, consent: true}`, staff only.
- `GET /api/v1/voice/calls?limit=50&offset=0`: tenant-scoped call history.
- `POST /api/v1/voice/calls/{id}/end`: revoke the local demo session; browser SDK stops audio.
- `POST /api/v1/voice/webhook`: Vapi Custom Credential bearer authentication, not a dashboard JWT.

Tests use deterministic Vapi-shaped fixtures, require no paid API, and cover verification,
tenant isolation, consent/RBAC, replay, pending/completed/failed tools, monotonic lifecycle,
and catalog-to-quote-to-confirm order persistence with a single stock deduction.
An actual Vapi microphone call is credential-gated and must be verified after setup; automated
fixtures alone do not certify live provider connectivity or voice latency.

Provider references: [web SDK](https://docs.vapi.ai/quickstart/web),
[server events](https://docs.vapi.ai/server-url/events), and
[credential authentication](https://docs.vapi.ai/server-url/server-authentication).

### Inbound phone calling (Vapi)

The same saved assistant and authenticated webhook support inbound PSTN calls. Vapi may offer
one free US number per account; availability is account-dependent, it is inbound-only, and
call usage is billed. For a non-US number or outbound calling, import an eligible carrier number.
See [Vapi phone calling](https://docs.vapi.ai/phone-calling) and
[Vapi free telephony](https://docs.vapi.ai/free-telephony).

1. In Vapi, create or import an inbound number and assign the saved ResolveAI assistant to it.
   The assistant ID must match `VAPI_ASSISTANT_ID`. Do not use dynamic assistant routing for this
   initial setup. Copy the provider's **phone number ID** (not just the displayed number).
2. In ResolveAI, register the number as an Admin. Set the customer profile's phone to E.164
   format (for example `+15551234567`) and complete customer email verification before testing.
   Choose an active Admin, Supervisor, or Support Agent as the tenant's service actor:

   ```powershell
   $me = Invoke-RestMethod -Uri http://localhost:8000/api/v1/auth/me -Headers $headers
   $phone = @{
     provider_number_id = "VAPI_PHONE_NUMBER_ID"
     phone_number = "+15559990100"
     service_user_id = $me.user.id
   } | ConvertTo-Json
   Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/voice/phone-numbers `
     -Headers $headers -ContentType "application/json" -Body $phone
   ```

   SMTP must be configured for the one-time caller-verification email; local Compose uses Mailpit.
   The returned mapping is scoped to the Admin's organization. A number ID cannot be assigned to
   multiple tenants. `GET /api/v1/voice/phone-numbers` lists that tenant's mappings; DELETE
   deactivates one without removing call history.
3. Call the Vapi number from a phone whose E.164 number is attached to one verified customer
   profile. The assistant asks for verbal consent before any account workflow. The backend then
   emails a short-lived six-digit verification code to the customer's verified email. Callers
   must provide the code before any account-specific workflow runs. Unknown, ambiguous, or
   unverified callers fail closed; caller ID alone is never authorization. Codes are hashed in
   PostgreSQL, expire after five minutes by default, and allow at most five attempts.
4. Confirm the phone call appears with channel `phone` in tenant call history and, after
   verification, in that customer's portal history. Browser calls remain unchanged.

The supplied assistant disables ResolveAI call recording. The spoken verification code may
still appear in Vapi/provider transcripts or logs; check provider retention controls before live
use and avoid real sensitive information in demos. Disabling a mapping stops new calls to it;
it does not cancel calls already in progress. Human transfer and outbound calls are not enabled.

### Creating demo orders through chat

Seed commerce data for your workspace first using the command below. Start a conversation with a
customer attached, ask `list products`, then say `Buy 2 AUDIO-101` (or another listed SKU).
Admins, Supervisors, and Support Agents can prepare an order summary; Viewers can only browse.
Review the customer name, product, quantity, and USD total, then say `confirm order`.
The server selects the latest summary in that conversation; internal quote references stay hidden.
Summaries expire after 15 minutes. Product availability and price are
checked again at confirmation; changes require a new summary. Repeating confirmation for the
same summary returns the existing order rather than creating another.
SKU matching ignores letter case, spaces, and hyphens (for example, `audio 101` matches
`AUDIO-101`); ambiguous matches are rejected rather than choosing a product automatically.

The order and line item are saved to the workspace database and stock is reduced atomically.
The dashboard reloads Orders when the chat workflow completes, so no manual order entry is needed.
This demo supports one product per order, quantities from 1 to 100, and USD catalog prices without
shipping or tax calculations. Orders remain pending; no payment, fulfillment, or real purchase
is executed. Order summaries and confirmations use server-composed replies, not model-generated
claims of success.

The dashboard automatically rotates its refresh cookie when a protected API request returns 401,
including after the browser removes an expired access cookie. Concurrent refreshes are coordinated
within one web-server process; multi-instance hosting needs shared coordination or a dedicated
session service. Expired or revoked refresh tokens still require signing in again.

## Authentication and tenancy

- `POST /api/v1/auth/register` creates an organization and its initial Admin when `ALLOW_SELF_REGISTRATION=true`.
- `POST /api/v1/auth/login` returns short-lived access and refresh tokens; supply `organization_id` when the account belongs to more than one organization.
- `POST /api/v1/auth/refresh` rotates a single-use, database-tracked refresh token.
- `GET /api/v1/auth/me` resolves the active user, organization, and current membership role from the database.
- `GET /api/v1/organizations` lists organizations where the authenticated user has an active membership.
- Admin-only `GET` and `POST /api/v1/organizations/{organization_id}/memberships` list members and create accounts for the current organization.

Passwords require at least 12 characters and are stored using Argon2. JWT signing requires `JWT_SECRET_KEY` to have at least 32 characters. Access-token authorization checks active user, organization, and membership status on every request; role decisions use the current database membership rather than trusting token role claims.

Apply migrations from the repository root with `alembic -c apps/api/alembic.ini upgrade head` when the configured PostgreSQL database is reachable. Use `alembic -c apps/api/alembic.ini downgrade -1` only when intentionally reverting the latest schema migration.

## Commerce data and APIs

Commerce records are scoped to the organization in the access token. Composite foreign keys prevent linking an order, product, customer, payment, or support ticket across tenants. The seed command adds mock records to an **existing active organization** and is safe to run repeatedly:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api python -m app.modules.commerce.seed --organization-slug demo-support
```

Replace `demo-support` with the slug of an organization created through registration. The command prints the number of newly created customers, products, orders, shipments, payments, and tickets; zero counts on a repeat run indicate the records were already present.

Available endpoints (all require a valid bearer access token):

- `GET /api/v1/customers` and `GET /api/v1/customers/{customer_id}`
- `GET /api/v1/customers/{customer_id}/orders`
- `GET /api/v1/products` (supports `search`, `limit`, and `offset`) and `GET /api/v1/products/{product_id}`
- `GET /api/v1/orders`, `GET /api/v1/orders/{order_id}`, and `GET /api/v1/orders/by-number/{order_number}`
- `GET /api/v1/orders/{order_id}/shipments`, `/payments`, and `/refunds`
- `GET /api/v1/support/tickets` and `GET /api/v1/support/tickets/{ticket_id}`
- `POST /api/v1/support/tickets` (Admin, Supervisor, or Support Agent); the customer and optional order must belong to the active organization, and the order must belong to that customer.

For example, after registering the `demo-support` organization and seeding it, use the returned access token to query its demo order:

```powershell
$headers = @{ Authorization = "Bearer $($tokens.access_token)" }
Invoke-RestMethod -Uri http://localhost:8000/api/v1/orders/by-number/ORD-10045 -Headers $headers
```

The original payment/refund endpoints are read-only. Module 7 provides separate, explicit demo
action and approval endpoints described below.

## Agent workflow foundation

Authenticated users can start tenant-scoped conversations and submit messages for asynchronous processing by the Celery worker:

- `POST /api/v1/conversations` creates a conversation; an optional `customer_id` must belong to the active organization.
- `GET /api/v1/conversations/{conversation_id}` returns conversation status and a paginated message history.
- `POST /api/v1/conversations/{conversation_id}/messages` stores a message and queues a workflow, returning HTTP 202 with its workflow ID.
- `GET /api/v1/workflows/{workflow_id}` returns status and persisted workflow events.
- `GET /api/v1/workflows/{workflow_id}/events` returns paginated events in sequence order.

The LangGraph flow classifies intent, prepares a bounded response, and persists checkpoint state to PostgreSQL through LangGraph's PostgreSQL checkpointer. The worker initializes the checkpointer schema on first use; these internal checkpoint tables are managed by that library rather than by Alembic. The worker also uses a PostgreSQL advisory lock to prevent duplicate concurrent execution of the same workflow. On Windows, its async task runner uses a selector event loop for psycopg compatibility.

The default intent provider is deterministic, so local workflows run without external credentials. To opt in to OpenAI structured intent classification and natural-language response generation, set `AGENT_INTENT_PROVIDER=openai` and provide `OPENAI_API_KEY`; `OPENAI_MODEL` and `AGENT_MODEL_TIMEOUT_SECONDS` configure the model and request timeout. In OpenAI mode, the latest customer message is sent for intent classification; up to 10 recent conversation messages and verified tool or knowledge results are sent to generate the reply. Do not include sensitive data unless you are authorized to process it through your configured OpenAI account. The response generator is instructed to use only verified tool results or cited, approved knowledge for factual claims, and to avoid claiming a refund, cancellation, or other action without a successful tool result. A missing key or provider failure marks the workflow failed rather than silently switching providers.

The graph is bounded by a step limit and stores at most 20 recent messages in its checkpoint
state. Responses claim an action only after a successful tool result. Module 7 adds explicit
mock cancellation/refund requests; no real commerce or payment provider is called.

## MCP tool catalog

The worker uses the official MCP Python SDK's in-process server/client to invoke typed, mock-backed tools from LangGraph. The server is created for a workflow and is not exposed as a public MCP HTTP endpoint.

Current tools:

- `commerce_get_order_status` reads order and shipment status for the workflow's organization.
- `commerce_list_products` lists active demo products, USD prices, and available stock.
- `commerce_quote_demo_order` persists a customer-linked summary without creating an order.
- `commerce_confirm_demo_order` verifies the exact confirmation message and creates a pending
  demo order atomically, with replay protection and no real payment.
- `commerce_get_refund_status` reads payment/refund status and is restricted to Admin, Supervisor, and Support Agent roles.
- `support_create_ticket` creates a ticket for the customer already linked to the conversation; it is role-restricted and idempotent per workflow.

The worker derives tenant, actor, conversation, and role from persisted workflow and current membership records. These values are never tool/model arguments. Inputs and outputs are typed, tool calls have bounded timeouts, and execution metadata is persisted in `tool_executions` and ordered workflow events without logging raw arguments or customer content. Ticket writes and their audit record are committed together.

The LangGraph flow selects tools using classified intent and server-validated arguments.
The MCP boundary is in-process only; it does not accept external MCP hosts. Customer identity
verification, real payment adapters, and notification tools are not implemented.

## Module 7: demo business workflows

Set `DEMO_BUSINESS_WORKFLOWS_ENABLED=true` for local demonstrations. It defaults to false in code;
keep it disabled when connecting real commerce. Apply the latest Alembic migration before use.
All actions are tenant-scoped; Support Agents, Supervisors, and Admins can request them, while
only Supervisors/Admins can decide refunds. Viewers can inspect status but cannot perform actions.

- **Tracking:** existing order/shipment tools continue to report stored facts.
- **Cancellation:** ask about cancelling an order, then explicitly send
  `confirm cancel ORD-D-...`. Only unpaid demo chat orders in pending/processing state without any
  shipment qualify. Cancellation and stock restoration commit together, exactly once. Paid,
  shipped, and non-demo orders require manual handling; no refund is implied by a cancellation.
- **Refund:** attach the order's customer and send `request refund ORD-10045`. This creates a
  durable request for the remaining eligible mock payment amount, reserving against existing
  pending/successful refunds. It does not move money. In **Orders → Business actions**, a
  Supervisor/Admin approves or rejects with a note. Approval simulates refund success and updates
  the mock payment; rejection leaves payment unchanged. Repeating requests/decisions replays the
  original outcome; conflicting decisions are rejected. One refund request per order is supported
  in this demo, including after rejection. Admins may review their own requests for solo demos.
- **Troubleshooting:** use the existing cited knowledge path for product/support questions.
  Upload approved troubleshooting instructions first; absent evidence must not become invented
  company policy. No device-control or diagnostic tool is executed.
- **Escalation:** asking for a human marks the conversation escalated and stops assistant messages.
  **AI conversations → Needs human review** lists the queue. Support staff can inspect the chat
  and, with an audited note, return it to the assistant or mark it resolved. This is an internal
  review queue, not a live phone transfer or notification.

Dashboard action forms require a reason and explicit confirmation. Chat writes validate the
actual stored customer message, not merely a model-generated intent. Action records retain the
requester, reviewer, decision note, status, and timestamps. Approval is a separate short database
transaction, rather than keeping a worker or LangGraph invocation running while awaiting review;
subsequent status lookups reflect the result. No automatic assistant message is sent on approval.

Endpoints:
- `POST /api/v1/orders/{order_id}/actions` (`kind`, `reason`, `confirmed`)
- `GET /api/v1/business-actions` (paginated)
- `POST /api/v1/business-actions/{action_id}/decision` (`approve`, `note`)
- `GET /api/v1/conversations?status=escalated` (paginated summaries)
- `POST /api/v1/conversations/{conversation_id}/review` (`status`: `open`/`resolved`, `note`)

Manual demo: request a refund for seeded `ORD-10045`, approve it in Orders, then ask for its refund
status. Separately create a chat order and cancel it, verifying stock and order status. Ask for
a human, inspect the queue, and return the conversation to the assistant. No live provider
transactions are involved.

Example, after registering and authenticating, submit a simulated delivery question and poll its status:

```powershell
$conversation = Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/conversations -Headers $headers -ContentType "application/json" -Body "{}"
$message = @{ content = "Where is order ORD-10045?" } | ConvertTo-Json
$workflow = Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/v1/conversations/$($conversation.id)/messages" -Headers $headers -ContentType "application/json" -Body $message
Start-Sleep -Seconds 2
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/workflows/$($workflow.id)" -Headers $headers
```

The API and worker must both be running, and the worker image should be rebuilt after dependency-manifest changes so it contains the LangGraph, MCP, and PostgreSQL checkpointer packages.

## Knowledge documents and retrieval

Module 6 uses OpenAI File Search as the first managed retrieval provider, behind a replaceable provider interface. Each organization receives a separate managed vector store; tenant IDs and provider store IDs are resolved by the server and cannot be supplied in search requests. Uploaded originals are stored in the configured S3-compatible bucket, while PostgreSQL tracks document versions, ingestion state, and provider references.

Configure `OPENAI_API_KEY` in `.env` to enable ingestion and retrieval. The existing `OPENAI_MODEL` setting is used for grounded knowledge answers. Uploaded documents and search questions are sent to OpenAI when this integration is enabled; only upload material you are authorized to share with that provider. Without a configured key, knowledge endpoints that need the provider return HTTP 503, while the rest of the local demo remains available.

Admin and Supervisor users can upload and version PDF, DOCX, Markdown, and UTF-8 text files (10 MiB maximum by default), inspect ingestion status, retry failed ingestion, and request deletion. Ingestion/deletion run in the Celery worker. All active authenticated roles can list documents and test search. Search returns provider citations only when they map back to a current, ready document version in the caller's organization; unsupported or uncited answers use a safe "not found" response. General conversation questions use the same grounded retrieval path when the organization has ready knowledge.

Available endpoints:

- `POST /api/v1/knowledge/documents` (multipart: `title`, optional `description`, and `file`)
- `GET /api/v1/knowledge/documents` and `GET /api/v1/knowledge/documents/{document_id}`
- `POST /api/v1/knowledge/documents/{document_id}/versions`
- `POST /api/v1/knowledge/documents/{document_id}/versions/{version_id}/retry`
- `DELETE /api/v1/knowledge/documents/{document_id}`
- `POST /api/v1/knowledge/search` with `{"query":"What is the return period?"}`

The managed provider treats document text as untrusted evidence, not instructions. The API exposes citations from search results and the agent adds source titles to grounded responses. Tests use deterministic provider and object-storage fixtures; they do not require provider credentials. Run the focused coverage with `python -m pytest apps/api/tests/test_knowledge.py`.

Manual verification after the stack is ready and migrated:

```powershell
$body = @{
  organization_name = "Demo Support"
  organization_slug = "demo-support"
  full_name = "Demo Admin"
  email = "admin@example.com"
  password = "Correct-Horse-Battery-1!"
} | ConvertTo-Json
$tokens = Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/v1/auth/register -ContentType "application/json" -Body $body
$headers = @{ Authorization = "Bearer $($tokens.access_token)" }
Invoke-RestMethod -Uri http://localhost:8000/api/v1/auth/me -Headers $headers
Invoke-RestMethod -Uri http://localhost:8000/api/v1/organizations/$($tokens.organization.id)/memberships -Headers $headers
```

Log in with `POST /api/v1/auth/login`, refresh once with `POST /api/v1/auth/refresh`, and verify that replaying the old refresh token returns HTTP 401.

Stop services with `docker compose --env-file .env -f infra/compose.yaml down`. Add `-v` only when you intentionally want to remove local database, Redis, and object-store data.

## Run backend tests locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e "apps/api[dev]"
python -m pytest apps/api/tests
python -m ruff check apps/api apps/worker
```

## Run the web app locally

```powershell
Set-Location apps/web
npm ci
npm run dev
```

Then open <http://localhost:3000>. Run `npm run lint`, `npm run typecheck`, and `npm run build` to validate the frontend.

## Module boundary

Modules 1–8 do not include password reset, email invitations, role/membership editing or suspension
endpoints, refresh-session cleanup jobs, rate limiting, external MCP transport, real business
integrations, outbound dialing, live phone transfers, or real payment/refund execution. These require
later modules and separate approval. Managed OpenAI File Search remains tenant-isolated behind
a replaceable interface. Self-registration should be disabled outside local onboarding until
abuse controls and invitation/provisioning workflows are added.
