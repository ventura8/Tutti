# Feedback log

First users log **every** problem they hit here, the small ones too. This file is the backlog: an entry is closed by
the change that fixes it, and a lesson worth keeping moves to [LESSONS.md](LESSONS.md).

How to add an entry: append a row. Say what you ran, what happened, and what you expected. No hostnames, names, paths
of your project or secrets: describe the setup in general words.

| # | Date | Command | What happened | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| 1 | 2026-11-14 | `tutti quiet --summary <worker>` | A failing command that printed nothing got an invented failure summary from the model. | No summary for an empty log. | Fixed in 0.1.0: an empty log prints "(the command printed nothing)". |
| 2 | 2026-11-14 | `tutti usage` | With `share: 0.5` a whole-account reading was compared with half the quota, so 60 % read as "spent". | A reading is the whole account's use; the share is information. | Fixed in 0.1.0: the pace is judged against the whole quota and the share is shown beside the verdict. |
| 3 | 2026-11-14 | `tutti test` on a `wsl` machine | Only the probe was exercised (the machine's yield flag was up); the copy and the run are untested there. | A verified full run. | Open (roadmap 0.2). |
| 4 | 2026-11-14 | `tutti test <track> echo -n x` | An option-like word in an unquoted command is read as an option of `tutti`. | Works either way. | Open: pass the command as one quoted string. |
| 5 | 2026-11-14 | `tutti ship --full` | The routed checks copy the tree you stand in; the scripts it replaces copied the main checkout with its full `.git`. Gates that read git history see a shallow repository. | Same result as before. | Open: watch the first full run. |

- 2026-11-14 (map project): the installer's `git clone` failed on a laptop whose PATH had a broken git first (another tool's bundled git with a mismatched libcurl). Works with /usr/bin/git. `tutti doctor` should detect a git that cannot do HTTPS and suggest fixing PATH.
