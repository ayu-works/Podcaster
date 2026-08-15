# Podcaster: Step by Step Build

~2 hours. Read ARCHITECTURE.md first, it explains the why. This is the do.

**Every block ends with a check.** Do not skip them. The failure mode of this product is silent (mediocre picks), not loud (a crash), so the checks are the build.

---

## Before you start

Two free keys, both take about two minutes.

1. **Podcast Index** at podcastindex.org/api/docs. Gives you a key and a secret.
2. **Resend** at resend.com. Grab the API key. **That is all you need to do.**

### On Resend

Resend is a service that sends email from code. You need one because a Python script cannot email you directly, providers block mail from random scripts and home IPs since that is what spam looks like.

**Use the sandbox sender.** Send from `onboarding@resend.dev` with no setup at all. The only restriction is that it can only deliver to your own account email address, which is exactly your situation. No domain, no DNS records, no waiting.

```python
import resend
resend.api_key = os.getenv("RESEND_API_KEY")

resend.Emails.send({
    "from": "Podcaster <onboarding@resend.dev>",
    "to": "you@gmail.com",
    "subject": "2 episodes for you",
    "html": rendered_html,
})
```

If you try to send to any other address you get a 403. That is the sandbox restriction, not a bug. When real users sign up, buy a domain, add the SPF and DKIM records Resend gives you, and change the `from` line. Nothing else in the code changes.

Free tier is 3,000 emails a month. You will send about 16.

### While you are at it

Write your interest strings in a scratch file. 4 to 6 of them, in your own words. **Specific beats broad.** "AI agents in production, not demos" will rank far better than "AI", because that literal text goes into both the search expansion and the ranking prompt.

---

## Block 1: Setup (15 min)

```bash
mkdir podcaster && cd podcaster
python3 -m venv .venv && source .venv/bin/activate
pip install httpx feedparser groq jinja2 resend flask python-dotenv
```

- `.env` with `PODCASTINDEX_KEY`, `PODCASTINDEX_SECRET`, `GROQ_API_KEY`, `RESEND_API_KEY`
- `config.py` with the constants block from ARCHITECTURE section 9
- `db.py` with the schema from ARCHITECTURE section 5, plus an `init_db()`

**Check:** `sqlite3 podcaster.db ".tables"` lists six tables.

---

## Block 2: Podcast Index client + the universe (20 min)

**`podcastindex.py`** — two functions:

- `search_shows(term)` wraps `/search/byterm`
- `episodes_by_feed(feed_id)` wraps `/episodes/byfeedid`

Auth is a SHA1 hash of key + secret + unix timestamp, sent in the headers. It trips everyone up. Read their auth doc once before writing it.

**`universe.py`** — given a user's interests, build the 200:

1. **One Groq call expands each interest into ~18 search terms.** Live Podcast Index testing showed that 8 precise terms leave too few fresh feeds after filtering; a three-interest profile needs about 18 each to reach a 200-show universe without using noisy deep search results. "AI agents in production" becomes `AI engineering`, `LLM infrastructure`, `MLOps`, `applied AI`, `AI developer tools`. The user's phrasing is precise, but it is not what shows call themselves, and the expansion bridges that gap.
2. Search each term via `search_shows`
3. Dedupe by `feed_id`, drop feeds with nothing published in 60 days
4. Keep the top 200, write `candidate_show` rows

**Check, and this is the highest-value ten minutes in the build:** run it on your own interests and **read the 200 show names.**

That list is the ceiling on everything this product will ever recommend to you. If it looks generic or off-topic, your interest strings were too broad. Fix them and rerun before writing another line. A bad universe fails invisibly, and no amount of prompt tuning later will rescue it.

---

## Block 3: Onboarding page (20 min)

**`app.py`**, two routes.

`GET /` renders `onboard.html`:

- ~20 interest chips, pick 3 or more
- Free text box: "What specifically, within those?"
- Email field

