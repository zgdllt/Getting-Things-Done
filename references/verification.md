# Verification

This file describes reproducible checks and their limits. It contains no personal runtime receipts, event paths, list identifiers, task data or user screenshots.

## Automated checks

- Standard-library Python unit tests run against temporary SQLite stores and synthetic event segments. They do not read live history, call model providers or write real Calendar/Reminders data.
- Browser verification uses a temporary store and a fake native bridge. It checks session establishment, an already-open-page route change, clarification, preview-before-write, explicit confirmation, completion, capture, dismissal, project/review views, clipboard and mobile layout.
- JavaScript syntax is checked with Node.
- The macOS CI job compiles both Swift components and verifies the ad-hoc signatures in temporary state. No native permissions are requested, no notification is posted and no real task is written.

Run the commands in [README](../README.md). Browser screenshots can be generated with `--artifacts .test-artifacts`. Only deliberately generated synthetic images belong in `docs/images/`.

## Local prototype evidence

The original local prototype passed 28 isolated Python tests and the full browser fixture. Its AppKit/WKWebView window loaded actual local data. A read-only path also passed: stop the workbench service, open the inbox URL scheme, recover the service and authenticated view, and return to Today. The system notification API accepted a native notification.

Those local checks used macOS 27.2 and Codex CLI 0.166.0. They do not establish support for every older macOS/CLI version, model provider or Computer History event schema. The public version changes branding, adds source installation and keeps both native build stamps outside signed app bundles; its release checks are run separately.

Before the initial public push, the public source separately passed all 28 unit tests, the browser fixture with generic demonstration content, both native builds with strict signature checks, and temporary-directory install/update checks. Existing installs are refused without `--update`; unrelated files survive an update. No live system-list access, real task write or model extraction was needed for those release checks.

## v0.3.0 downloadable app

Release packaging uses only the source installation manifest, a generated app icon and compiled native components. Both Mach-O executables target macOS 14 and are checked for the labelled architecture. The app's Info.plist contains no author-specific Python, source or state path. Runtime helpers are copied to private state; the signed app is never a writable state directory.

The release adds three isolated portability regressions (31 total tests): relocated app reuse, use of the precompiled bridge without Xcode or source mutation, and reuse by the separately installed Codex skill. On a logged-in macOS desktop, `verify_packaged_app.py` extracts to a different directory containing spaces, opens the actual app with synthetic state, confirms its authenticated native view and prebuilt bridge, and rechecks byte-for-byte bundle identity plus strict code signing after startup. It does not request Calendar/Reminders permissions or write real tasks.

The arm64 artifact has been built and run on Apple Silicon. macOS 14 is the explicit binary deployment target; runtime acceptance on an actual macOS 14 machine is not claimed by the local macOS 27 check. Intel builds can be made from source.

## Model settings and immediate answer processing (source update)

The source update passed 44 isolated Python tests, JavaScript syntax checking and the browser fixture. The fixture now checks the bottom-left Settings entry, model/effort persistence across reloads, the immediate saved/processing state, automatic candidate/reply updates, model-failure retry and Settings access on mobile. It uses a fake model and fake native bridge; no real system-list writes occur. The source installer includes both new Python modules and clarification prompt/schema.

## Source adapter and local recall update

The 2026-11-05 working-tree update passed 46 isolated Python tests, Python compilation, JavaScript syntax checking and the browser fixture. New checks cover the Computer History adapter boundary, Markdown memory summaries replacing covered raw events, retrieval of an older related clue, legacy timezone migration and persisted clarification drafts. The browser flow still uses a fake native bridge and confirms zero real Calendar/Reminders writes; the fixture also checked the source status section in Settings and completed with no browser errors.

A separate synthetic smoke check made one real `gpt-6.1-terra / high` request through the existing Codex provider. Its answer job completed, generated an explanation and closed the explicitly finished synthetic candidate; the formal-operation journal stayed empty. This establishes that route at the check time, not service availability for every provider or model.

