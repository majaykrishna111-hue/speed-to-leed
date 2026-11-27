# Launching for a real business

The free demo (`render.yaml`) is for showing people. This guide sets up the **production** service from `render.production.yaml`: an always-on server and a permanent database, about **$13/month** on Render (web $7 + Postgres $6, plus storage), plus Twilio and AI usage.

Do the steps in order. Start step 1 the day a business says yes, since carrier approval can take weeks.

## 1. Twilio number and registration (start first)
1. Create a Twilio account and upgrade it from trial; a trial account can only text numbers you've verified.
2. Buy a local number in the business's area code (with SMS and Voice).
3. Register for **A2P 10DLC** (Messaging → Regulatory Compliance) with the business as the brand. Use the campaign wording in README §4.4. Texts to real customers will be blocked until it's approved.

## 2. Deploy the production service
1. Render dashboard → **New → Blueprint** → this repo → set **Blueprint Path** to `render.production.yaml`.
2. Paste the four secrets when asked: `ANTHROPIC_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `GOOGLE_SERVICE_ACCOUNT_JSON_B64` (README §4.6 shows how to make that last one).
3. Deploy. It creates the web service `speed-to-lead` and the database `speed-to-lead-db`. The demo is switched off on this service, and the database only accepts connections from your Render services.

## 3. Point Twilio at it
In the Twilio number's settings, using your production URL (shown on the service page):
- A message comes in → `https://YOUR-SERVICE.onrender.com/webhooks/twilio/sms` (HTTP POST)
- A call comes in → `https://YOUR-SERVICE.onrender.com/webhooks/twilio/voice` (HTTP POST)

If you later add a custom domain, set `PUBLIC_BASE_URL` to it in Render's Environment tab and update both webhooks. Twilio's security check compares against that exact address.

## 4. Add the business and its logins
Open the service on Render → **Shell** and run the commands below.

Add the business. Paste its JSON here instead of committing it to GitHub, because it holds secrets (form key, Slack webhook):
```bash
python scripts/upsert_client.py - <<'JSON'
{ "slug": "acme-hvac", "name": "Acme Heating & Air", "twilio_number": "+1919...", ... }
JSON
```
(Copy `clients/triangle_comfort.json` as the template and fill in every value.)

Give the owner a login (they'll only ever see their own leads):
```bash
python scripts/create_user.py owner@acmehvac.com --client acme-hvac --name "Mike" --generate
```
Give yourself an operator login (sees every business):
```bash
python scripts/create_user.py you@example.com --operator
```
Share passwords privately (in person or by phone), never in the same channel as the link. To reset a password, run the same command again; it logs that person out everywhere. To remove someone, add `--deactivate`.

## 5. Run the preflight check
Still in the Shell:
```bash
python scripts/preflight.py --client acme-hvac --write-test --slack-test
```
It checks the database, the AI model, the Twilio number and both webhooks, the calendar (creates and deletes a test event), Slack, and the security settings. Fix every `FAIL` line until it ends with "Ready for a live test call."

## 6. Live test from your own phone
1. Submit the business's website form with your number and check the consent box. You should get a text within 10 seconds.
2. Text back a ZIP code and book a time. Check that the event appears on their Google Calendar.
3. Call the business number, let it ring out, and confirm you get a text.
4. Text "can I talk to a person". The owner's Slack should get an alert, and its link should open the conversation after login.
5. Text `STOP`. Nothing else should ever arrive.

Then show the owner how to log in at `https://YOUR-SERVICE.onrender.com/admin`, reply, take over and resume.

## What's protecting real customer data
| Area | Protection |
|---|---|
| Staff inbox | Personal logins with scrypt-hashed passwords; signed, HttpOnly, Secure cookies; CSRF tokens on every form; login lockout after repeated failures; each owner sees only their own business; logins and staff actions are logged |
| Webhooks | Twilio signature check on every request; website forms need the business's secret key |
| Database | Paid Render Postgres reachable only from your Render services |
| AI | Paid Anthropic API (no free-tier data use) |
| Texting | STOP honored before the AI ever sees the message; no texts without form consent |

## Changing the database later
Tables are created automatically, but **new columns on existing tables are not**. Until a migration tool (Alembic) is added, ask before changing `app/models.py` once a real business is live.
