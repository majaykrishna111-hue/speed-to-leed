# Put the demo online for free (about 15 minutes)

You'll end up with a public link like `https://speed-to-lead-demo.onrender.com` that opens the live demo. It runs on Render's free plan with your free Gemini key, so it costs $0.

## Before you start

- **A Gemini key that can never charge you.** Create it at https://aistudio.google.com/apikey in a Google Cloud project with **no billing account attached**. Without billing it can only use the free tier: if the quota runs out, the demo pauses instead of charging you.
- **A GitHub account** (free) at https://github.com.

## 1. Put the code on GitHub

1. On GitHub, click **New repository**. Name it `speed-to-lead`. **Public** lets OpenAI reviewers read your code too; **Private** works with Render as well. Don't add a README; click **Create repository**.
2. On the empty repo page, click **uploading an existing file**.
3. Unzip `speed-to-lead.zip` on your computer, open the `speed-to-lead` folder, select everything inside it (`app`, `prompts`, `clients`, `scenarios`, `scripts`, `tests`, `render.yaml`, `requirements.txt`, the `.md` files...) and drag it onto the GitHub page. Folders upload with their structure in Chrome, Edge and Firefox.
   - **Never upload your `.env` file.** It holds your keys.
   - Hidden files like `.gitignore` may not drag on a Mac. That's fine; nothing needs them.
4. Click **Commit changes**. Check that `render.yaml` and the `app` folder appear at the top level of the repo, not inside an extra `speed-to-lead` folder.

## 2. Deploy on Render

1. Go to https://render.com, click **Get Started**, and sign up **with GitHub**. A credit card isn't needed for the free plan.
2. In the dashboard, click **New** → **Blueprint**. Connect your `speed-to-lead` repo; if it isn't listed, click **Configure account** and give Render access to it.
3. Render reads `render.yaml` and shows one free web service, `speed-to-lead-demo`. It asks for **GEMINI_API_KEY**: paste your key there. Everything else is preset.
4. Click **Deploy Blueprint**. The first build takes about 3 to 5 minutes. The logs end with `Uvicorn running on http://0.0.0.0:10000` and the service shows **Live**.
5. Click the service's URL (top of its page). It opens the demo.

## 3. Test it, then share it

- Open the link on your phone and your laptop. Each tab gets its own private conversation, so reviewers never see each other's texts or yours.
- Run through the happy path and the "I smell gas" shortcut before you send the link anywhere.
- **Expect a slow first load.** Free services sleep after 15 minutes without visits, and the first visit after that takes up to about a minute while the server wakes. Open the link yourself a minute before a live demo. If reviewers might open it cold, a line like "first load can take ~1 minute (free hosting)" next to the link helps.

## What protects you on a public link

| Risk | What's in place |
|---|---|
| Someone burns your Gemini quota | 30 customer actions per conversation, 300 per day across everyone (`DEMO_MAX_ACTIONS_PER_SESSION`, `DEMO_DAILY_ACTION_LIMIT` in Render's Environment tab). A key with no billing can't cost money. |
| Visitors see each other's chats | Every browser tab has its own private session; idle sessions are deleted after 30 minutes. |
| Real texts or bookings | None. The demo's phone, calendar and alerts are simulated and nothing is stored after a restart. |
| The staff inbox | Login required; nobody has an account on the demo service, so it stays locked. |
| Want it private? | Add `DEMO_TOKEN` in Render's Environment tab; the link then becomes `https://YOUR-APP.onrender.com/demo?token=YOUR_TOKEN`. |

Free-tier Gemini prompts may be used by Google to improve its products, which is fine for this demo because it only has fictional data. Use a paid key before any real customer texts go through the agent.

## Updating it later

Edit files on GitHub (or upload new versions). Render redeploys automatically on every commit.