A second real synthetic check used the installed asynchronous answer endpoint with the sparse `/usr/bin:/bin` PATH typical of a GUI login service. Saving returned before inference (about 5 ms in that local check), the detached worker located the user CLI, and its `gpt-6.1-terra / high` job completed with zero formal writes. The worker preserves the caller's PATH priority and adds the usual user/Homebrew CLI locations.

The personal installed variant was merged separately to preserve its existing project-brief code and configuration, and passed its own 54 tests including those project-brief regressions. That private extension and its data are not part of the public source. The v0.2.0 release remains available as the first tagged preview.

## v0.5.0 swipe cards and Toy demo

The update passed 47 isolated Python tests, the existing full workbench browser scenario, and a separate card/Toy browser scenario in Chromium. Card checks cover default display, arrow buttons, keyboard, pointer swipe, horizontal trackpad events with inertia, stable identity after refresh/insertion, draft and selection retention, filters, saved style after reload, an intentionally delayed stale state response after saving settings, and a 390 px viewport with no horizontal overflow. Mobile touch was also exercised using actual Chromium CDP touch events. The Toy ran under a nested `/toy/gtd-demo/` path and completed capture, draft/save, explicit confirmation, completion and style persistence using synthetic data. Independent review repeated touch/vertical-scroll and empty-draft checks. No real model calls or Apple task writes were needed.

Reproduce with `python scripts/verify_cards_browser.py --artifacts .test-artifacts` after installing the existing development requirements and Chromium. `--browser` optionally selects an installed Chrome executable. Build the Toy with `python scripts/build_toy_demo.py`; only explicitly listed public UI resources and synthetic demo data enter the package. The official Toy doctor passed with zero findings. Browser touch emulation is not a claim of physical iPhone/Android device acceptance.

## v0.5.1 all-view card coverage

The same browser scenario now covers standalone clarification questions sharing the main candidate deck, counts/search/filters, drafts across navigation and reload, and saving those answers without calling a model. It also checks that a new standalone answer job becomes visible while older completed results remain in expandable history, including navigation during a deliberately delayed save refresh. Project, waiting, idea, review-step, calendar and summary collections use cards and retain their positions; review checkboxes survive navigation. Two calendar occurrences with the same series ID and different start times verify that both remain reachable. All four views can return to the global list style, and the added views fit a 390 px viewport. Checks use synthetic state and intercepted/fake native operations, with zero real model calls or Apple task writes.

After preview feedback, the card scenario also verifies visible in-card receipts and completion/dismissal/save transitions, a rejected operation keeping its original card, and an acknowledged write followed by a failed state refresh recovering from busy/inert/exit styles with a pending-sync message. Independent Chromium checks verify two list rows still issue separate concurrent completion requests and keyboard completion focuses the next card. Both the original workbench scenario and the card/Toy scenario were rerun after these changes.

The final success-feedback checks cover complete, dismiss, save and restore markers plus list-mode toasts, all using a 0.4-second hold and 0.3-second fade. They also check repeated-toast animation restart, persistent errors and reduced-motion behavior. Native release verification serves UI files from the extracted archive and reads the actual WKWebView's feedback timing. Add `--service-override` to `verify_packaged_app.py` to verify an explicitly configured service takes precedence over the bundled service, using a re-signed temporary copy and an invocation receipt; the release archive remains unchanged.

## History-source snapshot correction (source update after v0.5.1)

Two additional integration tests exercise the real `process → analyze → ingest` path: normalized current and retrospective summaries remain valid through persistence even when a memory changes during inference, while a source absent from the model input is still rejected without consuming its batch. The isolated Python suite now contains 49 tests. The workbench browser scenario also checks the explicit paused-history label, translated source-validation error and pending count in Settings, and removal of the error after recovery. These checks use fake extraction/native operations and do not process real history.

## Finder-tag file organization (source update after v0.5.1)

