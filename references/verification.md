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

## Explicit limits

- Fixture writes prove state transitions and write receipts, not successful writes into every user's Apple account.
- Posting a notification successfully is distinct from seeing its banner or personally clicking it. macOS notification and Focus settings still determine presentation.
- The app is locally compiled/ad-hoc signed. There is no notarized binary distribution.
- Computer History must be enabled by its user. A loaded GTD subscriber does not prove the recorder is running or that every event has been processed.
- CLI feature flags and the local event-file schema may evolve. Background extraction retains failed batches and makes persistent failures visible.
