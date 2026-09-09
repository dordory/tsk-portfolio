# TSK — Territory Management as a LINE MINI App

*[한국어 README](README.ko.md)*

A Django web application for managing a congregation's field-ministry **territories**, delivered as a **LINE MINI App (LIFF)**. Members sign in with their LINE account; territory-card data lives in **Google Sheets**, which the app reads and writes directly — the spreadsheet stays the single source of truth while the web app provides a mobile-first UI on top of it.

> This repository is a snapshot of a private working repository, published as a portfolio piece. Organization-specific names and credentials are externalized to environment variables.

## Why it's interesting (engineering highlights)

**LINE MINI App authentication, done server-side.**
The LIFF front end only bootstraps: it obtains an ID token and POSTs it to the server, which verifies the token against LINE's endpoint (stdlib `urllib` only — no SDK dependency) and establishes a normal Django session. Onboarding binds a LINE account to a pre-registered member via a **single-use invite code** issued per member by an admin; an already-linked member cannot be re-linked, preventing account hijacking. Identity is a **messenger-neutral layer** (`apps/messenger`: one `MessengerAccount` per member per provider, provider-agnostic link codes); `apps/line` only verifies the token and hands off to a shared login/onboarding contract, so a second messenger is a new adapter view, not a new identity model. Invite codes can also be issued **from the chat itself**: an unlinked user's first message becomes a request pushed to superusers, who approve it with a keyword — no admin screen needed for onboarding.

**A hybrid data layer.**
Member identity lives in the database (custom `AUTH_USER_MODEL` with soft delete), but the territory-card domain data has **no models at all** — it is read from and written to Google Sheets via a service account (`apps/territory_cards`). The congregation keeps its familiar spreadsheet workflow; the app adds authentication, mobile UX, and guarded writes.

**Sheet logic isolated as pure functions.**
Everything that can be computed without calling the Sheets API — coordinate constants, cell parsing, visit-record assembly, status→color mapping, dynamic detection of where data rows end — lives in `mapping.py` with no Google imports, so the tricky parts are unit-testable without network access. Google client libraries are imported lazily inside functions, so the app boots even where they are not installed.

**Safe writes against a live, human-edited spreadsheet.**
People keep editing the same sheets the app writes to, so every write is guarded. Tabs are addressed by immutable sheet `gid` (renames and reorders never break URLs), and row-level writes carry a **row fingerprint** (address columns) that the server re-reads and compares immediately before writing — a mismatch returns 409 and the UI routes the user back to a freshly-read list, so a visit record can never land on the wrong household even if rows were inserted meanwhile. Writes use `RAW` input (no formula injection via free-text memos). Caching is two-tiered — slow-changing metadata (master index, tab list, status options) for 5 minutes, sheet bodies for 60 seconds with write-through invalidation — cutting API round-trips per screen from 3–4 to 0–1, while the verification read right before a write always bypasses the cache (consistency path).

**A territory map with a self-healing coordinate cache.**
Every address of a territory (or of a whole card, for staff) is pinned on one Google Map, colored by visit recency. Geocoding happens client-side, and coordinates are cached in **three tiers** — a shared DB table keyed by the geocoding query, the device's `localStorage`, and the geocoder itself — with the lower tiers converging into the DB via one batch POST, so the map costs **zero Sheets round-trips**. The DB cache is a pure derivative: an edited address is simply a cache miss that re-geocodes and re-saves, so nothing ever needs manual repair. Three earlier designs (extra sheet columns, auto-added hidden columns, a hidden cache tab) were tried and discarded for concrete failure modes recorded in the code comments.

**A group-chat bot with a messenger-neutral core.**
An official-account bot answers keywords in the congregation's group chat (no friend-add required): a rich Flex menu, *the sender's* next Bible-reading unit, today's daily text, and this week's reading range scraped live from wol.jw.org (cached on success, explicit fallback on failure — never silent). The decision logic lives in `apps/bot`, a plain package that consumes normalized `(text, sender)` input and returns **abstract reply objects (data only)**; `apps/line` is just an adapter — webhook signature verification and a Flex renderer, both on stdlib `urllib`. Porting to another messenger means writing one adapter. Keywords and menu tiles are DB rows managed in the admin, so the bot's vocabulary changes without deployments.

**Scripture-stamp checks for kids (an allowance system in a group chat).**
Children post “\<scripture\> 읽음” (“I read \<scripture\>”) in the group chat; the bot parses the sentence, fetches the day's actual scripture from wol.jw.org, and compares book+chapter before stamping — a mismatch is gently declined *without revealing the right answer* (it's an honesty exercise). Accepted checks (one per day, back-dating limited to yesterday) feed a calendar-style Flex stamp card, streak/milestone feedback, and a monthly settlement with per-child rates and a perfect-month bonus. Admins fix history on a tap-to-toggle calendar screen. Visibility follows a **family axis** orthogonal to groups (`Family` / `FamilyRole` with an `is_guardian` flag): a guardian sees and manages only their own children's stamps, so the feature is safe to open to the whole congregation. A `help` keyword answers each sender with exactly the keywords they are entitled to — descriptions live in code, trigger words come from the admin-managed keyword table, and a completeness test forbids adding a bot action without a help entry.

