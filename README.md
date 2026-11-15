# Skylight Lists — reviewed personal fork

**Experimental, unofficial Home Assistant integration for Skylight shopping/to-do lists only.**

Forked from [MegaTheLEGEND/skylight_calendar](https://github.com/MegaTheLEGEND/skylight_calendar) at upstream commit
[6d9b8dee](https://github.com/MegaTheLEGEND/skylight_calendar/commit/6d9b8dee9a5950c1fac89a20ec9314c7ef1203be).
Original work is covered by the repository's MIT license.

## What it does

- Discovers Skylight frames/households during sign-in.
- Exposes each Skylight list as a Home Assistant \`todo\` entity.
- Reads, adds, completes/uncompletes, renames and deletes individual list items.
- Polls lists every 120 seconds by default; configurable 30–7200 seconds.

**Intentionally removed:** calendar events, chores, photos, images, photo uploads,
home/device settings, alarms, recipes, rewards, and domain-wide write services.

## Security model and limitations

- **The Skylight API is undocumented and unofficial.** Endpoints may change.
- **Skylight OAuth requests \`scope=everything\`.** The existing Skylight OAuth
  client appears to require this. Removing unrelated features from the fork
  reduces what this code *does*, but it cannot technically prevent a stolen token
  from accessing other information authorized by Skylight.
- No Skylight password is saved by this integration. OAuth access/refresh tokens
  are saved by Home Assistant's configuration-entry storage. Protect your HA
  backups, configuration directory, and administrator access.
- HTTP destinations are restricted to Skylight's HTTPS API/OAuth endpoints.
  Automatic HTTP redirects are disabled to avoid accidentally forwarding tokens.
- Raw Skylight responses, request bodies, and OAuth tokens are not written to logs
  or errors. Use care when enabling network diagnostics elsewhere in Home Assistant.
- This is a code review and hardening pass, **not** an independent security certification.

## Installation

This is a **draft**, not ready for a primary household until integration testing
against a disposable Skylight list is complete.

1. In Home Assistant Marketplace, add this repository's HTTPS GitHub URL.
2. Install the custom integration and restart Home Assistant.
3. Settings → Devices & services → Add integration → Skylight.
4. Complete the Skylight OAuth code flow. Keep authorization codes and URLs private.
5. Find the Skylight lists in **To-do lists**.
6. Test creating, checking, unchecking, renaming, and deleting an item in a
   disposable test list **before configuring bidirectional sync**.

Use Home Assistant's separate Alexa integration and a thoroughly tested
to-do sync mechanism to select and bridge the two lists. This integration
does **not** itself implement Alexa synchronization.

## Current verification status

- Static review of network destinations, authentication/error handling,
  and runtime imports.
- Static regression tests included under \`tests/\`.
- Not yet tested against live Skylight credentials or a physical frame.
- Verify the Skylight API accepts the \`label\` field on list-item update; the
  upstream integration used \`name\` despite reading/creating \`label\`.
- Review list deletion/propagation behavior before enabling sync; deletion
  should never be inferred from an API failure.

## Updates

Review upstream commits individually; do not blindly replace this fork
with upstream versions. Keep a known-good commit installed in production.
