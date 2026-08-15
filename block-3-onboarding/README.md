# Block 3: Onboarding page

The form that turns a person into a `user` row, three `interest` rows, and 200
`candidate_show` rows.

## Files

| File | What it does |
|---|---|
| `app.py` | Flask. The form, the subscribe handler, and the background universe build. |
| `templates/onboard.html` | Chips, the text box, email. |
| `templates/done.html` | Live build state; polls until the universe is written. |
| `static/style.css` | The whole design. |
| `_shared.py` | Import shim for Block 1's `config`/`db` and Block 2's `universe`. |

## Run it

```bash
cd block-3-onboarding
../.venv/bin/python app.py          # http://127.0.0.1:5001
```

Port 5001, not 5000 — macOS AirPlay Receiver holds 5000.

## Routes

| Route | Why |
|---|---|
| `GET /` | The form. |
| `POST /subscribe` | Creates the user + interests, starts the build, redirects. |
| `GET /done/<job>` | The waiting/confirmation page. |
| `GET /status/<job>` | JSON, polled by that page. |

The plan calls for two routes. The last two exist only because the build runs
off-request: ARCHITECTURE section 8 says fire it in a thread rather than leave
a blank loading page, and a thread needs some way to report itself.

## Chips are not interests

This is the one decision in the block worth arguing about.

ARCHITECTURE section 8: *"The chips exist only to beat the blank page. The free
text is where the actual signal lives... Every layer of abstraction between the
user's words and the prompt costs relevance."*

So a chip does not become an `interest` row. Tapping **Design** appends an
editable line — `Design — ` — to the text box and puts the cursor there. The
user finishes the sentence, and *that sentence* is the interest. Untapping the
chip removes the line only if it was never filled in.

A chip label reaches the database in exactly one case: the user picked three or
more areas and wrote nothing at all. That is a floor to keep the universe from
being empty, not the intended path.

`MAX_INTERESTS = 6`. Every interest costs one expansion slot and 18 searches,
and past six the universe stops being about anything in particular.

## The build runs in a thread

`POST /subscribe` writes the user and interests **synchronously** — that takes
milliseconds — then hands the universe build to a daemon thread and redirects
immediately. Two reasons for that split:

- The page is never blank. `done.html` shows what it is doing and how long it
  has taken.
- If the build fails, the interests are already on record, so a retry does not
  ask the user to type them again.

Jobs live in a module-level dict. A server restart mid-build loses the handle,
and `/done/<job>` says so plainly rather than 500ing. The user row survives;
resubmitting the form rebuilds against the same email (`ensure_user` upserts).

**A failed build is shown, not swallowed.** A user with zero candidate shows is
the one state the product cannot recover from by itself, and it is invisible
from the outside — it looks exactly like a quiet week.

## Check

Submit the form as yourself, then:

```bash
../.venv/bin/python -c "
import sys; sys.path[:0]=['../block-1-setup']
import db
with db.session() as c:
    print(c.execute('SELECT COUNT(*) FROM user').fetchone()[0], 'users')
    print([r[0] for r in c.execute('SELECT text FROM interest')])
    print(c.execute('SELECT COUNT(*) FROM candidate_show').fetchone()[0], 'shows')
"
```

One user, your interests verbatim, 200 shows.

Then read the confirmation page's show list. If the head of it does not look
like *your* interests, the problem is upstream in Block 2's search terms, not
here.

## Next

Block 4 (`fetch.py`) reads `candidate_show` and `user.last_run_at`. A new user's
`last_run_at` is NULL on purpose — the first digest falls back to
`MAX_LOOKBACK_DAYS`, so it covers the last five days rather than all of time.
