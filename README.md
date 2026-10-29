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

1. Create a private GitHub test repository with at least one commit and a branch to release. In your GitHub account's **Settings → Password and authentication**, enable TOTP. Save the setup key in your secret manager, enroll that same key in your authenticator app, and save GitHub's recovery codes separately. Keep your existing recovery method available.
2. Copy `example.input.json` to `input.json` and change the owner, repository, tag, branch, title, and description. `input.json` is gitignored.
3. Run from a terminal and enter `GITHUB_USERNAME`, `GITHUB_PASSWORD`, and `GITHUB_TOTP_SECRET` when prompted. The password and setup key are entered with terminal echo disabled. For non-interactive runs, inject these variables from a secret manager. Do not paste them into the JSON file or commit them.
4. Run:

```bash
./run.sh --input input.json --output artifacts/first
./run.sh --input input.json --output artifacts/second
```

`run.sh` creates `.venv`, installs the dependencies and Chromium on first use, then runs the CLI. `--headed` shows the browser. To exercise stale state, replace `.state/github.json` with invalid JSON and run again. To request a different archive, change the tag and release fields in the JSON file; no code edit is required.

The manifest records the input, visited URLs, stage timings, session path (`fresh_login`, `reused`, or `stale_login`), SHA-256 and size of captured files, and final status. Browser state stays in `.state/` and is set to owner-only permissions. It contains live login cookies and must remain private.

To run the exact resolver offline and inspect each strategy, use:

```bash
.venv/bin/python -m portal_runner.offline --html fixtures/form.html --targets fixtures/targets.json
```

Edit the fixture HTML to rename classes, reorder controls, add wrappers, or move the form into or out of the iframe, then repeat the command.

## Evaluation answers

- **Second factor:** `TOTPProvider.get_code(challenge)` computes a six-digit RFC 6238 code from `GITHUB_TOTP_SECRET` using a 30-second window. A rejected code waits for the next window and retries once. The code is not printed or put in the manifest.
- **Dead session:** the runner opens the protected `/settings/profile` page and requires both its `Public profile` heading and an account menu marker. A malformed state file is discarded before context creation. A valid-looking but expired state fails the protected-page check and triggers full login.
- **Locators:** `Resolver` tries role and accessible name, label, placeholder, nearby text, tag/attributes, then id and class. It requires a unique visible match and records the winning strategy. Semantic text survives wrappers, reordered siblings, and renamed CSS classes better than CSS paths. Nearby text for a link or button must resolve to an actual clickable element. The same resolver runs against saved HTML via `find_in_html`, including `iframe srcdoc` fixtures.
- **Portal quirks:** the new-tag choice exposes the target branch choice; the source ZIP is captured through Playwright's native download event. The resolver also searches child frames and is tested offline with a frame fixture.
- **Retries:** navigation and download timeouts or I/O errors retry at most twice with exponential backoff. Authentication rejection gets exactly one new TOTP code. Invalid input, ambiguous locators, and unexpected portal state fail immediately. Final failure creates `failure/page.html`, `failure/screenshot.png`, and `failure/failure.json` with the stage, sanitized URL, console entries, and resolver attempts. Screenshots are rendered from scrubbed HTML rather than captured raw from the logged-in page.
- **At 100 portals:** first extract a small portal adapter interface for login, authenticated marker, form, and document actions; keep shared code for evidence, retries, code providers, state, and locators. Add portal-specific contract fixtures and canary runs before adding a queue.

## Verification status

Five offline tests pass with `python -m pytest -q`, covering TOTP, redaction, iframe/wrapper mutations, a heading that shares its text with a link, and malformed saved state. A live run on the account holder's private test repository accepted a generated TOTP code, saved state, created a release, and downloaded its source ZIP. A repeated run reused the saved state and produced the same SHA-256 hash. Changing the input JSON to a second tag created and downloaded another release, with a different ZIP hash. The captured manifests and failure bundles are under gitignored `artifacts/` on the run machine.

`examples/success-manifest.json` is a redacted copy of the successful changed-input run, and `examples/stale-manifest.json` records the corrupted-state recovery run. `examples/failure/` is a synthetic, offline failure bundle that demonstrates the screenshot, HTML, URL, console, stage, and resolver-attempt format without exposing account data. The live failure bundles remain local in `artifacts/`.

The corrupted-state login test also passed: the manifest recorded `stale_login`, ran the full TOTP login, and downloaded the same ZIP hash as the reused-session run. A no-state live success still needs a separate run; it follows the same login branch, and the first live run already verified that GitHub accepted the generated code. This workspace has no Git remote, so a GitHub submission link is still pending.

GitHub reference: [2FA login](https://docs.github.com/en/authentication/securing-your-account-with-two-factor-authentication-2fa/accessing-github-using-two-factor-authentication), [release flow](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository), [Playwright state](https://playwright.dev/python/docs/auth), [downloads](https://playwright.dev/python/docs/downloads).
