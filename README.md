# Speed-to-Lead SMS Booking Agent

When a lead fills out a website quote form or calls and nobody answers, this agent texts them within seconds. It qualifies them over SMS (what they need, ZIP code, urgency), books a real open slot on the business's Google Calendar, and hands off to a person when it should.

Stack: Python FastAPI, Twilio, an LLM (Gemini through the `google-genai` SDK for free testing, or Claude through the Anthropic SDK, switched with `LLM_PROVIDER`), Google Calendar and Postgres. One deployment serves many businesses; each business is one row in the `clients` table.

---

## 1. How it fits together

```
Website form ──► POST /webhooks/form/{client}  ─┐
Missed call  ──► POST /webhooks/twilio/voice   ─┼─► flows.py: record event, check STOP / paused / consent
Lead texts   ──► POST /webhooks/twilio/sms     ─┘         │
                                                           ▼  (background, so Twilio gets a fast 200)
                                              agent.py: LLM tool loop (max 8 rounds)  
                                                ├─ tool_handlers.py ─► Google Calendar (busy times, booking)
                                                ├─ escalate ─► Slack / email alert + pause lead
                                                └─ guardrails ─► Twilio SMS (≤300 chars)
                                              Postgres: clients · leads · messages · slot_offers · event_log
Owner ──► /admin inbox: read threads, reply as staff (pauses agent), take over / resume
```

**Memory.** Every text in and out is a row in `messages`, keyed to one `leads` row per (business, phone number). Each turn, the agent gets the last 40 messages plus a structured summary (name, service, ZIP, urgency, slots already offered) in the system prompt. State lives in the database, so restarts lose nothing.

**Where the guardrails live.** The prompt asks Claude to behave. The code enforces anything that must never fail:

| Rule | Enforced in |
|---|---|
| STOP and other keywords: never text again | `flows.handle_inbound_sms` (before Claude is called) and `agent.send_sms` |
| Human replied or took over: agent goes silent | `Lead.paused`, checked before every run |
| Only real, open slots can be booked | `slot_offers` table + calendar re-check in `book_appointment` |
| No times until the ZIP is confirmed in-area | `get_available_slots` refuses otherwise |
| Phone number in the booking is the real sender | Set by the server, never by Claude |
| Texts ≤ 300 characters, no IDs or markup | `agent.problem_with`: one rewrite, then a safe fallback |
| Text stays in the cheap GSM-7 encoding | `agent.sanitize` converts curly quotes and dashes to ASCII |
| First text within 10 seconds | 7-second deadline, then a template opener |
| Runaway loops | Max 8 tool rounds per reply; max 20 automated texts per lead per day |
| Claude or a tool errors out | Holding text + owner alert + lead paused |

## 2. Files

```
prompts/system_prompt.md   Step 1: the system prompt (loaded by the app) + example values
prompts/tools.json         Step 2: the six tool schemas sent to Claude
app/main.py                Webhooks (Twilio SMS/voice, web form) and the /admin staff inbox
app/demo.py                Live demo API at /demo (simulated phone, calendar, alerts; real LLM)
app/static/demo.html       The demo page (single file, Tailwind via CDN)
app/flows.py               What each trigger does: STOP/START/HELP, consent, paused leads, missed calls
app/agent.py               The Claude tool loop and all message guardrails
app/tool_handlers.py       Server-side tool logic (service area, slots, booking, escalation, close)
app/calendar_slots.py      Slot generation, Google Calendar adapter, fake calendar
app/prompt.py              Fills the prompt per client and per turn; prompt caching
app/adapters.py            Gemini + Claude clients (same interface), Twilio, Slack/email, fakes
app/models.py              Database tables (schema.sql is generated from these)
clients/triangle_comfort.json   Example client config (fictional HVAC company)
scripts/simulate.py        Chat with the real agent in your terminal, no Twilio or Google needed
scripts/upsert_client.py   Add or update a client from its JSON file
scripts/export_schema.py   Regenerate schema.sql
scenarios/                 Step 4 replay files for the simulator
TEST_SCENARIOS.md          Step 4: the sample conversations and pass checks
DEPLOY.md                  Put the demo online for free (GitHub + Render)
render.yaml                Render Blueprint for one-click deploys
tests/                     55 automated tests (no API keys needed)
```

