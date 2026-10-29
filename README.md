# Playwright portal document capture

This Python CLI signs in to your GitHub account with password and TOTP, reuses the saved browser session, creates a release for a new tag, and downloads GitHub's generated source ZIP. A local mock portal exercises the same workflow without credentials.

## Quick start: local mock

Requires Python 3.11 or newer. From the repository root, run:

```bash
./run.sh --mock --input fixtures/mock.input.json --output artifacts/mock-first --state .mock/session.json
```

`run.sh` creates a virtual environment, installs the pinned dependencies and Chromium, and starts the mock portal. The run should end with `[run] success`. Check `artifacts/mock-first/manifest.json` for `"status": "success"` and `"session": "fresh_login"`. The downloaded document is `artifacts/mock-first/demo-v1.zip`.

Run it again with the same state file:

```bash
./run.sh --mock --input fixtures/mock.input.json --output artifacts/mock-second --state .mock/session.json
```

The second run should have no `[login]` stage. Its manifest should say `"session": "reused"`, and its downloaded ZIP should have the same SHA-256 hash. For a fresh demonstration after previous runs, choose a new `--state` path.

Run the test suite:

```bash
.venv/bin/python -m pytest -q
```

GitHub Actions runs the same tests on pushes and pull requests.

## Real GitHub run

1. Use a test repository you own with a commit on the branch you will target. Enable authenticator-app two-factor authentication on your GitHub account and save its **TOTP setup key**. This is not a current six-digit code.
2. Create the local input file:

   ```bash
   cp example.input.json input.json
   ```

3. Edit `input.json`. Set `owner` to your GitHub username; `repository` to the repository **name only**, not a URL or `owner/repository`; `target_branch` to an existing branch; and `tag` to a tag without a release. Set `title` and `description`, and keep `"publish": true`. The template values are placeholders.
4. Run the program:

   ```bash
   ./run.sh --input input.json --output artifacts/real-first
   ```

`run.sh` prompts for your username, password, and TOTP setup key. Password and key input are hidden. The code provider generates the six-digit login code automatically. A new tag causes the program to publish a release in the repository named by `input.json`.

Run the same input again to check reuse:

```bash
./run.sh --input input.json --output artifacts/real-second
```

The second manifest should say `"session": "reused"`. The run should skip `create_release` because the tag already has a release. To create a different document, change the tag and title in `input.json` and choose a new output directory; no code change is needed.

For unattended runs, supply these environment variables instead of using the prompts:

| Variable | Value |
| --- | --- |
| `GITHUB_USERNAME` | Account username; must match `owner`. |
| `GITHUB_PASSWORD` | Account password. |
| `GITHUB_TOTP_SECRET` | Base32 authenticator setup key, not a current code. |

The wrapper prompts for missing credentials on each real invocation so it can recover if a saved session is stale. A reused session does not visit the login page.

`input.json`, `.state/`, `.mock/`, and `artifacts/` are gitignored. Do not put credentials in the JSON input or commit saved browser state or raw run output.

## Reproduce the evaluation cases

Run the two mock commands above first. Then use these commands to exercise specific cases:

| Case | Command | Expected result |
| --- | --- | --- |
| Corrupt saved state | `printf broken > .mock/session.json; ./run.sh --mock --input fixtures/mock.input.json --output artifacts/mock-stale --state .mock/session.json` | Full login, success, `"session": "stale_login"`. |
| Change input | `sed 's/demo-v1/demo-v2/' fixtures/mock.input.json > .mock/changed.json; ./run.sh --mock --input .mock/changed.json --output artifacts/mock-changed --state .mock/session.json` | New tag and a different ZIP hash. |
| Break a step | `./run.sh --mock --mock-break --input fixtures/mock.input.json --output artifacts/mock-broken --state .mock/session.json` | Nonzero exit and `artifacts/mock-broken/failure/`. |

The failure directory contains `failure.json`, `page.html`, and `screenshot.png`. These include the stage, sanitized URL, console messages, resolver attempts, and a masked page view.

Run the resolver against saved GitHub HTML without visiting the site:

```bash
.venv/bin/python -m portal_runner.offline --html fixtures/github-release-form.html --targets fixtures/github-release-targets.json
.venv/bin/python -m pytest -q tests/test_core.py::test_mutated_saved_github_form_resolves_offline
```

The mutation test renames classes, reorders a control, adds a wrapper, and puts the form in an iframe. All five controls should still resolve.

## Design

- **Second factor:** `CodeProvider.get_code(challenge)` is async. `TOTPProvider` calculates codes from the setup key and avoids codes near expiry or from a previously used time window. The runner retries a rejected code once with a fresh window. Codes are not logged.
- **Session persistence:** Playwright state is saved after login. On a later run, the program checks a protected profile page, its HTTP status, the “Public profile” heading, and the expected account identity. Missing markers or a login redirect trigger a full login; temporary network or server errors are retried.
- **Navigation and portal quirks:** The program visits the repository, Releases page, and release form, then fills the tag, target branch, title, and description. The target branch selects the commit for a new tag; in the mock, its branch field is disabled until a tag is entered. The live run captures GitHub's native download event. The resolver can search iframes, as the offline test shows.
- **Locators:** `Resolver` tries role and name, label, placeholder, nearby text, scoped structure, ID, class, then an optional ordinal fallback. It searches all frames, waits for late controls, requires one visible match, and records the winning strategy. Semantic names come first because they are less affected by class changes and reordered HTML.
- **Retries and evidence:** HTTP 429/5xx, Playwright timeouts, and browser network failures get at most two retries with backoff. Invalid inputs, rejected authentication, unresolved or ambiguous controls, and unexpected page states are final failures. A final failure writes sanitized HTML, a masked screenshot, and diagnostic JSON.

`manifest.json` records the JSON inputs, pages visited, stage timings, session outcome, retry events, locator strategies, and captured file path, size, and SHA-256. The downloaded ZIP is validated before replacing any existing file.

## Evidence and submission

The repository includes a [mock successful run](examples/success/) with its document and manifest. Redacted real GitHub examples show a [fresh login](examples/fresh.json), [session reuse](examples/reused.json), [stale-session recovery](examples/stale.json), and a [failure bundle](examples/failure/). Submit the repository URL; the downloaded ZIP is the portal-produced document, not a package of this project.

For a hundred portals, first make a small adapter per portal for login, identity check, form traversal, and document capture. Keep code providers, state storage, locators, retries, evidence, and manifests shared. Add saved-form tests and private canary runs for each adapter before adding a queue.

References: [GitHub 2FA](https://docs.github.com/en/authentication/securing-your-account-with-two-factor-authentication-2fa/accessing-github-using-two-factor-authentication), [GitHub releases](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository), [Playwright authentication](https://playwright.dev/python/docs/auth), [Playwright downloads](https://playwright.dev/python/docs/downloads).