The current source adds isolated file-organization regressions and a browser scenario for selecting workspaces, preserving recursion choices during polling, editing suggestions, preview-before-write, native-tag receipts, filtering, undo, shared display styles and mobile layout. Fixture operations do not write real user files or Apple task records. `verify_file_tags_native.py` uses temporary files to check custom tag colors, preserving later unrelated tags, stale previews, symlinks and actual file-change events.

An additional temporary-workspace check covered a file arriving during inference and confirmed that saving the organizer's state did not cause another model call. One real `gpt-6.1-terra / high` call used synthetic metadata and task context, returned suggestions and abstention, and wrote neither tags nor formal tasks. Packaged WebView acceptance includes the fifth navigation entry and the native file-picker bridge. Scope, ownership and limits are documented in [file organization](file-organization.md).

## Explicit limits

- Fixture writes prove state transitions and write receipts, not successful writes into every user's Apple account.
- Posting a notification successfully is distinct from seeing its banner or personally clicking it. macOS notification and Focus settings still determine presentation.
- The app is locally compiled/ad-hoc signed. There is no notarized binary distribution.
- Computer History must be enabled by its user. A loaded GTD subscriber does not prove the recorder is running or that every event has been processed.
- CLI feature flags and the local event-file schema may evolve. Background extraction retains failed batches and makes persistent failures visible.
## Board data layer

Implemented in `gtd_board.py`, `gtd_feedback.py` and `gtd_board_notifications.py`, with synthetic
tests in `test_board.py` and fixtures in `test_board_support.py`. `install.py` includes these
files and their existing thread/memory dependencies. The board has no UI or delivery integration.

Decisions filling gaps in the board brief:

- Project IDs use `project:<gtd_threads.norm(name)>`, keeping identity stable across spelling
  punctuation/case changes. Memory spelling is retained for display and memory updates.
  Joins use normalized names; declared priorities and past candidate decisions reuse `match`.
  Explicit snapshot project fields are honored; reminder project context can also come from
  a linked candidate's `target_id`. Titles are never guessed into project associations.
- Activity importance is `min(0.2, 0.2 * unique_active_days / SCAN_DAYS)` (currently 14 days).
  Accepted and resolved candidates count as kept, matching existing `decisions`. Projects/tasks
  have no base importance; reminders/events start at 0.5. Scores are rounded to six decimals.
  Each card's `updated_at` is its board generation time; persisted JSON count keys are strings.
- Confirmed memory stages decide inclusion and stage-based importance. Inference is descriptive:
  existing `left_off`/`remaining` are reused even when the inference cache is older than 12 hours;
  it is never refreshed or used to silently finish a project. Memory context takes precedence.
  An inference-only name without activity or memory does not create a card.
- The host's local timezone defines days, date-only deadlines and quiet hours. Naive datetimes
  are local. Date-only deadlines expire at the end of their local date; datetime deadlines are
  exact instants. Deadline windows and the calendar lookahead are elapsed 24/72/168 hours,
  including their upper boundaries. Calendar cards start at or after now; ongoing events are
  outside the lookahead. `paused` with no valid `next_at` still has no deadline.
- Native reminders accept the existing `due: {value, all_day}` shape as well as ISO strings.
  Candidate links to any reminder in the snapshot suppress that candidate, including completed
  or undated reminders; reminders themselves still obey the brief's incomplete-and-dated filter.
- Pins override the final quadrant, including notification eligibility. A move within the same
  importance half does not train a weight. Pins persist while hidden/temporarily absent and
  expire when a rebuilt card's due/status fingerprint differs. Terminal project feedback also
  removes its pin because its status has changed to a stage outside the visible board.
- `doing`/notification `start` counts as 24 hours of explicit user activity for any card. Project
  memory changes end this override early; otherwise the agent resumes source-based status after
  24 hours. `blocked` and `tool` apply only to projects. Project feedback preserves notes, plans
  and related context. Invalid or unknown feedback raises `ValueError` without a success log.