## 3. Run it locally (about 15 minutes)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # add GEMINI_API_KEY; set ADMIN_TOKEN to a long random string

pytest -q                       # 55 tests, all offline

# Talk to the agent with the real model (texts and calendar are simulated):
python scripts/simulate.py --source web_form --name "Jordan Lee" --message "AC blowing warm air"
python scripts/simulate.py --scenario scenarios/01_happy_path.txt
```

Run all five scenario files and work through the checklists in `TEST_SCENARIOS.md` before showing this to anyone.

### Live web demo

```bash
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000/demo. To put it online for free with a public link, follow `DEPLOY.md`. The left side is the customer: submit the quote form, simulate a missed call, or text Ava, with character and SMS-segment counts. The right side is the owner's live view: conversation status (Active, Booked, Escalated, Opted Out), booked appointments and owner alerts, refreshed every 1.5 seconds. "Reset Demo" starts over with a fresh calendar.

The demo runs the real agent with your `LLM_PROVIDER` key. Texts, calendar and alerts are simulated, so nothing is sent and real leads are never touched, and you don't need Twilio, Google Calendar or Slack. Tomorrow 8-10am is pre-booked solid, so you can show that full windows are never offered. On a public server, set `DEMO_TOKEN` (then open `/demo?token=...`) or `ENABLE_DEMO=false`, since every demo message spends LLM requests.

## 4. Production setup

Do these in order. The Twilio registration takes the longest, so start it on day one.

### 4.1 The LLM

**Free testing with Gemini (default, `LLM_PROVIDER=gemini`):**
1. Create a free API key at https://aistudio.google.com/apikey and set `GEMINI_API_KEY`.
2. The model is `GEMINI_MODEL=gemini-3.8-flash`, with a low thinking level (`GEMINI_THINKING_LEVEL=low`) for speed.
3. The free tier has tight per-minute and per-day request limits; check yours in AI Studio. One customer reply can take 2 to 4 requests (one per tool round). On a rate-limit error the app waits 2 seconds and retries once; if that fails too, the normal safety net applies: holding text, owner alert, lead paused.
4. **Never send real customer conversations through the free tier.** Google may use free-tier prompts and responses to improve its products. Use fictional test data only, and switch to a paid tier or to Anthropic before going live.

**Production with Claude (`LLM_PROVIDER=anthropic`):**
1. Create an API key at console.anthropic.com and add billing.
2. Set `ANTHROPIC_API_KEY`. The default model is `claude-sonnet-5-5`. You can set `ANTHROPIC_MODEL=claude-haiku-5-5` to cut cost, but rerun every scenario first, since tool-use quality matters more than price here.
3. Prompt caching is already on. The fixed part of the prompt and the tools are cached, so repeat turns cost much less.

### 4.2 Database (Postgres)
1. Create a free Postgres database (Neon, Supabase or Railway).
2. Set `DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME`.
3. Tables are created automatically on startup. `schema.sql` is there if you want to review the tables or create them by hand.

### 4.3 Deploy the app
Any host that runs Python works. On Railway or Render:
1. Push this folder to a private GitHub repo and create a new web service from it.
2. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
3. Add every variable from `.env.example`. Set `PUBLIC_BASE_URL` to the service's HTTPS address, with no trailing slash.
4. Check that `https://YOUR-APP/health` returns `{"ok": true}`.
5. **Run one instance.** Per-lead locking is in memory. To scale beyond one instance, switch `agent._lock_for` to a Postgres advisory lock first.

For local Twilio testing before deploying, expose your machine with `ngrok http 8000` or `cloudflared tunnel` and use that URL as `PUBLIC_BASE_URL`.

