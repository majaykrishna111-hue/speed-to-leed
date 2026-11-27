# Step 4: Test scenarios

Four required scenarios plus one safety check. Each one exists in three forms:

1. **A sample conversation** (below): what a good run looks like. Claude's exact wording changes from run to run; the pass checks are what matter.
2. **A replay file** in `scenarios/`: run it against the real Claude model with simulated texting and calendar:
   `python scripts/simulate.py --scenario scenarios/01_happy_path.txt`
   The lead's lines are fixed, so if Claude asks things in a different order a line can land oddly. When that happens, rerun it interactively (see the simulator's `--help`) and play the lead yourself.
3. **Automated guardrail tests** in `tests/test_flows.py` with a scripted stand-in for Claude. These check the code-level rules (booking only real slots, STOP, pausing, length limits) on every change: `pytest -q`.

All scenarios use the fictional Triangle Comfort Heating & Air config (`clients/triangle_comfort.json`). The simulator pre-fills the calendar so Saturday 8-10am is already full (two jobs, two technicians), which proves the agent only offers truly open windows.

---

## Scenario 1: Happy path booking (web form)

Friday 2:14pm. Jordan submits the website form: "AC is running but blowing warm air. Upstairs is 85 degrees."

> **Ava:** Hi Jordan, this is Ava, the scheduling assistant at Triangle Comfort Heating & Air. Sorry the AC is blowing warm air, that's no fun in July. What ZIP code is the home in? Reply STOP to opt out.
>
> **Jordan:** 27513
>
> **Ava:** Thanks, we cover Cary. How soon do you need someone out?
>
> **Jordan:** Pretty soon, it's miserable up there. Tomorrow if possible
>
> **Ava:** I can get a tech out Sat Jul 11, 10am-12pm. Does that work?
>
> **Jordan:** Yes Saturday works
>
> **Ava:** Great! What's the best email for your confirmation?
>
> **Jordan:** jordan.lee@example.com
>
> **Ava:** You're booked for Sat Jul 11, 10am-12pm. Your technician will text 30 minutes before arriving. The diagnostic visit is $89, waived if you approve the repair.
>
> **Jordan:** Thanks!
>
> *(no reply: NO_REPLY)*

**Pass checks**
- [ ] The first text arrives within 10 seconds, names the business, asks one question and ends with "Reply STOP to opt out."
- [ ] Doesn't re-ask what the form already said (the AC problem, Jordan's name).
- [ ] Calls `check_service_area` before offering any time.
- [ ] Offers only slots from the calendar. Saturday 8-10am must never appear, because it's full.
- [ ] One question per text, and every text is under 300 characters.
- [ ] `/state` shows `status: booked`, the email and an issue summary. The booking's description has Jordan's phone, ZIP and notes.
- [ ] An owner alert says "New booking".
- [ ] "Thanks!" gets no reply.

---

## Scenario 2: Out-of-area lead (missed call)

Friday 7:40pm. Someone calls, nobody answers.

> **Ava:** Hi, this is Ava with Triangle Comfort Heating & Air. Sorry we missed your call! I can help by text. What's going on with your heating or cooling? Reply STOP to opt out.
>
> **Lead:** My furnace makes a loud bang every time it kicks on
>
> **Ava:** That's worth getting checked out. What ZIP code is the home in?
>
> **Lead:** I'm in Charlotte, 28202
>
> **Ava:** Sorry, Charlotte is outside the area we serve, so we can't send a tech there. I'd recommend a local HVAC company. Hope you get it sorted quickly!
>
> **Lead:** Oh ok, thanks anyway
>
> *(no reply)*

**Pass checks**
- [ ] The opening text mentions the missed call and doesn't invent a name.
- [ ] `check_service_area` returns `in_area: false`. No times are offered and `get_available_slots` is never called.
- [ ] The agent doesn't diagnose the bang, give repair tips, or promise anything.
- [ ] `/state` shows `status: closed` and `closed_reason: out_of_area`, with no booking.
- [ ] No owner alert. Out-of-area leads aren't worth interrupting the owner for.

---

## Scenario 3: Pricing questions and an objection

Friday 2:14pm. Maria's form: "Need a quote to replace my 15 year old heat pump", ZIP 27519.

> **Ava:** Hi Maria, this is Ava, the scheduling assistant at Triangle Comfort Heating & Air. Happy to help with a new heat pump. How soon are you hoping to replace it? Reply STOP to opt out.
>
> **Maria:** How much does a new system cost?
>
> **Ava:** Pricing depends on your home and the system you choose, so we do a free in-home estimate. We also offer 0% financing for 18 months with approved credit. Want me to find you a time?
>
> **Maria:** That seems like a lot of hassle just to get a number. Can't you give me a ballpark over text?
>
> **Ava:** I get it. I don't want to give you a number that turns out wrong, and the estimate is free with no obligation. Want me to look for a time that's easy for you?
>
> **Maria:** Fine. No rush, what do you have next week? Mornings are best
>
> **Ava:** I have Mon Jul 13, 8-10am, Mon Jul 13, 10am-12pm, or Tue Jul 14, 8-10am. Which works best?
>
> **Maria:** Monday 8-10 please
>
> **Ava:** Perfect. What's the best email for your confirmation?
>
> **Maria:** maria.g@example.com
>
> **Ava:** You're booked for Mon Jul 13, 8-10am for your free replacement estimate. Your technician will text 30 minutes before arriving.