- Completing any non-project card hides it indefinitely in this local board, preserving the
  candidate and all Apple data. `not_important` hides only the selected card for 30 days, while
  changing its project's weight when available. Snoozing affects notifications for four hours,
  keeping the card visible; the independent 12-hour notification dedupe can last longer.
- Selection is a dry run: `notified.json` stores preview metadata and card evidence, but only
  `mark_notified` consumes delivery windows/day quotas. Repeated previews do not count as sends.
  Q1 cards and Q2 project nudges both respect 12-hour receipt dedupe; Q3/Q4 never notify. Q2 uses
  existing `rank` order and `phrase`; only one Q2 receipt per local delivery day is permitted,
  including outstanding previews. A batch with multiple Q2 receipts is rejected before writes;
  repeated IDs within one batch are deduplicated. Marking requires a previous selection.
- Quiet intervals include their start and exclude their end, and may cross midnight. Equal
  endpoints, `false`, or `null` disable quiet hours. Configured times use ISO `HH:MM` strings.
  Bodies have at most two lines, at most 70 characters each, without the old inline action list.
  Previously previewed notification cards remain available for feedback after leaving the board
  (for example, once a calendar event starts); their saved scoring evidence is logged.
- The CLI requires `--state` explicitly to avoid accidental writes to the live state. Each command
  builds a current board; feedback and notification ranking use the activity cache without refresh.
  The optional `rank(activity=...)` parameter preserves existing callers' refresh behavior.

Validation on 2026-11-11 used Python 3.15, `-B`, synthetic state only, with `TMPDIR` inside this
worktree's ignored `.test-artifacts/`. All 25 board tests and 6 existing thread tests passed.
All new modules are at most 400 lines; AST checks verified every function has annotations and a
docstring. Tests cover artifact mode 0600, all CLI commands, native snapshot shapes, cache-only
execution, feedback effects, pin expiry, local-day/quiet boundaries, and notification receipts.

The exact requested full suite ran 146 tests: 144 passed and two existing integration tests errored
under the managed sandbox. `test_private_http_auth_origin_and_new_token_replace_old_cookie`
cannot bind `127.0.0.1` (`PermissionError: Operation not permitted`).
`test_stop_kills_model_descendants_after_leader_exits` times out because its worker identity check
uses `/bin/ps`, which this sandbox prohibits. Those source/tests were not changed or skipped, and
the complete suite is not claimed as passing. No live-state build, model call, Apple write, UI/Swift
change, notification delivery or commit was performed.

## Board tuning

The five approved smoke-run changes replace the earlier no-base project importance,
all-candidate inclusion and source-order board behavior above. Other Board data layer
decisions remain in effect. Runtime merging lives in `gtd_board_merge.py`; the installer
includes it and the split test helpers imported by `test_board`.

Decisions added where the tuning brief left details open:

- Activity/update recency uses inclusive elapsed windows: 0–7 days, then >7–14 days.
  Future timestamps do not qualify. The activity cache's `last` is already restricted to
  own messages/commits; assistant replies remain context for the unchanged thread scan.
  Cards associated with a project inherit its activity base alongside the existing rules.
- Project associations continue to use exact normalized names, without title guesses or
  new fuzzy project matching. Eligibility route (c) requires a visible project source;
  explicitly dated/accepted candidates can still appear without one. Existing native
  `target_id` suppression takes precedence, including completed or undated reminders.
- Empty evidence means unknown provenance, not affirmative evidence of assistant-only
  origin. A nonempty all-assistant evidence list excludes every inclusion route. Such
  inbox rows remain unsorted; excluded accepted rows are omitted without changing the DB.
  Hidden candidates and candidates already represented by native reminders are not
  counted as unsorted. Unsorted entries retain their candidate fields, add `id`, decode
  `evidence`, and sort by update instant descending, then source ID before the cap of 50.
- Missing deadlines never match each other. Same-day matching compares nonempty ISO date
  parts as stored, with normalized nonempty titles. Next-step containment is normalized
  and limited to a candidate's own matched project. Candidate sibling matching likewise
  requires a nonempty shared normalized project.
