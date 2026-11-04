# System reminders and calendar

Run the helper through:

    python3 ~/.codex/skills/personal-gtd/scripts/gtd.py COMMAND

The helper compiles a local EventKit bridge into
~/.codex/personal-gtd/runtime/Personal GTD Bridge.app. It includes calendar and
reminder usage descriptions. Data commands use LaunchServices to keep the same
app permission identity in Codex and in the login agent. doctor checks authorization without prompting;
authorize requests macOS full read/write access. Read existing items only with
the user's authorization; this does not mean every proposed task is approved.

If the configured model route uses a local HTTPS gateway, its existing public
CA certificate can be configured for this helper only:

    python3 ~/.codex/skills/personal-gtd/scripts/gtd.py configure --ca-certificate /absolute/path/gateway.crt

This sets CODEX_CA_CERTIFICATE only for the extractor subprocess. It keeps TLS
verification enabled, does not change the system trust store, and does not copy
model credentials or alter the global Codex configuration. Use the certificate
provided by your own gateway administrator; never disable TLS verification.

## Read

snapshot saves private JSON at ~/.codex/personal-gtd/last-snapshot.json. It includes
all incomplete reminders, completions from the past seven days, and calendar
events from seven days ago through fourteen days ahead. Missing permissions are
reported separately; missing arrays mean unreadable, not zero items.

Read relevant fields from that snapshot. Use the actual list/calendar identifiers
and writable flags. Do not substitute a guessed account, create another formal
task database, or overwrite unrelated user notes.

## Write a clarified batch

After the user has clarified/accepted the proposed changes in the conversation or explicitly confirmed the workbench preview,
write a JSON plan to the private state folder, then invoke:

    python3 ~/.codex/skills/personal-gtd/scripts/gtd.py apply --plan /absolute/path/plan.json

An example plan shape (identifiers are placeholders, not real destinations):

    {
      "operations": [
        {
          "op_id": "a-new-UUID",
          "action": "create_reminder",
          "candidate_key": "the-local-candidate-key",
          "list_id": "actual-list-id-from-snapshot",
          "title": "向导师确认下次讨论时间",
          "notes": "Confirmed in Codex; include useful source context.",
          "due": "2026-11-06"
        }
      ]
    }

Each op_id belongs to one exact operation; keep it when checking a retry and never
reuse it for different content. Receipts prevent repeating successful writes.
If the helper reports an uncertain previous write, read the system state and
reconcile first. Do not automatically issue a new create with a new ID.

Supported actions:

| action | Fields |
|---|---|
| create_list | title, optional source_id; use only when creating a list is within the user's accepted setup |
| create_reminder | list_id, title; optional notes, due, priority 0–9, url |
| update_reminder | id; supplied fields only, plus optional list_id, completed, expected_modified_at |
| create_event | calendar_id, title, start, end; optional all_day, notes, location, url |
| update_event | id and original_start from the exact snapshot occurrence; optional occurrence_date, expected_modified_at and changed fields |

due can be a strict YYYY-MM-DD date (no invented time) or an ISO datetime with an
explicit offset. Event start/end are explicit, and end must be later than start.
All-day events use an exclusive end date.

For existing-item updates, carry expected_modified_at from the fresh snapshot.
For recurring events, carry original_start and occurrence_date from the chosen
instance; save affects only that instance. Never use only the series ID.

Reminders notes preserve the helper's operation marker. Receipt IDs are technical
write provenance, not a second copy of the authoritative task state.

## GTD organization

Prefer the user's existing lists when they fit. If they choose a dedicated GTD
setup, suggest Inbox, Next, Waiting, Someday and Projects lists and clarify which
account to use from the actual snapshot. A multi-step outcome is a project with
a concrete next action. Waiting-for items should say who/what is pending and,
when appropriate, when to check again.

Calendar holds fixed-time or fixed-date commitments and user-approved time blocks.
A task due date is not automatically a calendar booking. Keep reference material
and someday ideas out of the immediate action list.

Sources: [GTD fundamentals](https://gettingthingsdone.com/what-is-gtd/),
[GTD project guidance](https://gettingthingsdone.com/2017/05/managing-projects-with-gtd/).
