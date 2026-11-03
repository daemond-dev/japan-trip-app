Add a booking confirmation the owner gives you (PDF, screenshot, email text) to the app data.

1. Read `docs/NOTES.md` (stays and items). Extract: what, date and times, name the booking is under, confirmation number, address and phone, cancellation terms, manage/cancel link, and whether it has a QR code.
2. For a stay: add or update the entry in `data/stays.json` (`dates`, `checkIn`, `checkOut`, `conf`, `under`, `lines`, `links`) and make sure the place exists in `data/places.json` and the days point at it with `stay`. For anything else: add or update the item in `data/days.json` with status Booked, `end` set, `links`, and remove that meal from the day's `mealOpen`.
3. QR codes: regenerate from the decoded text with segno as an inline SVG, black on white, with a Japanese caption in `ja`, and check it decodes back (zxing-cpp). Booking page images go to the live app's `docs` collection from a claude.ai chat, not into this repo.
4. If this repo is public or shared, keep confirmation numbers, QR codes and private phone numbers out of `data/` entirely and tell the owner they belong in the live copy only.
5. Validate the JSON, run `python3 scripts/test_page.py live`, and say what you added and what you left out.
