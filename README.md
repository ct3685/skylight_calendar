# Skylight Lists

**Experimental, unofficial Home Assistant integration for Skylight shopping/to-do lists only.**
It exposes each Skylight list as a Home Assistant `todo` entity so a sync engine can pair it
with Alexa's shopping list (or any other `todo` entity).

Forked from [MegaTheLEGEND/skylight_calendar](https://github.com/MegaTheLEGEND/skylight_calendar) at upstream commit
[6d9b8dee](https://github.com/MegaTheLEGEND/skylight_calendar/commit/6d9b8dee9a5950c1fac89a20ec9314c7ef1203be).
Original work is covered by the repository's MIT license.

## What it does

- Signs in with Skylight's OAuth (authorization code + PKCE) and refreshes tokens automatically.
- Creates one `todo` entity per Skylight list (shopping and to-do lists alike).
- Reads, adds, completes, un-completes, renames and deletes list items.
- Polls every 120 seconds by default (30–7200 s, configurable). Writes refresh immediately.

**Intentionally removed:** calendar events, chores, photos, image uploads, device
settings, alarms, recipes, rewards and every domain-wide service. Creating or
deleting whole Skylight lists is not supported.

**Not included:** synchronization itself. Use [To-do List Sync](#two-way-sync-with-alexa)
for that.

### Behaviour that matters for sync

- **Outages never look like empty lists.** If Skylight times out, errors, returns malformed
  JSON, paginates, or omits items it says the list contains, the refresh fails and *all*
  Skylight `todo` entities become `unavailable` until the next good poll. An item without an
  id fails the read rather than silently vanishing.
- A Skylight list that is deleted (or disappears from the feed) turns its entity `unavailable`, not empty.
- Completing an item keeps it in Skylight as completed (`status: completed`); un-completing sets `pending`.
- Deletes are verified: after `DELETE`, the list is re-read. Items still present are retried
  through Skylight's `bulk_destroy` endpoint. If they still remain, or the list cannot be
  re-read, the action fails visibly. An unverified delete is never reported as done.
- Duplicate item names are allowed. Items are addressed by their Skylight id.
- Write failures raise an error from the `todo.*` action, so a sync engine sees them.

## Security model and limitations

- **The Skylight API is undocumented and unofficial.** Endpoints may change without notice.
- **Skylight OAuth requests `scope=everything`.** The web/mobile OAuth client Skylight uses
  offers no narrower scope that we could find (none of the community clients use one).
  This fork reduces what the *code* does. It cannot stop a stolen token from reaching
  calendar, photos, or other account data. Treat the Home Assistant config directory and backups
  as holding full Skylight account access.
- No Skylight password is stored. OAuth access/refresh tokens live in Home Assistant's
  config-entry storage (`.storage/core.config_entries`), which is included in HA backups.
- Requests only go to `https://app.ourskylight.com`. Redirects are never followed.
  Resource ids are validated before they are placed in a URL path.
- Tokens, request bodies and Skylight response bodies are never logged or put in error
  messages. Errors only carry the method and HTTP status.
- Re-authentication must sign in to an account that can still see the configured frame.
  Otherwise it aborts (`wrong_account`).
- No third-party Python dependencies, no services, no file access, no dynamic code.
- This is a code review and hardening pass, **not** an independent security certification.

## API contract and verification status

| Operation | Request | Evidence |
|---|---|---|
| Read list items | `GET /api/frames/{frame}/lists/{list}?include=list_items` | Used live by upstream; fallback `GET …/list_items` |
| Add item | `POST …/list_items` `{"label": "Milk"}` | Upstream, pyskylight, skylight-mcp |
| Complete / un-complete | `PUT …/list_items/{id}` `{"status": "completed"\|"pending"}` | Upstream, pyskylight, skylight-mcp |
| Rename | `PUT …/list_items/{id}` `{"label": "Oat milk"}` | pyskylight, skylight-mcp (upstream sent `name`, likely ignored) |
| Delete | `DELETE …/list_items/{id}`, fallback `DELETE …/list_items/bulk_destroy` `{"ids": [...]}` | pyskylight, skylight-tools |
| Token refresh | `POST /oauth/token` `grant_type=refresh_token` (rotating) | Upstream |

**None of this has been verified live against Skylight by this fork.** The automated tests (below)
run against an in-memory model of these contracts. Run the [disposable-list test](#3-test-with-a-disposable-skylight-list)
before you connect a household list.

## Installation (Home Assistant Marketplace / HACS)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/ct3685/skylight_calendar`, type **Integration**.
2. Open **Skylight Lists** → **Download** → restart Home Assistant.
3. This integration uses the domain `skylight`. Do not install another Skylight integration at the same time.

### 1. Authenticate

Skylight only redirects its OAuth client to its own web app (`ourskylight.com/welcome`),
which immediately navigates away, so the code has to be captured from the browser's network log.

1. Settings → Devices & services → **Add integration** → **Skylight Lists**. Keep this dialog open.
2. In a new browser tab, press F12 → **Network** → tick **Preserve log**.
3. In that tab, open the **Skylight sign-in** link from the dialog and sign in. If the browser
   is already signed in to Skylight, the login form may be skipped. Check it is the right account.
4. Skylight's page redirects a few times. Type `code=` in the Network filter and copy the first
   `ourskylight.com/welcome?code=…` URL (right-click → Copy → Copy URL).
5. Paste the whole URL (or only the code) into the dialog and submit straight away. Codes are single-use.
   If a code is rejected, open the same dialog link again. It stays valid until the dialog closes.
6. If the account has several frames, pick the one with your lists.
7. When tokens are later rejected (password change, revoked session), Home Assistant shows
   **Re-authentication required**. Repeat steps 2–5 there.

Never paste codes, URLs containing `code=`, or `.storage` files into issues or chats.

### 2. Find the entities

Settings → Devices & services → Skylight Lists → your frame. Each list is
`todo.<frame>_<list>` (for example `todo.home_grocery`). The attributes include
`skylight_list_id` and `skylight_kind` (`shopping` or `to_do`). They also appear in the **To-do lists** sidebar.

### 3. Test with a disposable Skylight list

Create a list named e.g. `HA Test` in the Skylight app, wait up to 2 minutes (or reload the
integration), then check each operation in both directions:

| Step | In Home Assistant (To-do lists or Developer tools → Actions) | Expect in Skylight |
|---|---|---|
| Add | `todo.add_item` item `Test milk` | Item appears |
| Complete | Tick the item | Item shows completed |
| Un-complete | Un-tick it | Item open again |
| Rename | Edit name to `Test oat milk` | Name changes |
| Delete | Delete the item | Item gone |
| Reverse | Add, tick, rename, delete an item in the Skylight app | HA shows each change within 2 minutes |

If **rename** fails with `HTTP 422` or silently does nothing, or **delete** reports
`Skylight did not delete`, stop. Please open an issue with only the error text. The rest of the integration
is still usable, but do not enable sync until it is fixed.

## Two-way sync with Alexa

Use **[To-do List Sync](https://github.com/skornehl/ha-todo-sync)** (`todo_sync`, v0.1.3 reviewed).
It only calls Home Assistant `todo.*` actions: no network access, no dependencies, MIT licence.
It was reviewed for this use, and its merge logic was exercised against Alexa/Skylight scenarios:

- One config entry per pair, so **multiple independent pairs** work (Alexa Shopping ↔ Skylight
  Grocery, Alexa Costco ↔ Skylight Costco). The same pair cannot be added twice.
- Items are matched by name (case/whitespace-insensitive). A snapshot of the last sync
  distinguishes "added on one side" from "deleted on the other" to prevent loops and re-adds.
- **The first sync only adds**: each item missing on one side is copied over. Nothing is deleted or completed.
  Items that are completed only in Skylight are not copied to Alexa.
- **List A is the side that drops checked items (Alexa).** An item disappearing from Alexa is
  treated as checked off, and is completed (not deleted) in Skylight.
- Skips a sync entirely if either list is `unavailable`. That is why this integration
  fails closed instead of reporting an empty list.
- An item must be missing for 6 minutes before its absence is acted on. A sync that would remove
  or check off more than half of the items (minimum 5) is refused.
- Saying "Alexa, add milk" while Skylight still has a completed `Milk` re-opens that item
  instead of adding a duplicate.

Known limits: there is **no dry-run mode**. A rename is synced as "add new name, then check off the old name"
after the 6-minute window. On lists with fewer than 5 items the mass-change guard does not apply.
Alexa normal voice usage is untouched: the Alexa integration keeps using Amazon's real list.

### Mapping

| Change | Result |
|---|---|
| "Alexa, add milk" | `Milk` added to Skylight (≈ Alexa poll + 10 s) |
| Added in Skylight | Added to Alexa (≤ 2 min Skylight poll + 10 s) |
| Checked in Skylight | Checked (removed) in Alexa |
| Checked/removed in Alexa | Completed in Skylight after 6–10 min |
| Un-checked in Skylight | Added to Alexa again |
| Deleted in Skylight | Removed from Alexa |
| Renamed on either side | New name added, old name checked off (after confirm window) |

### Setup

1. **Back up** Home Assistant (Settings → System → Backups → Create backup).
2. **Record both lists.** In Developer tools → Actions, run `todo.get_items` for the Alexa
   list and the Skylight list with `status: [needs_action, completed]`, and save the responses.
   This is your preview of what the first sync will merge, and your restore source.
3. HACS → Custom repositories → `https://github.com/skornehl/ha-todo-sync` (Integration) →
   download **To-do List Sync** (pin v0.1.3) → restart.
4. **Test pair first.** Pair a throwaway Alexa list (if your Alexa integration exposes custom
   lists) with the `HA Test` Skylight list. Settings → Devices & services → Add integration →
   **To-do List Sync**: **List A = the Alexa entity**, **List B = the Skylight entity**.
   Run through the mapping table above, allowing up to 10 minutes per absence-based step.
5. **Real pairs.** Add one entry per pair, always Alexa as A, e.g.
   `todo.alexa_shopping_list` ↔ `todo.home_grocery`. Use the entity ids you see under
   Settings → Entities (filter by `todo.`). After the first sync, compare both lists with your
   step-2 records: only additions should have happened.

## Troubleshooting

- **Sync status:** each pair has a `sensor.<pair>_last_sync` entity. Its `last_error` and `last_changes`
  attributes say what happened. "is not available" means one list was `unavailable`, and sync skipped safely.
- **Skylight entity unavailable:** check Settings → System → Logs for `custom_components.skylight`.
  `HTTP 5xx`, `timed out` or `Could not contact` are Skylight/network outages and recover by themselves.
  `omitted some requested list items` / `paginated` mean Skylight changed its response format. Open an issue.
- **Re-authentication required:** the refresh token was rejected. Re-authenticate from the notification.
- **Mass-change refused:** if you really cleared a list, run `todo_sync.sync_now` with `force: true`.
- **Debug logging:** add the following to `configuration.yaml` temporarily (no secrets are logged):

  ```yaml
  logger:
    logs:
      custom_components.skylight: debug
      custom_components.todo_sync: debug
  ```

## Disable, roll back, update

- **Pause sync:** Settings → Devices & services → To-do List Sync → the pair → ⋮ → **Disable**.
  Lists stop changing immediately. Re-enabling resumes from the stored snapshot.
- **Restore items:** re-add anything lost from the `todo.get_items` records from setup step 2, or restore the backup.
- **Roll back this integration:** HACS → Skylight Lists → ⋮ → **Redownload** → choose the previous
  version, restart. Disable sync first.
- **Update safely:** disable sync pairs, update, restart, confirm the Skylight entities load
  and the disposable-list test still passes, then re-enable sync. Review upstream changes
  individually; do not blindly replace this fork with upstream.

## Continuous operation (Windows-hosted HA OS VM)

- Set the VM to **start automatically** with the host (Hyper-V: *Automatic Start Action*;
  VirtualBox/VMware: autostart), and give it an *Automatic Stop Action* of **Shut down**/**Save**
  so Windows Update reboots don't corrupt it.
- Disable sleep/hibernate on the Windows host. A suspended VM stops syncing, and its clock may drift.
- Keep the VM clock synced (HA OS uses NTP). Token expiry depends on correct time.
- Schedule regular Home Assistant backups and store them off the host, since they contain the Skylight tokens.
- Nothing needs inbound network access. Both integrations poll outbound only.

## Future: Apple Reminders

Not part of this release. Possible paths, in order of practicality:

1. **iOS Shortcuts automation** that calls `todo.add_item` / `todo.update_item` on the
   Skylight entity via Home Assistant's companion app. One-way, but robust.
2. **CalDAV** (Home Assistant's CalDAV integration exposes VTODO lists as `todo` entities).
   iCloud no longer serves upgraded Reminders lists over CalDAV, so this only works with a
   CalDAV server you control or legacy lists.

Either path would give a third `todo` entity that To-do List Sync can pair with the Skylight list.

## Development

```sh
python3.14 -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/ruff check . && .venv/bin/pytest -q
```

Tests run Home Assistant 2026.10 (via `pytest-homeassistant-custom-component`) against an
in-memory model of the Skylight list API (`tests/conftest.py`). They cover auth/refresh,
item CRUD, outages and partial reads, and log/secret hygiene.
`scripts/ha_core_smoke.py` boots a real Home Assistant 2026.11.1 core with the same fake
(config flow, every todo action, outage, unload; real network calls are blocked).
CI runs both, plus hassfest and HACS validation.
