# GitHub release capture with Playwright

This is a single-process Python CLI for an account and repository you control. It signs in with a password and a TOTP code, saves browser state, creates a release from JSON input when that tag has no release, and downloads GitHub's generated source ZIP. Use a **private test repository** for the assignment run. Publishing a release changes that repository; `publish: true` in the input is the explicit switch.

## Workflow

```text
input.json -> validate -> load browser state -> protected-page check
                                      | stale/missing
                                      v
                              password + TOTP -> save state
                                      |
                                      v
repo -> Releases -> Draft a new release -> tag -> target branch -> title/body
                                      |
                                      v
                            publish -> Source code (zip)
                                      |
                                      v
                          ZIP + manifest.json
```

The tag choice makes the target branch field relevant; GitHub documents that target selection follows creation of a new tag. The form also has release title and description. The runner visits the repository, releases page, and release form. On a repeated run it finds the existing release and downloads its archive without publishing again.

## Setup and run

Requires Python 3.11 or newer.

1. Create a private GitHub test repository with at least one commit and a branch to release. In your GitHub account's **Settings → Password and authentication**, enable TOTP. Save the setup key in your secret manager, enroll that same key in your authenticator app, and save GitHub's recovery codes separately. Keep your existing recovery method available.
2. Copy `example.input.json` to `input.json` and change the owner, repository, tag, branch, title, and description. `input.json` is gitignored. The owner must match `GITHUB_USERNAME`, and the CLI rejects unexpected fields, so credentials cannot accidentally be copied into the manifest through this file.
3. Run from a terminal and enter `GITHUB_USERNAME`, `GITHUB_PASSWORD`, and `GITHUB_TOTP_SECRET` when prompted. The password and setup key are entered with terminal echo disabled. For non-interactive runs, inject these variables from a secret manager. Do not paste them into the JSON file or commit them.
4. Run:

```bash
./run.sh --input input.json --output artifacts/first
./run.sh --input input.json --output artifacts/second
```

`run.sh` creates `.venv`, installs the pinned dependencies and Chromium on first use or after `requirements.txt` changes, then runs the CLI. The CLI prints stage progress without credentials or codes. `--headed` shows the browser. To exercise stale state, replace `.state/github.json` with invalid JSON and run again. To request a different archive, change the tag and release fields in the JSON file; no code edit is required.

The manifest records the input, visited URLs, stage timings, winning locator strategies, session path (`fresh_login`, `reused`, or `stale_login`), SHA-256 and size of captured files, and final status. Browser state stays in `.state/` and is set to owner-only permissions. It contains live login cookies and must remain private.

To run the exact resolver offline and inspect each strategy, use:

```bash
.venv/bin/python -m portal_runner.offline --html fixtures/github-release-form.html --targets fixtures/github-release-targets.json
```

This is a sanitized snapshot of the real GitHub release form. Edit it to rename classes, reorder controls, or add wrappers, then repeat the command. The offline tests also wrap that snapshot in an iframe and verify all five fields. The smaller `fixtures/form.html` fixture isolates iframe behavior.

## Safe submission package

Share the repository URL, or run `./package.sh` to make `submission.zip` from committed files only. Do not zip the working directory: `.state/` contains live GitHub cookies, `artifacts/` contains account identifiers, and `.venv/` is a local environment. If you have already shared a ZIP containing `.state/`, revoke those GitHub sessions under **Settings → Sessions**. The published files under `examples/` are redacted.

## Evaluation answers