**Web/LIFF dual rendering.**
A middleware flags LIFF sessions (`request.is_liff`); a template-resolution helper prefers `liff/<name>` templates when present and falls back to the standard ones, letting one view serve both the in-app WebView and regular browsers.

**Korean-first UI with an opt-in English locale.**
The congregation is Korean-speaking, so Korean stays the source language (msgids) and English lives in `locale/en` — flipping the base language would push every Korean string into a translation file and destabilize hundreds of Korean test assertions for no user benefit. Language selection is deliberately **explicit only** (`?lang=en`, persisted in a cookie) via a tiny middleware instead of Django's `LocaleMiddleware`: a member whose phone is set to Japanese or English must still see the Korean UI. Sheet data (assignee labels, visit statuses) is content, not UI, and is never translated. A catalog test fails the build on any untranslated string or mismatched placeholder.

**Real-world deployment quirks solved.**
Runs on PythonAnywhere behind its mandatory outbound proxy — including the subtle failure where `httplib2` *silently ignores* proxy settings when PySocks is missing (the fix is codified in `requirements.txt` and an explicit proxy injection in `sheets.py`).

## Architecture

```
LINE app (LIFF WebView)                 Browser (web)
        │                                    │
        └────────────┬───────────────────────┘
                     ▼
        Django 5.2 (server-rendered, Tailwind CDN)
        ├── apps/line             LIFF entry & ID-token verification, onboarding;
        │                         bot adapter (webhook + Flex renderer)
        ├── apps/bot              messenger-neutral bot core (no models, abstract replies)
        ├── apps/messenger        messenger-neutral identity (accounts, link codes, chat requests)
        ├── apps/member           custom user model (soft delete), groups, families
        ├── apps/territory        DB-backed territory domain (views split by audience)
        ├── apps/territory_cards  Google-Sheets-backed territory cards (only a geocode cache model)
        ├── apps/board            congregation board (Google Drive folder listing)
        ├── apps/deck, manager    territory bundles, reporting
        ├── apps/bible_reading    Bible reading planner (plan as pure data, progress in DB)
        ├── apps/character_quiz   character quiz card viewer (static images as data source)
        └── apps/home             link hub
                     │
        ┌────────────┴────────────┐
        ▼                         ▼
   SQLite (identity,         Google Sheets / Drive
   assignments, history)     (territory cards — source of truth)
```

See [docs/DESIGN.md](docs/DESIGN.md) (Korean) for the design document of the sheets-backed app, written before implementation.

## Stack

Python 3.11 · Django 5.2 · google-api-python-client (service account) · LIFF v2 · Tailwind (CDN) · SQLite

## Getting started

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in values (all optional for a quick look)
python manage.py migrate
python manage.py runserver
```

With LINE/Google variables left empty and `DEBUG=True`, the app runs in a **development bypass-login mode** (pick a member directly) so the UI can be explored without any external accounts.

The UI is Korean by default; append `?lang=en` to any URL for the English locale (territory-card, Bible-reading and sign-in screens are translated; the choice is remembered in a cookie).

### Key environment variables

| Variable | Purpose |
|----------|---------|
| `SECRET_KEY` | Django secret; if unset, a random key is generated per process (dev only) |
| `CONGREGATION_NAME` | Display name of the organization (kept out of source) |
| `LINE_LIFF_ID`, `LINE_CHANNEL_ID` | LINE MINI App credentials; empty ⇒ LINE login disabled |
| `LINE_MESSAGING_CHANNEL_SECRET`, `LINE_MESSAGING_ACCESS_TOKEN` | Messaging API channel (official-account bot); empty ⇒ webhook disabled |
| `GOOGLE_SERVICE_ACCOUNT_FILE` / `_JSON` | Service-account key for Sheets/Drive access |
| `TERRITORY_CARDS_MASTER_SHEET_ID` | Master index spreadsheet for territory cards |
| `GOOGLE_MAPS_API_KEY` | Browser-side Maps JavaScript key for the territory map; empty ⇒ map screen shows a notice |

## Notes

- UI text is Korean (the app serves a Korean-speaking congregation in Tokyo); timezone is `Asia/Tokyo`.
- Tests (424) cover the pure sheet-mapping logic, LINE token verification (mocked), write guards, caching/invalidation, the coordinate cache, bot keyword routing and help completeness, invite-code flows, family-scoped permissions, scripture parsing/settlement math, language switching and translation-catalog completeness, and template rendering: `python manage.py test`.
- The quiz app ships with generated **sample cards**; the real card images are copyrighted material and are excluded from this public snapshot.

## License

[MIT License](LICENSE)
