"""Small localhost portal for credential-free evaluation of the runner."""
import asyncio
from contextlib import contextmanager
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import threading
from urllib.parse import parse_qs, quote, urlsplit
import zipfile

from .codes import Challenge, TOTPProvider
from .errors import FinalError
from .locator import Target
from .runner import Runner


USERNAME = "demo"
PASSWORD = "local-demo-password"
SECRET = "JBSWY3DPEHPK3PXP"


@contextmanager
def serve(data: dict, database: Path):
    database.parent.mkdir(parents=True, exist_ok=True)

    class Portal(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Never log credentials or codes sent to the local server.

        def send(self, status: int, body: str | bytes = b"", *, location: str | None = None,
                 cookie: str | None = None, content_type: str = "text/html; charset=utf-8"):
            payload = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            if content_type == "application/zip":
                self.send_header("Content-Disposition", 'attachment; filename="source.zip"')
            if location:
                self.send_header("Location", location)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(payload)

        def authenticated(self):
            return "mock_session=valid" in self.headers.get("Cookie", "")

        def releases(self):
            try:
                return json.loads(database.read_text())
            except (OSError, ValueError):
                return {}

        def do_GET(self):
            path = urlsplit(self.path).path
            root = f'/{data["owner"]}/{data["repository"]}'
            if path == "/login":
                return self.send(200, '<form method="post" action="/login"><label>Username or email address'
                                 '<input name="username"></label><label>Password<input name="password" '
                                 'type="password"></label><input type="submit" value="Sign in"></form>')
            if path == "/sessions/two-factor":
                return self.send(200, '<form method="post" action="/sessions/two-factor">'
                                 '<label>Authentication code<input name="code" type="text" '
                                 'oninput="if(this.value.length===6)this.form.requestSubmit()"></label>'
                                 '<button type="submit">Verify</button></form>')
            if not self.authenticated():
                return self.send(303, location="/login")
            if path == "/settings/profile":
                return self.send(200, '<h1>Public profile</h1><button data-login="demo">demo</button>')
            if path == root:
                return self.send(200, f'<h1>Repository</h1><a href="{root}/releases">Releases</a>')
            if path == root + "/releases":
                return self.send(200, f'<h1>Releases</h1><a href="{root}/releases/new">'
                                 'Create a new release</a>')
            if path == root + "/releases/new":
                branch = html.escape(data["target_branch"], quote=True)
                return self.send(200, f'''<h1>New release</h1><form method="post" action="{root}/publish">
                  <label>Tag<input name="tag" oninput="document.getElementById('branch').disabled=!this.value"></label>
                  <label>Target branch<select id="branch" name="target_branch" disabled>
                    <option value="{branch}">{branch}</option></select></label>
                  <label>Release title<input name="title" placeholder="Title"></label>
                  <label>Release description<textarea name="description"></textarea></label>
                  <button type="submit">Publish release</button></form>''')
            prefix = root + "/releases/tag/"
            if path.endswith("/archive.zip") and path.startswith(prefix):
                tag = path[len(prefix):-len("/archive.zip")]
                release = self.releases().get(tag)
                if release is None:
                    return self.send(404, "Missing archive")
                content = BytesIO()
                with zipfile.ZipFile(content, "w") as archive:
                    entry = zipfile.ZipInfo("release/document.txt", date_time=(1980, 1, 1, 0, 0, 0))
                    archive.writestr(entry, json.dumps(release, sort_keys=True))
                return self.send(200, content.getvalue(), content_type="application/zip")
            if path.startswith(prefix):
                tag = path[len(prefix):]
                if tag not in self.releases():
                    return self.send(404, "<h1>Release not found</h1>")
                return self.send(200, f'<h1>Release</h1><details open><summary>Assets</summary>'
                                 f'<a href="{prefix}{quote(tag)}/archive.zip">Source code (zip)</a></details>')
            return self.send(404, "Not found")

        def do_POST(self):
            size = min(int(self.headers.get("Content-Length", "0")), 10000)
            form = {key: values[0] for key, values in
                    parse_qs(self.rfile.read(size).decode()).items()}
            path = urlsplit(self.path).path
            root = f'/{data["owner"]}/{data["repository"]}'
            if path == "/login":
                if form.get("username") != USERNAME or form.get("password") != PASSWORD:
                    return self.send(200, '<h1>Incorrect username or password</h1>')
                return self.send(303, location="/sessions/two-factor")
            if path == "/sessions/two-factor":
                expected = asyncio.run(TOTPProvider(SECRET).get_code(Challenge()))
                if form.get("code") != expected:
                    return self.send(200, '<h1>Invalid authentication code</h1>'
                                     '<form method="post" action="/sessions/two-factor">'
                                     '<label>Authentication code<input name="code"></label>'
                                     '<button type="submit">Verify</button></form>')
                return self.send(303, location="/settings/profile",
                                 cookie="mock_session=valid; Path=/; HttpOnly; SameSite=Lax")
            if path == root + "/publish" and self.authenticated():
                tag = form.get("tag", "")
                if not tag or not all(form.get(field) for field in
                                      ("target_branch", "title", "description")):
                    return self.send(400, "Incomplete release")
                releases = self.releases()
                releases[tag] = form
                database.write_text(json.dumps(releases))
                return self.send(303, location=f"{root}/releases/tag/{quote(tag)}")
            return self.send(403, "Forbidden")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Portal)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class MockRunner(Runner):
    def __init__(self, browser, data, output, state, base_url, break_step=False):
        super().__init__(browser, data, output, state, code_provider=TOTPProvider(SECRET))
        self.base_url = base_url
        self.break_step = break_step

    async def visit(self, url):
        return await super().visit(url.replace("https://github.com", self.base_url, 1))

    async def release_exists(self):
        exists = await super().release_exists()
        if self.break_step:
            await self.resolver.find(self.page, Target(
                "deliberately_missing_field", role="textbox", name="Never exists", tag="input"),
                timeout_ms=0)
        return exists

    async def create_release(self):
        await self.open_release_form()
        branch = await self.resolver.find(self.page, Target(
            "target_branch", label="Target branch", tag="select", scope="form"))
        if not await branch.is_disabled():
            raise FinalError("Mock dependency was not disabled before tag entry")
        await self.fill(Target("tag", label="Tag", tag="input", scope="form"), self.data["tag"])
        if await branch.is_disabled():
            raise FinalError("Mock dependency did not activate after tag entry")
        await branch.select_option(self.data["target_branch"])
        await self.fill(Target("release_title", label="Release title", tag="input", scope="form"),
                        self.data["title"])
        await self.fill(Target("release_description", label="Release description",
                               tag="textarea", scope="form"), self.data["description"])
        await self.click(Target("publish_release", role="button", name="Publish release",
                                names=("Publish",), tag="button", input_type="submit", scope="form"))
        await self.page.wait_for_load_state("domcontentloaded")
        if not await self.release_exists():
            raise FinalError("Mock release was not visible after publishing")


async def run_mock(browser, data: dict, output: Path, state: Path, break_step: bool = False):
    if data["owner"] != USERNAME:
        raise ValueError("Mock input owner must be demo")
    values = {"GITHUB_USERNAME": USERNAME, "GITHUB_PASSWORD": PASSWORD,
              "GITHUB_TOTP_SECRET": SECRET}
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        database = state.parent / "mock-releases.json"
        with serve(data, database) as base_url:
            await MockRunner(browser, data, output, state, base_url, break_step).run()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
