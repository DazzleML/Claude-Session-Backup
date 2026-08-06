---
description: "Show what's open now, save the group as a named set, or reclaim one -- csb session sets"
---

Work with csb session sets (the Live Session Registry) for the user. `$ARGUMENTS` may name a subaction or a set name.

1. If `$ARGUMENTS` is empty or `current`: run `csb set show current` and present the roster conversationally -- which sessions are open this boot, which are process-verified running vs "no exit observed", in open order. If the registry is empty, explain that live tracking needs the csb plugin's hooks and how to update.
2. If `$ARGUMENTS` starts with `save` (e.g. `save MY-GROUP`): run `csb set new <name> --from current` to freeze the currently-open group as a named set, then show it with `csb set show <name>`.
3. If `$ARGUMENTS` starts with `reclaim` (e.g. `reclaim MY-GROUP`): run `csb resume set <name>` to list which members are NOT currently open, and tell the user to run `csb resume set <name> <N>` in whichever terminal they want each session to open in. NEVER run the resume yourself -- csb never spawns windows, and neither should you: the user chooses the terminal.
4. If `$ARGUMENTS` is `last`: run `csb set show last` and summarize what was active before the machine's last shutdown, noting any `[open at shutdown]` badges.
5. Otherwise treat `$ARGUMENTS` as a set name: `csb set show <name>`.

Keep output compact; the roster's own numbers are the addressing scheme (`csb resume set <name> <N>`).
