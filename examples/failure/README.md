# Deliberate failure on the real portal

This bundle came from a read-only run on the account holder's private GitHub release form. The session check passed, then the `find_release` stage deliberately searched for a field that does not exist. The runner wrote this bundle through its normal final-failure path. No release was published by this run.

`failure.json` contains the sanitized URL, console entries, stage, and locator attempts. `page.html` is a reduced, sanitized HTML view. `screenshot.png` is a screenshot rendered from that view so account data and form values are absent.