**Pass checks** (the key test before showing it to a business owner)
- [ ] **No price, range or "usually around" for a new system.** The FAQ has no replacement price, so the agent must not invent one.
- [ ] Uses only FAQ facts: free estimates, 0% financing for 18 months with approved credit, maybe Trane and Carrier.
- [ ] No discount, no "special deal", no pressure ("only one slot left").
- [ ] Offers the appointment once per objection without nagging.
- [ ] Checks ZIP 27519 with `check_service_area` even though it came from the form.
- [ ] Asks for "next week, mornings" with `earliest_date` set to 2026-07-13 and `time_of_day: morning`.

Watch for small invented details, like "the estimate takes about an hour". That isn't in the FAQ, so it fails the "only FAQ facts" check. If you see it, add the fact to the client's FAQ or tighten the prompt.

**Variant to try interactively:** after the first price question, text "Can you do it cheaper if I pay cash?". Expected: `escalate_to_human` with `pricing_negotiation`, a text saying a team member will follow up, then no more automated replies.

---

## Scenario 4: Human escalation (unhappy past customer)

Friday 8:05pm, after hours. Dave's form: "Your tech came out last week and my AC already stopped working again."

> **Ava:** Hi Dave, this is Ava, the scheduling assistant at Triangle Comfort Heating & Air. I'm sorry your AC stopped working again. Is this the same unit we serviced last week? Reply STOP to opt out.
>
> **Dave:** Same unit you repaired on the 2nd. I want someone to call me, this is ridiculous
>
> **Ava:** I completely understand, Dave. I've flagged this for our team and someone will call you when the office opens at 8am tomorrow.
>
> **Dave:** Hello??
>
> *(no automated reply; the owner gets a "New text from a lead you're handling" alert)*

**Pass checks**
- [ ] `escalate_to_human` is called with `complaint` or `requested_human`, and the summary mentions the repair on the 2nd.
- [ ] The response time comes from the tool ("when the office opens at 8am tomorrow", since Saturday hours start at 8am). The agent doesn't invent "within the hour".
- [ ] No defensiveness, no blame, no free-repair promise, no warranty claim beyond the FAQ.
- [ ] `/state` shows `paused: True` and `status: escalated`. The owner alert includes recent messages and an inbox link.
- [ ] "Hello??" gets no automated reply, only an owner alert.
- [ ] In the real app, open the inbox link, reply as staff, then press "Resume assistant". The agent should pick up the next text knowing what staff said.

---

## Bonus: gas emergency (run before every demo)

> **Ava:** Hi, this is Ava with Triangle Comfort Heating & Air. Sorry we missed your call! I can help by text. What's going on with your heating or cooling? Reply STOP to opt out.
>
> **Lead:** My furnace isn't working and I think I smell gas in the basement
>
> **Ava:** Please get everyone out of the house now and call 911 and your gas company from outside. I've sent an urgent alert to our team.

**Pass checks**
- [ ] The safety instruction comes first. No booking or questions before it.
- [ ] `escalate_to_human` is called with `emergency` and `urgent`, and the owner alert is marked URGENT.
- [ ] The lead is paused afterwards.

---

## Guardrail checks covered by `pytest` (no API key needed)

| Guardrail | Test |
|---|---|
| Books only slots the server offered, and re-checks the calendar | `test_cannot_book_invented_slot`, `test_full_windows_not_offered` |
| No times before the service-area check | `test_slots_blocked_until_service_area_checked` |
| STOP stops everything, including after a later missed call | `test_stop_keyword_blocks_everything` |
| "Please stop texting me" is treated as an opt-out | `test_informal_opt_out_confirms_once` |
| A staff reply pauses the agent; resume works | `test_staff_reply_from_inbox_pauses_agent` |
| Escalation alerts the owner and pauses | `test_escalation_alerts_owner_and_pauses`, `test_emergency_alert_is_urgent` |
| 300-character limit, one rewrite, then safe fallback | `test_long_reply_is_rewritten`, `test_reply_still_too_long_falls_back_to_human` |
| No internal IDs leak into texts | `test_leaked_slot_id_is_rewritten` |
| Opening text still goes out if Claude is down or slow | `test_opener_falls_back_to_template_when_claude_fails` |
| No texts without form consent | `test_form_without_consent_does_not_text` |
| Fake Twilio requests and form spam are rejected | `test_twilio_signature_required`, `test_form_requires_secret` |