- **Second factor:** `TOTPProvider.get_code(challenge)` computes a six-digit RFC 6238 code from `GITHUB_TOTP_SECRET` using a 30-second window. A rejected code waits for the next window and retries once. The code is not printed or put in the manifest.
- **Dead session:** the runner opens the protected `/settings/profile` page, requires its `Public profile` heading, and checks the account button or `user-login` metadata against `GITHUB_USERNAME`. These are positive page and identity checks, not only a redirect test. A malformed state file is discarded before context creation. An expired or wrong-account state fails this check; the runner discards that browser context and logs in again. Saved state is written atomically with owner-only permissions.
- **Locators:** `Resolver` tries role and accessible name, label, placeholder, nearby text, tag/attributes, then id and class. It checks all frames at each strategy before accepting a broader structural match. It polls each target for up to six seconds with bounded backoff, requires a unique match, prints the winning strategy, and records it in `manifest.json` under `locator_strategies`. The source link gets a longer 15-second wait because GitHub loads it later. Semantic text survives wrappers, reordered siblings, and renamed CSS classes better than CSS paths. Nearby text for a link or button must resolve to an actual clickable element. The same resolver runs against saved HTML via `find_in_html`, including `iframe srcdoc` fixtures.
- **Portal quirks:** the target branch determines the commit used when the chosen tag is new; the source ZIP is captured through Playwright's native download event. The resolver also searches child frames and is tested offline with a frame fixture.
- **Retries:** navigation and download timeouts or I/O errors retry at most twice with exponential backoff. Authentication rejection gets exactly one new TOTP code. Invalid input and unexpected portal state fail immediately; unresolved or ambiguous locators fail after their bounded wait. Downloads are checked as ZIP archives and atomically replace the destination only after validation. Final failure creates `failure/page.html`, `failure/screenshot.png`, and `failure/failure.json` with the stage, sanitized URL, console entries, and resolver attempts. The HTML sanitizer keeps structural and accessible text while removing executable content, hidden fields, values, URLs, and account identifiers. Screenshots are rendered from that sanitized HTML. If evidence capture itself fails, the manifest records that error without hiding the original failure. A Ctrl+C interruption records `interrupted` and the active stage timing.
- **At 100 portals:** first extract a small portal adapter interface for login, authenticated marker, form, and document actions; keep shared code for evidence, retries, code providers, state, and locators. Add portal-specific contract fixtures and canary runs before adding a queue.

## Verification status

Sixteen offline tests pass with `python -m pytest -q`, covering TOTP, redaction, input validation, the saved GitHub form under iframe/wrapper mutations, a heading that shares its text with a link, a non-main target, wrong-account state, archive validation, interruption status, and malformed saved state. GitHub Actions runs this suite on push and pull request without account secrets. An uninterrupted live run on the account holder's private test repository completed fresh TOTP login, created the `automation-demo-v4` release, and downloaded its source ZIP. A second run reused the saved state and downloaded the same SHA-256 hash, taking 3.8 seconds across stages versus 15.0 seconds for the fresh run. Changing the input JSON to a second tag also created and downloaded another release, with a different ZIP hash. The captured manifests and failure bundles are under gitignored `artifacts/` on the run machine. These raw manifests contain account and repository identifiers, so do not share the `artifacts/` directory or local `.state/` files.

`examples/v4-fresh-manifest.json` and `examples/v4-repeat-manifest.json` are redacted copies of the uninterrupted fresh and repeat runs. `examples/v5-reuse-manifest.json` is a later successful reuse run on the updated locator code and shows the `locator_strategies` map. Earlier `examples/fresh-manifest.json`, `examples/success-manifest.json`, and `examples/stale-manifest.json` cover the original evaluation scenarios. `examples/failure/` is a sanitized bundle from a deliberately broken locator on the real GitHub form, with no release published. Other live failure bundles remain local in `artifacts/`.

The later `automation-demo-v3` validation began with no saved state: the runner completed TOTP login, saved state, and published the new release. The operator interrupted it before the download stage. A recovery run reused that state, found the published release, and downloaded a valid ZIP with SHA-256 `91e605344c3e6f4e412b089c17e9c62891821a1a19fd4b2fc6118fa6a6c83b9d`; its redacted manifest is `examples/v3-recovery-manifest.json`. This verifies fresh login and release creation on the hardened code, plus idempotent recovery and download. It does not represent one uninterrupted post-hardening command.

The corrupted-state login test passed: the manifest recorded `stale_login`, ran the full TOTP login, and downloaded the same ZIP hash as the reused-session run. The separate no-state test recorded `fresh_login`, completed TOTP login, and downloaded the same ZIP hash. The fresh and stale login runs took longer than the reused-session run.

The current code passed the offline suite and the uninterrupted v4 fresh and repeat runs. GitHub may change its release form and authentication flow, so keep a private canary repository and inspect failed-run evidence when the portal changes.

GitHub reference: [2FA login](https://docs.github.com/en/authentication/securing-your-account-with-two-factor-authentication-2fa/accessing-github-using-two-factor-authentication), [release flow](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository), [Playwright state](https://playwright.dev/python/docs/auth), [downloads](https://playwright.dev/python/docs/downloads).
