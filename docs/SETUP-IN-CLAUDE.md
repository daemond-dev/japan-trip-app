# Publishing a live copy from a claude.ai chat

The live app (shared data, Ask, to-dos, booking documents, live sync to the Android app) is a published claude.ai artifact with a database. Publishing and database writes are claude.ai chat tools, so this step runs in a chat, not in Claude Code. It needs a Claude plan whose artifacts support the `db`, `user`, `downloads` and `sample` capabilities; Mapbox and Rome2Rio are optional connectors for the place lookup and travel times.

Personalize the repo first (`/setup` in Claude Code, or edit `data/` by hand), push it, then start a new chat in a claude.ai Project, and paste the text below the line with your repo URL filled in. If the repo is private, attach a zip of it instead and change step 1 to "unzip the attachment".

---

I'm publishing a trip app from my repo REPO_URL. Set it up in my account so I own it. Do these in order and stop to tell me if a step can't be done in this chat.

1. `git clone REPO_URL` in the container (or unzip the attachment) and read `docs/NOTES.md` fully before doing anything else.
2. Run the Artifact tool's "capabilities" action and confirm that `db`, `user`, `downloads`, `sample` and `mcp` are available to me. If any of the first four isn't, stop and tell me what that means for the app before publishing.
3. Publish `app/index.html` as a new artifact titled with `meta.title` from `data/meta.json` (favicon 🗾) with exactly these capabilities: `{"db":{}, "user":{}, "downloads":true, "sample":{"images":true}, "mcp":{"servers":[{"server":"Mapbox","tools":["search_and_geocode_tool"]},{"server":"Rome2Rio","tools":["get-routes"]}]}}`. If the publish is refused because the Mapbox or Rome2Rio connectors aren't connected in my account, publish without the `mcp` entry and tell me how to add it later. Give me the artifact link.
4. Seed the database with `write_db`: each `data/*.json` file is one document in collection `trip` with the file name (without .json) as the document id; use a batch. Then set `app/page` to `{version: 1, html: <the contents of app/index.html>, savedAt: <today>}`. If `data/todo/` has files, write each as a doc in collection `todo`. Leave `docs`, `visited`, `pastMarks`, `phrasePins`, `phraseThemes` and `phraseOrder` empty.
5. Open the artifact and check that the Today tab, Places, Yen and Speak render and the header clock shows my home time.
6. Tell me the artifact link to put in `android/local.properties` as `LIVE_URL`, so I can build the Android app with Claude Code (`cd android && ./build.sh 1.0`). If you can build it here instead (the toolchain is described in `android/README.md`), do that and attach the APK.
7. Add the artifact link and the date to `docs/NOTES.md` under "Map of the system" and give me the updated file to commit.

From then on, chats about the live app read `docs/NOTES.md` first (keep it in the Project's files), change data with `read_db` then `write_db`, change the page with Artifact `edit`, and after every page change write `app/page` with a higher version and tell me to commit the same page to the repo.
