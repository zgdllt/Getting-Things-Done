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

## Explicit limits

- Fixture writes prove state transitions and write receipts, not successful writes into every user's Apple account.
- Posting a notification successfully is distinct from seeing its banner or personally clicking it. macOS notification and Focus settings still determine presentation.
- The app is locally compiled/ad-hoc signed. There is no notarized binary distribution.
- Computer History must be enabled by its user. A loaded GTD subscriber does not prove the recorder is running or that every event has been processed.
- CLI feature flags and the local event-file schema may evolve. Background extraction retains failed batches and makes persistent failures visible.