- Merge groups are transitive connected components, so matching a third member cannot
  strand an earlier duplicate. Survivor priority is reminder, event, project, candidate;
  equal-priority ties use the smallest source ID. `merged_ids` contains the other IDs,
  sorted and excluding the survivor. `merged` counts absorbed visible source cards,
  not groups; unsorted entries are not merged.
- The survivor retains its title, due/date source, status and project association.
  Importance/urgency and their explanatory reasons come from the strongest member for
  each score independently. The quadrant is recomputed unless pinned. A valid survivor
  pin wins; otherwise valid alias pins use source priority then ID. Each source pin still
  expires on its own due/status fingerprint. Feedback resolves aliases before writing
  preferences or logging, so the log's `item_id` is the survivor's identity.
- Completion/tool feedback suppresses merged aliases as well as the source being
  completed; not-important hides all known members for the same 30-day window. Source
  suppression precedes merging, allowing an explicitly reopened project to return while
  its completed task aliases remain hidden. A project survivor still uses its memory
  stage for terminal visibility rather than a permanent hide on the project itself.
  Snoozes and delivery receipt checks inspect every currently merged ID, preserving
  existing four-hour snoozes and twelve-hour dedupe when source priority changes.
- `updated_at` retains its board-generation meaning. Numeric `activity_at` provides the
  sorting timestamp: latest own activity/memory update for projects, candidate update,
  or native `modified_at` (falling back to `updated_at`, then `created_at`). Missing
  timestamps sort last; a merged card uses its newest member timestamp. Final ties use
  source ID. Task parents point to the surviving project identity after merging, and are
  cleared for self-links or absent parents. `quadrant_counts` mirrors existing `counts`;
  neither includes unsorted candidates, and Q4 has no item cap.
- Local reply filtering happens before the event cap, so excluded replies cannot crowd
  out own activity. Both adapter intake methods follow `local_include_agent_replies`;
  direct `gtd_local.window` calls retain their default reply-inclusive behavior.
- New production/test modules and the expanded board/local/test modules stay below
  400 lines, with annotated, documented new functions and stdlib dependencies only.
  The pre-existing oversized `gtd.py` receives only the requested backfill argument
  change and signature annotations; it is not split as an unrelated refactor.

Validation used `/opt/homebrew/bin/python3.15 -B -m unittest` from `scripts/` and a
worktree-local `TMPDIR`. The full requested ten-module suite ran 163 tests: 161 passed;
the same two unchanged sandbox integration tests errored (localhost bind denied;
worker-stop times out because `/bin/ps` is prohibited). No tests were skipped. The
board/local focused suite accounts for 68 tests. AST checks confirmed annotations and
docstrings in the board/feedback/new test modules; `git diff --check` passed.

A realistic smoke run used `cp` for the required JSON inputs and Python SQLite's backup
API from a read-only source connection for the DB. The board was built only in the
temporary copy, reusing the copied activity cache with no refresh. Its recorded clock
was `2026-11-11T16:01:18.500743+00:00`: Q1 **4**, Q2 **144**, Q3 **3**, Q4 **4**,
**171** unsorted (newest 50 exposed), **13** absorbed cards, **155** board items.
The temporary state directory was deleted; the count-only report is in the worktree's
ignored `.test-artifacts/board-tuning-smoke.json`. No live-state write, model call,
Apple write, delivery, UI change or commit was performed.

## Board tuning 2

This data-layer update supersedes the earlier all-item quadrant counts and the zero
base importance for dated standalone candidates. Other Board data layer and Board
tuning decisions remain in effect.

- Every surviving item has a boolean `top_level`. Only a task with a surviving parent
  and no valid due date is nested. Dated tasks retain their parent and remain top-level;
  tasks whose parent is absent are top-level. The flat `items` list keeps every nested
  ID available to feedback, pins and notification selection/receipts.