`POST /subscribe`: create user, save interests, run `universe.build()`, render `done.html`.

The build takes ~30 seconds, so either show a "setting up your first digest" state or fire it in a thread and return immediately. Do not leave a blank loading page.

You are a designer. This is the one block where that helps, so make it look like something you would show someone.

**Check:** submit the form as yourself. Confirm one `user` row, your interests, and 200 `candidate_show` rows.

---

## Block 4: Fetch and filter (20 min)

**`fetch.py`:** for each of the 200 shows, get episodes published since `user.last_run_at`. Run 15 concurrently (`ThreadPoolExecutor`) or this takes minutes instead of seconds. Upsert into `episode` on `guid`.

Use `last_run_at`, not a hardcoded 2 days. Cap the lookback at `MAX_LOOKBACK_DAYS`.

Then filter:

- drop anything already sent to this user
- drop descriptions under 100 chars
- drop trailers and anything under 3 minutes
- dedupe on `guid`

**Check:** print `raw -> after filter`. You want 60+ surviving.

**Under 30 means stop and widen the universe**, either more terms per interest or a bigger `UNIVERSE_TARGET`. Do not push forward. A thin pool produces bad picks that look exactly like a bad ranker, and you will lose an hour debugging the wrong stage.

---

## Block 5: The ranker (30 min)

**The biggest block on purpose. This is the product.**

**`rank.py`:** build the prompt from ARCHITECTURE section 6 Stage 3. One batched call, strict JSON out, retry once on a parse failure, then return empty rather than crash.

Log the full prompt and raw response to a file every run.

**Check, and this is the real gate on the entire build:** run it three times against live data and read the reasons out loud.

- Does each reason name something concrete from that episode's description, a guest, a claim, a case study?
- Or does it say "a great listen for someone interested in AI"?

**A generic reason is a bug, not a weak output.** If you see them, tighten rule 3 in the system prompt and rerun before moving on.

Everything downstream is worthless if this stage is mediocre, and it is the only part you cannot fix later by changing a number in config.

---

## Block 6: Email (20 min)

**`templates/digest.html`:** inline CSS, single column, 600px max, no external images. Gmail strips `<style>` blocks and clips at 102KB.

Per item: title, show, duration, the why-this line, listen link.

**`email.py`:** render and send via Resend. **Write the digest rows before sending, mark sent after,** so a failed send is not silently lost.

If nothing cleared the bar, record a `quiet` digest and send nothing.

**Check:** send to yourself and open it **on your phone**. That is where you will actually read it, so that is where it has to look right.

---

## Block 7: Schedule and end to end (15 min)

**`run.py`:** the full cycle, fetch → filter → rank → send → update `last_run_at`.

**launchd, not cron.** Your Air sleeps, and cron silently skips jobs scheduled during sleep, permanently. launchd fires them on wake.

Create `~/Library/LaunchAgents/com.ayush.podcaster.plist` for Sun/Mon/Wed/Fri at 07:00, then `launchctl load` it and trigger once manually with `launchctl start`.

**Final check:** wipe `digest` and `digest_item`, run clean, read the email as a user rather than as the person who built it.

**The only question that matters: would you actually play either of those episodes?**

If no, the ranker needs work and nothing else does. Go back to Block 5.

---

## If you run long

Cut in this order:

1. The quiet-day handling, just send nothing
2. The onboarding chips, free text box only
3. Concurrency in fetch, let it be slow

**Never cut:** the relevance bar, or the Block 5 check. Those are the product.

---

## First week after

Log one line per run: `candidates -> after filter -> cleared bar -> sent`.

- **Few surviving the filter** → universe too narrow, not a ranker problem
- **Never clearing the bar** → bar too high or pool too shallow, check the first number to tell which
- **Always at the cap** → bar too low, filler is leaking in. Worse direction.

**Tune `RELEVANCE_BAR` before touching anything else.** It is the single number that decides whether this feels curated or spammy.

Do not add features for two weeks. Get the picks right first.