### 4.4 Twilio (one number per client)
1. Buy a local number in the client's area code (Phone Numbers → Buy a number, with SMS and Voice).
2. **Register for A2P 10DLC** (Messaging → Regulatory Compliance). US carriers block unregistered business texting from local numbers. You register a Brand (the business) and a Campaign (this use case). Approval usually takes days to a few weeks. Your campaign will need:
   - **Description:** "Responds to customers who submit a quote request on our website or call our business, to answer questions and schedule service appointments."
   - **Sample messages:** use the two openers and a booking confirmation from `TEST_SCENARIOS.md`.
   - **How people opt in:** "Customers check an SMS consent box on our website quote form, or call our business line and receive a text reply about their call." Link to the form and the business's privacy policy.
   - **Opt-out:** STOP. **Help:** HELP.
3. On the number's configuration page:
   - **A message comes in:** Webhook, `https://YOUR-APP/webhooks/twilio/sms`, HTTP POST
   - **A call comes in:** Webhook, `https://YOUR-APP/webhooks/twilio/voice`, HTTP POST
4. Leave **Advanced Opt-Out** on (Messaging → Services or the number's settings), with `TWILIO_HANDLES_OPT_OUT=true`. Twilio then answers STOP, START and HELP and blocks texts to opted-out numbers; this app mirrors that state.
   - Watch out: **CANCEL** is a default opt-out keyword. A customer who texts "cancel" to cancel an appointment gets unsubscribed. If that matters for a client, remove CANCEL from the opt-out keywords in Twilio's Advanced Opt-Out settings and from `STOP_WORDS` in `app/flows.py`.
5. Put the number in the client's JSON as `twilio_number`.

### 4.5 Missed-call wiring (pick one per client)
Set `call_mode` in the client JSON.

- **`missed_only` (easiest, recommended):** the business keeps its existing phone number and turns on **conditional call forwarding (no answer / busy)** to the Twilio number. The dial codes vary by carrier and phone system; search "<carrier name> conditional call forwarding no answer". Unanswered calls reach Twilio, which plays a short greeting and the agent texts the caller.
- **`forward_first`:** the Twilio number becomes the number the business advertises. Twilio rings `forward_to_number` for `ring_seconds`, and only if nobody answers does the caller get a text. Use this when the business is launching a new tracking number anyway.

Callers with blocked caller ID can't be texted. Those calls are logged and skipped.

### 4.6 Google Calendar (per client, one-time)
1. In Google Cloud Console: create a project, enable the **Google Calendar API**, then create a **service account** and download a JSON key.
2. Base64-encode the key (`base64 -w0 key.json`, or `base64 -i key.json` on macOS) and set `GOOGLE_SERVICE_ACCOUNT_JSON_B64`. One service account serves all your clients.
3. Have the business open Google Calendar → the calendar's settings → **Share with specific people** → add the service account's email (it looks like `name@project.iam.gserviceaccount.com`) with **Make changes to events**.
4. Copy the **Calendar ID** from the same settings page into the client JSON's `calendar_id`.
5. Set `slot_windows` (their arrival windows per weekday), `slot_capacity` (jobs per window, usually the number of technicians), `min_notice_minutes` and `horizon_days`.
6. Anything already on that calendar blocks time, including all-day events like holidays. Events marked "Free" don't.

### 4.7 Owner alerts
- **Slack:** in the client's Slack, create an app with Incoming Webhooks turned on, add it to a channel, and paste the webhook URL into `slack_webhook_url`.
- **Email (optional):** set the `SMTP_*` variables (a Gmail app password or a SendGrid SMTP key works), plus the client's `owner_email`.
- Alerts go out for escalations, new bookings (turn off with `notify_on_booking: false`), texts from paused leads, form leads without consent, and agent errors. Each alert links to that lead's thread in the staff inbox.

### 4.8 The website form
Send a POST to `https://YOUR-APP/webhooks/form/{client-slug}` with the form secret in an `X-Form-Key` header (or `?key=` in the URL). The body can be JSON or form-encoded:

```json
{"name": "Jordan Lee", "phone": "919-555-0142", "email": "jordan@example.com",
 "message": "AC blowing warm air", "zip": "27513", "sms_consent": true}
```

- Most form tools (Jotform, Typeform, Webflow, WPForms, Gravity Forms, Zapier) can send a webhook from their own server; use that. Don't post from browser JavaScript, because the secret would be visible in the page.
- Add a hidden `website` field to the form as a honeypot: bots fill it in and get ignored.
- **The consent checkbox is required.** If `sms_consent` isn't true, nobody gets texted; the owner gets a "call them" alert instead. Suggested checkbox text:
  > By checking this box, I agree to receive text messages from Triangle Comfort Heating & Air about my request, including appointment scheduling. Message frequency varies. Msg & data rates may apply. Reply STOP to opt out, HELP for help. See our Privacy Policy.

### 4.9 Load the client and go live
```bash
python scripts/upsert_client.py clients/your_client.json   # run against the production DATABASE_URL
```
Then run the go-live checklist below.

## 5. Onboarding a new business (checklist)

1. Copy `clients/triangle_comfort.json` and fill in **every** value. Leave the FAQ to the owner and get their sign-off, because the agent can only quote facts that are in it.
2. Get the ZIP code list they serve and their arrival windows, technician count and office hours.
3. Twilio: buy the number, add it to the Brand and Campaign, set the webhooks.
4. Set up the calendar share, the Slack webhook and call forwarding.
5. Run `upsert_client.py`.
6. **Go-live test from your own phone:** submit the form, book a slot, check the calendar event; text STOP and confirm nothing else arrives; call and let it ring out; ask for a person and confirm the alert and the pause.
7. Show the owner the inbox link and how "Take over" and "Resume assistant" work.

## 6. Operating it

- **Staff inbox:** `https://YOUR-APP/admin?token=ADMIN_TOKEN`. The token is in alert links, so treat them as private. Put real login in front of `/admin` before you have many clients.
- **Everything is logged** in `event_log`: every tool call with its input and result, plus errors, opt-outs and escalations.
- **Monthly results for each client** (this is what you show them to justify the fee):

```sql
SELECT c.name,
       COUNT(*)                                            AS leads,
       COUNT(*) FILTER (WHERE l.status = 'booked')         AS booked,
       COUNT(*) FILTER (WHERE l.source = 'missed_call')    AS from_missed_calls,
       COUNT(*) FILTER (WHERE l.status = 'escalated')      AS handed_to_staff
FROM leads l JOIN clients c ON c.id = l.client_id
WHERE l.created_at >= date_trunc('month', now())
GROUP BY c.name;
```

- **Changing behavior:** edit `prompts/system_prompt.md` or the client JSON, rerun the scenarios, then redeploy. Run `pytest` after any code change.

## 7. Compliance notes (not legal advice)

- The TCPA and some state laws regulate business texting. Florida, Oklahoma and others have their own rules on consent and sending hours. Have a lawyer review your setup and your client contract before you sell this widely.
- The agent always says it's automated if asked, and never claims to be human. California and some other states require bots to disclose this in sales conversations.
- **Healthcare niches (dental, medical):** texts become protected health information under HIPAA. You'd need signed Business Associate Agreements with Twilio and Anthropic (both offer them on qualifying plans), a HIPAA-eligible host and database, and a prompt that collects as little clinical detail as possible. Don't run dental clients on this setup as-is.

## 8. Known limits and good next steps

- No reschedule or cancel tool yet; those requests escalate to a person.
- No follow-up nudges for leads who go quiet. Next feature: one polite follow-up after 24 hours, only within sending hours.
- Staff reply through the `/admin` inbox. Replying from inside Slack threads needs a Slack app with the Events API.
- Single instance only (in-memory locks); see 4.3.
- No quiet-hours setting yet. Every text is a reply to something the lead just did, but check your state's rules and add a sending window if needed.
