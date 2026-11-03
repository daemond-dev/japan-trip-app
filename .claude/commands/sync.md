Update `data/` from the owner's plan (a document, a calendar export, pasted notes, or a booking they hand you) so the app matches it.

1. Read `docs/NOTES.md` (data model and item rules), then the plan the owner gives you or points you at.
2. Edit `data/days.json` (one day per date; items with `t`, `m`, `end`, `type`, `status`, `place`; `mealOpen` for unbooked meal times; a Sleep item last with `stay`), `data/stays.json`, and `data/places.json` (new places need `placeId`, `lat`, `lng`, `district`, `station`, `cat`; add `ja`, `addr`, `ro`, `addrRo` for a driver card). Keep every booked item's details; don't invent times.
3. Refresh `meta.rate` (yen per USD, with `asOf` and `source`) and `meta.weather` (one entry per day and city) if the owner wants them in the static copy, and set `meta.updatedAt`.
4. Flag anything that needs booking and when booking opens, overpacked days, and conflicts with fixed points (flights, booked hotels, fixed events). Say it plainly instead of working around it.
5. Validate the JSON, run `python3 scripts/test_page.py live` and `static` with `Date.now` faked to a day of the trip, build the site, and list what changed and what you didn't touch.
