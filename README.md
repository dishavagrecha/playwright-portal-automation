# Playwright portal document capture

A single-process Python CLI signs in to **your own GitHub account** with password and TOTP, reuses Playwright browser state, creates a release from JSON input when needed, and downloads GitHub's generated source ZIP. The repository also includes a localhost mock portal for repeatable grading without credentials. The mock demonstrates the same fresh, reused, stale, changed-input, and failure paths; the [redacted examples](examples/) come from the real GitHub account except where noted.

## Quick start

Requires Python 3.11+. `run.sh` creates `.venv`, installs pinned dependencies and Chromium, then runs the CLI.

```bash
# No credentials or network account needed; creates a local release and ZIP.
./run.sh --mock --input fixtures/mock.input.json --output artifacts/mock-fresh --state .mock/session.json
```

For the real portal, create a private GitHub test repository with a commit and enable authenticator-app 2FA on your account. Save the **TOTP setup key** separately from the rotating six-digit codes and recovery codes. Copy `example.input.json` to gitignored `input.json`, set its owner, repository, tag, target branch, title and description, then run:

```bash
./run.sh --input input.json --output artifacts/first
./run.sh --input input.json --output artifacts/second
```

`run.sh` prompts for missing values in a terminal; password and setup key entry have echo disabled. In unattended runs, inject them from a secret manager. Never put credentials or the setup key in input JSON.

| Environment variable | Real GitHub run | Mock run |
| --- | --- | --- |
| `GITHUB_USERNAME` | Account that owns the input repository | Supplied internally as `demo` |
| `GITHUB_PASSWORD` | Account password | Supplied internally |
| `GITHUB_TOTP_SECRET` | Base32 authenticator setup key, not a current code | Supplied internally |

The JSON schema is `owner`, `repository`, `tag`, `target_branch`, `title`, `description`, and `publish: true`. Unknown fields are rejected. The owner must match `GITHUB_USERNAME`; publishing a release changes that repository. To get a different document, change the tag or other release inputs, without editing code.

## How it works

1. Load saved browser state, then visit GitHub's protected profile page. Require HTTP 200, the `Public profile` heading and the expected account identity. A login redirect or missing markers means a stale session. HTTP 429/5xx and browser network errors are retryable and never trigger a password login directly. Malformed state is discarded.
2. If state is missing or stale, enter the password and get a six-digit code through the injectable `CodeProvider.get_code(challenge)` interface. `TOTPProvider` computes RFC 6238 codes locally. It records the last used 30-second window in a private state-side file, avoids codes near expiry, and uses a new window for the one allowed wrong-code retry. Codes never go to logs or manifests. A rejected password raises `AuthenticationError` before the 2FA step.
3. Visit the repository, Releases page, and release form. For a new tag, choose the target branch, fill the release title and description, then publish. On a repeat run, find the existing release and skip publication. The target branch determines the commit for a new tag; GitHub displays the target selector before tag creation, so we do not claim it appears only afterward.
4. Capture the source ZIP with Playwright's native download event, validate the ZIP, atomically replace the output file, and record its path, byte size and SHA-256 in `manifest.json`. The manifest also includes inputs, visited pages, stage timings, retry events, session path and winning locator strategies.

The live portal quirks are the new-tag/target-branch relationship and the native download. Frame searching is implemented and tested offline against saved HTML; GitHub's live release form does not contain an iframe.

## Locator and retry design

Every interactive target goes through `Resolver`. It tries accessible **role and name**, then **label**, **placeholder**, **nearby text**, **scoped structure**, **ID**, **class**, and an optional **ordinal** fallback. Targets can provide multiple names and labels, so a button reworded from “Publish release” to “Publish” still resolves. Semantic strategies come first because they survive class changes, added wrappers and reordered siblings; structural and positional hints are last because they are more fragile. The resolver searches all frames, requires a unique visible match, polls for up to six seconds with bounded backoff, prints the winning strategy and records it under `locator_strategies` in the manifest. The lazy-loaded source link gets 15 seconds.