- `counts` and `quadrant_counts` both count top-level cards only; `nested_count` counts
  the remaining items. Hierarchy is derived after merging and final board sorting.
  `children` lists nested survivor IDs in that same order, and `children_count` is its
  length. These fields are present on every card (empty for leaves), so a native card
  that absorbs a project also retains the project's nested tasks after parent remapping.
- Existing sequence/containment/sibling rules remain. Additional same-date matching
  uses unique adjacent-character bigrams of `gtd_threads.norm(title)`; projects use
  `norm(next_step or title)`. A nonempty union with Jaccard similarity at least 0.3
  matches, as does coverage of at least 60 percent of a project's nonempty next-step
  bigram set by the other item's title. Empty sets cannot establish a new match.
- The brief's exact deployment example does not satisfy those thresholds by itself:
  its first task has Jaccard 0.1212 and 50 percent coverage against the plan's next step,
  and Jaccard 0.2128 against the second task. To satisfy the explicit example without
  lowering either threshold, an additional narrow fallback is used: a task and its
  explicitly matched project on the same nonempty due date also match when the task's
  normalized title contains the final four normalized characters of the confirmed next
  step (`真机部署` in the example). This is an implementation assumption resolving the
  conflicting requirements, not a change to the stated bigram thresholds or project
  association rules. Regressions check the exact supplied titles, different projects,
  absent/different dates, and unrelated homework-event/paper-review deadlines.
- Project links are resolved before importance scoring. A task with a valid due date
  and no visible matched project starts at 0.5, with reason `有截止日期`. Existing project
  signals, kept/dismissed history, learned weights, clamping and pins still apply.
  Undated/invalid-date standalone tasks and dated tasks with a matched project gain no
  additional base from this rule.
- Only `candidate` deadlines more than 72 elapsed hours overdue receive urgency 0.4
  and reason `已逾期 N 天，可能已处理`. Exactly 72 hours remains urgency 1.0. Existing
  date-only end-of-day parsing and local-calendar-day explanations remain unchanged;
  reminders, events and project plans keep their existing overdue urgency.

Validation on 2026-11-11 used `/opt/homebrew/bin/python3.15 -B -m unittest` from
`scripts/` with a worktree-local `TMPDIR`. The focused command
`test_board test_board_merge test_board_notifications` passed all 76 tests, including
nested-ID move/start/snooze/completion/receipt behavior, native-parent remapping,
the exact 0.3 Jaccard and 60 percent coverage boundaries, the required deployment
example, standalone learning, and stale-overdue boundaries. Independent read-only
review found no additional correctness issues. Modified Python modules are at most
385 lines; AST checks confirmed syntax, annotations and docstrings. Dependencies
remain standard-library only, and `git diff --check` passed.

The complete requested suite (`test_gtd test_local test_options test_providers
test_workbench test_files test_clarification test_release test_threads test_board
test_board_merge test_board_notifications`) ran 199 tests: 197 passed and the same two
unchanged integration tests errored under the managed sandbox. The workbench HTTP test
cannot bind `127.0.0.1` (`PermissionError: Operation not permitted`). The file-worker
stop test times out; `/bin/ps` was separately confirmed prohibited. No tests were
skipped, and the complete suite is not claimed as passing. These invocation counts
include merge/notification classes imported by `test_board` and explicitly requested
again as separate modules.

A smoke build copied JSON inputs with `cp` and the SQLite DB with its backup API from
a read-only connection. Only the temporary copy was built, reusing cached activity.
At `2026-11-12T00:28:52.952781+08:00` (`2026-11-11T16:28:52.952781Z`), the top-level
counts were Q1 **5**, Q2 **26**, Q3 **0**, Q4 **3**, with **121** nested items,
**174** unsorted candidates, **14** absorbed cards and **155** flat board items.
These counts describe this later snapshot/local date. The temporary state was deleted;
the count-only report is in ignored `.test-artifacts/board-tuning2-smoke.json`.
No live-state write, model call, Apple write, notification delivery, UI change or
commit was performed.