`RetryableError`, Playwright timeouts and `net::ERR_*` network failures receive at most two retries with exponential backoff and jitter. HTTP 429/5xx are retryable. Authentication rejection, invalid inputs, ambiguous locators after their polling budget and unexpected portal state are final failures. Login itself can retry transient browser/network failures; a wrong TOTP code is a separate one-time auth retry.

Run the production resolver without visiting a portal:

```bash
.venv/bin/python -m portal_runner.offline --html fixtures/github-release-form.html --targets fixtures/github-release-targets.json
.venv/bin/python -m pytest -q tests/test_core.py::test_mutated_saved_github_form_resolves_offline
```

The saved form came from the real GitHub release page. The test renames classes, reorders a control, adds wrappers and puts the form in an iframe. The smaller `fixtures/form.html` isolates frame behavior.

## Reproduce the evaluation rows locally

The mock server runs inside the CLI process on `127.0.0.1`. It uses demo credentials and a demo TOTP secret; no GitHub account is involved. These commands write only under gitignored `.mock/` and `artifacts/`. Start from a clean local demo with `rm -rf .mock`.

| Evaluation | Command after the quick-start command | Expected result |
| --- | --- | --- |
| No saved state | The quick-start command above | `fresh_login`, ZIP and complete manifest |
| Run again | `./run.sh --mock --input fixtures/mock.input.json --output artifacts/mock-repeat --state .mock/session.json` | `reused`, same ZIP SHA-256, no login page |
| Corrupt state | `printf broken > .mock/session.json; ./run.sh --mock --input fixtures/mock.input.json --output artifacts/mock-stale --state .mock/session.json` | `stale_login`, full TOTP login, valid ZIP |
| Change input | `sed 's/demo-v1/demo-v2/' fixtures/mock.input.json > .mock/changed.json; ./run.sh --mock --input .mock/changed.json --output artifacts/mock-changed --state .mock/session.json` | New tag and a different ZIP hash |
| Mutate HTML | Run the two offline resolver commands above | Five controls resolve after mutation, including the iframe |
| Break a step | `./run.sh --mock --mock-break --input fixtures/mock.input.json --output artifacts/mock-broken --state .mock/session.json` | Nonzero exit and `artifacts/mock-broken/failure/` bundle |

The last command deliberately requests a nonexistent field during `find_release`. A final failure writes `failure.json` with the stage, sanitized URL, error class/message, console entries and only that target's resolver attempts. `page.html` keeps safe IDs and classes while removing scripts, hidden fields, values, links and account identifiers. `screenshot.png` is a real Playwright screenshot with inputs and identity text masked; if the page cannot be captured, it falls back to a render of the sanitized HTML. The [published failure bundle](examples/failure/) is from a read-only, deliberately broken step on the real GitHub release form.

## Evidence and submission

The compact real-portal examples are [fresh](examples/fresh.json), [reused](examples/reused.json), [stale](examples/stale.json), and [failure](examples/failure/). The fresh and reused ZIP hashes match; these examples took about 15.0 s and 4.9 s across stages. The reused example also shows `locator_strategies`. All example text is redacted. Raw `artifacts/` manifests contain account and repository identifiers, and `.state/` contains live cookies: never share either directory.

Share the repository URL or run `./package.sh` to create `submission.zip` from committed files only. Do not zip the working directory. If an earlier ZIP containing `.state/` was shared, revoke those GitHub sessions under **Settings → Sessions**. Run tests with `.venv/bin/python -m pytest -q`; GitHub Actions runs the same suite on pushes and pull requests.

For a hundred portals, first extract a small adapter with four operations: login, authenticated identity check, form traversal, and document capture. Keep code providers, session storage, locator resolution, retries, evidence and manifest generation shared. Give each adapter saved-form contract fixtures and a private canary run. Add a queue only after those contracts are stable.

References: [GitHub 2FA](https://docs.github.com/en/authentication/securing-your-account-with-two-factor-authentication-2fa/accessing-github-using-two-factor-authentication), [GitHub releases](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository), [Playwright authentication](https://playwright.dev/python/docs/auth), [Playwright downloads](https://playwright.dev/python/docs/downloads).
