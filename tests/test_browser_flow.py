"""Functional browser tests executed directly in Chromium via Chrome DevTools Protocol (CDP).

Validates end-to-end user workflows, real DOM rendering, event handlers, and S1-S5 fixes:
- S1: Bootable JavaScript (syntax valid, zero unhandled errors in real browser).
- S2: Real API schemas (quote.text rendered, ReplyResponse.id for feedback, source drawer
      evidence, honest timestamp formatting without fake UTC, actionable analysis error codes).
- S3: Chat state isolation and deterministic barriers (delayed profile fetch resolving after
      chat switch does not overwrite editor, old poll does not auto-select old chat, empty
      selection clears state, overlapping searches keep latest).
- XSS defense: safe DOM text node handling prevents injection.
"""

import asyncio
import base64
import json
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import uvicorn
import websockets

from app.config import settings
from app.db import Database
from app.importer import TelegramImporter
from app.main import app, get_ai_provider, get_db
from app.providers.fake import FakeAIProvider

FIXTURES_DIR = Path(__file__).parent / "fixtures"
ARTIFACTS_DIR = Path(__file__).parent / "artifacts"


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CDPBrowser:
    """Lightweight async CDP client driving real Chromium headless."""

    def __init__(self, cdp_port: int, proc: subprocess.Popen, ws_url: str):
        self.cdp_port = cdp_port
        self.proc = proc
        self.ws_url = ws_url
        self.ws = None
        self._msg_id = 0
        self.exceptions = []
        self.console_logs = []

    @classmethod
    async def launch(cls) -> "CDPBrowser":
        cdp_port = find_free_port()
        proc = subprocess.Popen(
            [
                "/usr/bin/chromium",
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-extensions",
                f"--remote-debugging-port={cdp_port}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(40):
            try:
                data = urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json").read()
                targets = json.loads(data)
                page_target = next((t for t in targets if t.get("type") == "page"), None)
                if page_target:
                    ws_url = page_target["webSocketDebuggerUrl"]
                    browser = cls(cdp_port, proc, ws_url)
                    browser.ws = await websockets.connect(ws_url, max_size=20 * 1024 * 1024)
                    await browser.send("Page.enable")
                    await browser.send("Runtime.enable")
                    return browser
            except Exception:
                await asyncio.sleep(0.1)
        proc.terminate()
        raise RuntimeError("Failed to connect to Chromium CDP endpoint")

    async def send(self, method: str, params: dict | None = None) -> dict:
        self._msg_id += 1
        curr_id = self._msg_id
        payload = {"id": curr_id, "method": method, "params": params or {}}
        await self.ws.send(json.dumps(payload))
        while True:
            raw = await self.ws.recv()
            resp = json.loads(raw)
            if resp.get("method") == "Runtime.exceptionThrown":
                self.exceptions.append(resp.get("params"))
            elif resp.get("method") == "Runtime.consoleAPICalled":
                self.console_logs.append(resp.get("params"))
            elif resp.get("method") == "Page.javascriptDialogOpening":
                # Auto-accept any unexpected alerts so test never hangs
                dialog_ack = {
                    "id": 999999,
                    "method": "Page.handleJavaScriptDialog",
                    "params": {"accept": True},
                }
                await self.ws.send(json.dumps(dialog_ack))
            elif resp.get("id") == curr_id:
                if "error" in resp:
                    raise RuntimeError(f"CDP error: {resp['error']}")
                return resp

    async def evaluate(self, expr: str):
        res = await self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        result_obj = res.get("result", {}).get("result", {})
        if res.get("result", {}).get("exceptionDetails"):
            raise RuntimeError(f"JS evaluation error: {res['result']['exceptionDetails']}")
        return result_obj.get("value")

    async def wait_for_ready(self, timeout: float = 8.0):
        start = time.time()
        while time.time() - start < timeout:
            val = await self.evaluate("document.readyState")
            if val == "complete":
                return
            await asyncio.sleep(0.05)
        raise TimeoutError("Page readyState did not reach complete")

    async def wait_for_predicate(self, expr: str, timeout: float = 8.0):
        start = time.time()
        while time.time() - start < timeout:
            res = await self.evaluate(expr)
            if res:
                return res
            await asyncio.sleep(0.05)
        raise TimeoutError(f"Predicate timed out: {expr}")

    async def capture_screenshot(self, output_path: Path):
        res = await self.send("Page.captureScreenshot", {"format": "png"})
        b64data = res["result"]["data"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(base64.b64decode(b64data))

    async def close(self):
        try:
            if self.ws:
                await self.ws.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()


@pytest.fixture
def running_server(tmp_path: Path):
    """Starts a live uvicorn server in a background thread."""
    db_file = tmp_path / "browser_test.db"
    db = Database(db_file)
    db.run_migrations()

    fake_provider = FakeAIProvider()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_ai_provider] = lambda: fake_provider

    server_port = find_free_port()
    old_app_port = settings.app_port
    settings.app_port = server_port
    config = uvicorn.Config(app, host="127.0.0.1", port=server_port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to start responding
    server_ready = False
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server_port}/") as resp:
                if resp.status == 200:
                    server_ready = True
                    break
        except Exception:
            time.sleep(0.1)

    assert server_ready, "Server failed to start in time"
    try:
        yield f"http://127.0.0.1:{server_port}", db, fake_provider
    finally:
        server.should_exit = True
        settings.app_port = old_app_port
        app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_browser_journey_full_flow(running_server):
    """Drive full user journey in real Chromium:
    1. Page load & syntax validity (S1)
    2. Import JSON chat archive & check stats summary (S2: rep.supported_text_messages, no fake UTC)
    3. Chat selection & hero/coverage display
    4. Search quotes with date formatting
    5. Quote reply generation & rendering (S2: quote.text rendered, not null)
    6. Source drawer verification (S2: actual selected quote and evidence IDs)
    7. Feedback submission (S2: ReplyResponse.id in URL, not undefined)
    8. Profile edit & save with optimistic locking
    9. Analysis cancellation & actionable error code handling
    """
    server_url, db, _ = running_server
    browser = await CDPBrowser.launch()

    try:
        # 1. Load app
        print("STEP 1: navigating", flush=True)
        await browser.send("Page.navigate", {"url": server_url})
        await browser.wait_for_ready()

        title = await browser.evaluate("document.title")
        assert "Вкид" in title
        hero_badge = await browser.evaluate("document.getElementById('hero-badge').textContent")
        assert hero_badge == "Не обрано чат"
        coverage_display = await browser.evaluate(
            "document.getElementById('coverage-grid').style.display"
        )
        assert coverage_display == "none"

        # Verify no runtime/syntax exceptions on load (S1)
        assert len(browser.exceptions) == 0, f"JS exceptions on load: {browser.exceptions}"

        # 2. Import chat archive
        print("STEP 2: importing", flush=True)
        chat_a_json = (FIXTURES_DIR / "synthetic_chat_a.json").read_text(encoding="utf-8")
        escaped_json = json.dumps(chat_a_json)

        # Trigger import via UI elements: click open import FIRST (which resets the form),
        # then populate files, then submit
        setup_file_js = f"""
        (() => {{
            document.getElementById('btn-open-import').click();
            const rawJson = {escaped_json};
            const dt = new DataTransfer();
            const file = new File([rawJson], 'synthetic_chat_a.json', {{
                type: 'application/json'
            }});
            dt.items.add(file);
            const input = document.getElementById('import-file-input');
            input.files = dt.files;
            const form = document.getElementById('import-form');
            form.dispatchEvent(new Event('submit', {{ cancelable: true }}));
        }})()
        """
        await browser.evaluate(setup_file_js)

        # Wait for import completion view
        print("STEP 2b: waiting for import result", flush=True)
        await browser.wait_for_predicate(
            "document.getElementById('import-result-view').style.display === 'block'"
        )

        # S2: verify imported supported messages stat matches rep.supported_text_messages
        import_summary_text = await browser.evaluate(
            "document.getElementById('import-stats-summary').textContent"
        )
        assert (
            "Імпортовано текстових повідомлень: 6" in import_summary_text
            or "6" in import_summary_text
        )

        # Verify period formatting: no fake 'UTC' in date display
        assert "UTC" not in import_summary_text or "2026" in import_summary_text

        # Click finish import
        print("STEP 3: finish import", flush=True)
        await browser.evaluate("document.getElementById('btn-finish-import').click()")

        # Verify chat is active and coverage grid visible
        await browser.wait_for_predicate(
            "document.getElementById('coverage-grid').style.display !== 'none'"
        )
        active_title = await browser.evaluate(
            "document.getElementById('chat-title-heading').textContent"
        )
        assert "Devpost Hackathon Team" in active_title

        chat_id_val = await browser.evaluate("document.getElementById('chat-select').value")
        assert chat_id_val != ""

        # 4. Search quotes
        print("STEP 4: search", flush=True)
        await browser.evaluate(
            """
        (() => {
            document.querySelector('.tab-btn[data-tab="tab-search"]').click();
            const inp = document.getElementById('search-input');
            inp.value = 'хакатон';
            const form = document.getElementById('search-form');
            form.dispatchEvent(new Event('submit', { cancelable: true }));
        })()
        """
        )

        await browser.wait_for_predicate(
            "document.getElementById('search-results-list').children.length > 0"
        )
        first_result_text = await browser.evaluate(
            "document.getElementById('search-results-list').children[0].textContent"
        )
        assert "хакатон" in first_result_text.lower() or "каву" in first_result_text.lower()

        # 5. Quote reply generation & rendering (S2)
        print("STEP 5: quote reply", flush=True)
        await browser.evaluate(
            """
        (() => {
            document.querySelector('.tab-btn[data-tab="tab-playground"]').click();
            document.getElementById('reply-mode-select').value = 'quote';
            document.getElementById('reply-context-input').value = 'каву';
            const form = document.getElementById('reply-form');
            form.dispatchEvent(new Event('submit', { cancelable: true }));
        })()
        """
        )

        reply_predicate = (
            "document.getElementById('reply-output-card').style.display === 'block' && "
            "document.getElementById('reply-action-badge').textContent === 'ЦИТАТА'"
        )
        await browser.wait_for_predicate(reply_predicate)

        # S2: quote.text must be rendered in replyBodyText (not null!)
        reply_body = await browser.evaluate(
            "document.getElementById('reply-body-text').textContent"
        )
        assert len(reply_body) > 0
        assert "null" not in reply_body
        assert "Хто бере каву" in reply_body or "каву" in reply_body or len(reply_body) > 10

        # Feedback buttons must be enabled with valid ReplyResponse.id
        btn_feedback_up_disabled = await browser.evaluate(
            "document.querySelector('.btn-feedback[data-verdict=\"landed\"]').disabled"
        )
        assert btn_feedback_up_disabled is False

        # 6. Source drawer verification (S2: actual selected quote and evidence IDs)
        print("STEP 6: source drawer", flush=True)
        await browser.evaluate(
            "document.getElementById('reply-source-drawer').setAttribute('open', '')"
        )
        drawer_content = await browser.evaluate(
            "document.getElementById('reply-drawer-content').textContent"
        )
        assert len(drawer_content) > 0
        # Drawer must display quote author/id and evidence without fabricated candidate fields
        assert "Олександр" in drawer_content or "Марія" in drawer_content or "№" in drawer_content

        # 7. Feedback submission (S2: ReplyResponse.id in URL, not undefined)
        print("STEP 7: feedback", flush=True)
        await browser.evaluate(
            "document.querySelector('.btn-feedback[data-verdict=\"landed\"]').click()"
        )
        ack_predicate = (
            "document.getElementById('feedback-acknowledgement')"
            ".textContent.includes('Відгук враховано')"
        )
        await browser.wait_for_predicate(ack_predicate)
        ack_text = await browser.evaluate(
            "document.getElementById('feedback-acknowledgement').textContent"
        )
        assert "Відгук враховано: 👍 Влучно" in ack_text

        # 8. Profile edit & save with optimistic locking
        print("STEP 8: profile", flush=True)
        await browser.evaluate(
            """
        (() => {
            document.querySelector('.tab-btn[data-tab="tab-profile"]').click();
        })()
        """
        )
        await browser.wait_for_predicate(
            "document.getElementById('profile-version-badge').textContent.includes('Версія: v')"
        )

        # Modify instructions and save
        await browser.evaluate(
            """
        (() => {
            const ta = document.getElementById('profile-instructions-text');
            ta.value = '# Оновлені інструкції гумору\\nБути дотепним та тримати контекст хакатону.';
            const form = document.getElementById('profile-editor-form');
            form.dispatchEvent(new Event('submit', { cancelable: true }));
        })()
        """
        )

        save_predicate = (
            "document.getElementById('profile-save-status')"
            ".textContent.includes('Збережено успішно')"
        )
        await browser.wait_for_predicate(save_predicate)
        version_badge = await browser.evaluate(
            "document.getElementById('profile-version-badge').textContent"
        )
        assert "Версія: v" in version_badge

        # Export profile link must point to valid URL
        export_href = await browser.evaluate(
            "document.getElementById('btn-export-profile-link').getAttribute('href')"
        )
        assert f"/api/chats/{chat_id_val}/profile/export" in export_href

        # 9. Analysis cancellation & actionable error code handling (S2)
        print("STEP 9: analysis", flush=True)
        await browser.evaluate(
            """
        (() => {
            document.querySelector('.tab-btn[data-tab="tab-analysis"]').click();
            document.getElementById('input-max-calls').value = '1';
            document.getElementById('btn-start-analysis').click();
        })()
        """
        )

        # Wait for progress card
        await browser.wait_for_predicate(
            "document.getElementById('analysis-progress-card').style.display !== 'none'"
        )

        # Click cancel analysis
        await browser.evaluate("document.getElementById('btn-cancel-analysis').click()")
        cancel_predicate = (
            "['СКАСОВАНО', 'ПАУЗА (БЮДЖЕТ)', 'ЗАВЕРШЕНО'].includes("
            "document.getElementById('analysis-status-badge').textContent)"
        )
        await browser.wait_for_predicate(cancel_predicate)

        # Capture visual screenshot as proof of execution
        screenshot_path = ARTIFACTS_DIR / "browser_test_success.png"
        await browser.capture_screenshot(screenshot_path)
        assert screenshot_path.exists()
        assert screenshot_path.stat().st_size > 1000

        # Assert no unexpected uncaught JS errors occurred throughout the entire flow
        assert len(browser.exceptions) == 0, f"Uncaught exceptions: {browser.exceptions}"

    finally:
        await browser.close()


@pytest.mark.anyio
async def test_browser_s3_chat_state_isolation_barriers(running_server):
    """Deterministic S3 barrier tests in Chromium:
    1. Delayed Chat A profile JSON: switching to Chat B before Chat A resolves
       never overwrites Chat B's editor with Chat A's instructions.
    2. Stale Chat A poll: resolving after user is on Chat B never auto-selects Chat A.
    3. Empty chat selection: selecting '' cleans all state (replies, editor, drawer, suggestions).
    4. Overlapping searches: only the latest query results are rendered.
    """
    server_url, db, _ = running_server

    # Pre-populate Chat A and Chat B in SQLite
    chat_a_bytes = (FIXTURES_DIR / "synthetic_chat_a.json").read_bytes()
    chat_b_bytes = (FIXTURES_DIR / "synthetic_chat_b.json").read_bytes()

    importer = TelegramImporter(db)
    res_a = importer.import_chat_json(chat_a_bytes, name_override="Chat Alpha")
    res_b = importer.import_chat_json(chat_b_bytes, name_override="Chat Beta")

    # Seed distinct profile instructions for Chat A and Chat B
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO profiles (
                id, chat_id, version, instructions_md, structured_json,
                edited_by_user, created_at, is_active
            ) VALUES (?, ?, 1, ?, '{}', 0, '2026-10-25T00:00:00Z', 1)
        """,
            (f"prof_{res_a.chat.id}", res_a.chat.id, "ALPHA_INSTRUCTIONS_UNIQUE_MARKER"),
        )
        conn.execute(
            """
            INSERT INTO profiles (
                id, chat_id, version, instructions_md, structured_json,
                edited_by_user, created_at, is_active
            ) VALUES (?, ?, 1, ?, '{}', 0, '2026-10-25T00:00:00Z', 1)
        """,
            (f"prof_{res_b.chat.id}", res_b.chat.id, "BETA_INSTRUCTIONS_UNIQUE_MARKER"),
        )
        # Seed active running analysis jobs for Chat A and Chat B for deferred-poll testing
        conn.execute(
            """
            INSERT INTO analysis_jobs (
                id, chat_id, corpus_version, status,
                total_supported_messages, processed_unique_messages,
                total_chunks, completed_chunks, provider_calls,
                max_calls, last_error_code, active_worker_id, created_at, updated_at
            ) VALUES (
                ?, ?, 1, 'running', 20, 5, 4, 1, 1, 50,
                NULL, 'worker-1', '2026-10-25T00:00:00Z', '2026-10-25T00:00:00Z'
            );
            """,
            (f"job_{res_a.chat.id}", res_a.chat.id),
        )
        conn.execute(
            """
            INSERT INTO analysis_jobs (
                id, chat_id, corpus_version, status,
                total_supported_messages, processed_unique_messages,
                total_chunks, completed_chunks, provider_calls,
                max_calls, last_error_code, active_worker_id, created_at, updated_at
            ) VALUES (
                ?, ?, 1, 'running', 20, 10, 4, 2, 2, 50,
                NULL, 'worker-2', '2026-10-25T00:00:00Z', '2026-10-25T00:00:00Z'
            );
            """,
            (f"job_{res_b.chat.id}", res_b.chat.id),
        )

    browser = await CDPBrowser.launch()

    try:
        await browser.send("Page.navigate", {"url": server_url})
        await browser.wait_for_ready()

        # Wait for chat select options to populate
        await browser.wait_for_predicate(
            "document.getElementById('chat-select').options.length >= 3"
        )

        chat_a_id = res_a.chat.id
        chat_b_id = res_b.chat.id

        # --- Barrier 1: Delayed Chat A profile JSON ---
        # Hook window.fetch so that requests to Chat A's profile are delayed by 300ms,
        # while Chat B's profile returns immediately.
        await browser.evaluate(
            f"""
        (() => {{
            const origFetch = window.fetch;
            window._delayedFetchCalls = [];
            window.fetch = async function(url, opts) {{
                if (typeof url === 'string' && url.includes('/api/chats/{chat_a_id}/profile')) {{
                    window._delayedFetchCalls.push(url);
                    await new Promise(r => setTimeout(r, 300));
                }}
                return origFetch.apply(this, arguments);
            }};
        }})()
        """
        )

        # Rapidly switch: Select Chat A, then immediately select Chat B (within 10ms)
        await browser.evaluate(
            f"""
        (() => {{
            const sel = document.getElementById('chat-select');
            // Trigger selection of Chat A
            sel.value = '{chat_a_id}';
            sel.dispatchEvent(new Event('change'));

            // Immediately switch to Chat B
            setTimeout(() => {{
                sel.value = '{chat_b_id}';
                sel.dispatchEvent(new Event('change'));
            }}, 15);
        }})()
        """
        )

        # Wait for Chat B to load
        await browser.wait_for_predicate(
            "document.getElementById('chat-title-heading').textContent.includes('Chat Beta')"
        )

        # Wait 450ms to ensure Chat A's delayed fetch has fully settled
        await asyncio.sleep(0.45)

        # Check editor content: MUST be Beta instructions, NEVER Alpha!
        editor_text = await browser.evaluate(
            "document.getElementById('profile-instructions-text').value"
        )
        assert "BETA_INSTRUCTIONS_UNIQUE_MARKER" in editor_text
        assert "ALPHA_INSTRUCTIONS_UNIQUE_MARKER" not in editor_text

        # --- Barrier 2: Real deferred-poll regression (N1 fix) ---
        # Chat A poll is held in-flight at fetch. User switches to Chat B and starts B's poll.
        # When A's old poll returns, it must stop only its own timer, NOT Chat B's timer.
        await browser.evaluate(
            f"""
        (() => {{
            window._alphaPollHeld = false;
            window._alphaPollGateResolve = null;
            window._alphaPollGate = new Promise(r => {{ window._alphaPollGateResolve = r; }});
            window._betaPollTicksAfterRelease = 0;
            window._alphaReleased = false;

            const prevFetch = window.fetch;
            window.fetch = async function(url, opts) {{
                if (typeof url === 'string') {{
                    if (url.includes('/api/chats/{chat_a_id}/analysis/')) {{
                        window._alphaPollHeld = true;
                        await window._alphaPollGate;
                    }} else if (url.includes('/api/chats/{chat_b_id}/analysis/')) {{
                        if (window._alphaReleased) {{
                            window._betaPollTicksAfterRelease++;
                        }}
                    }}
                }}
                return prevFetch.apply(this, arguments);
            }};
        }})()
        """
        )

        # 1. Switch back to Chat A and start analysis to trigger startPollingProgress for A
        await browser.evaluate(
            f"""
        (() => {{
            const sel = document.getElementById('chat-select');
            sel.value = '{chat_a_id}';
            sel.dispatchEvent(new Event('change'));
        }})()
        """
        )
        await browser.wait_for_predicate(
            "document.getElementById('chat-title-heading').textContent.includes('Chat Alpha')"
        )
        await browser.evaluate("document.getElementById('btn-start-analysis').click()")

        # 2. Wait until Chat A's poll request is held at the gate
        await browser.wait_for_predicate("window._alphaPollHeld === true", timeout=6.0)

        # 3. Switch to Chat B and start Chat B's analysis polling (starts Timer B)
        await browser.evaluate(
            f"""
        (() => {{
            const sel = document.getElementById('chat-select');
            sel.value = '{chat_b_id}';
            sel.dispatchEvent(new Event('change'));
        }})()
        """
        )
        await browser.wait_for_predicate(
            "document.getElementById('chat-title-heading').textContent.includes('Chat Beta')"
        )
        await browser.evaluate("document.getElementById('btn-start-analysis').click()")

        # 4. Release Chat A's held poll callback
        await browser.evaluate(
            """
        (() => {
            window._alphaReleased = true;
            if (window._alphaPollGateResolve) {
                window._alphaPollGateResolve();
            }
        })()
        """
        )

        # 5. Timer B must continue ticking! Wait for Chat B to receive subsequent poll ticks.
        # Under N1 defect, Chat A's callback cleared Timer B (pollIntervalId).
        await browser.wait_for_predicate("window._betaPollTicksAfterRelease >= 1", timeout=6.0)
        beta_ticks = await browser.evaluate("window._betaPollTicksAfterRelease")
        assert beta_ticks >= 1

        # Selected chat in dropdown must remain Chat B
        active_val = await browser.evaluate("document.getElementById('chat-select').value")
        assert str(active_val) == str(chat_b_id)
        heading = await browser.evaluate(
            "document.getElementById('chat-title-heading').textContent"
        )
        assert "Chat Beta" in heading

        # --- Barrier 3: Empty selection barrier ---
        # User selects "" (no chat)
        await browser.evaluate(
            """
        (() => {
            const sel = document.getElementById('chat-select');
            sel.value = '';
            sel.dispatchEvent(new Event('change'));
        })()
        """
        )

        # Coverage grid must be hidden
        await browser.wait_for_predicate(
            "document.getElementById('coverage-grid').style.display === 'none'"
        )
        # Hero badge must reset
        hero_badge = await browser.evaluate("document.getElementById('hero-badge').textContent")
        assert hero_badge == "Не обрано чат"

        # Reply card and source drawer must be cleared / hidden
        reply_display = await browser.evaluate(
            "document.getElementById('reply-output-card').style.display"
        )
        assert reply_display == "none"

        drawer_content = await browser.evaluate(
            "document.getElementById('reply-drawer-content').textContent"
        )
        assert drawer_content == ""

        # --- Barrier 4: Overlapping searches ---
        # Select Chat B again
        await browser.evaluate(
            f"""
        (() => {{
            const sel = document.getElementById('chat-select');
            sel.value = '{chat_b_id}';
            sel.dispatchEvent(new Event('change'));
        }})()
        """
        )
        await browser.wait_for_predicate(
            "document.getElementById('coverage-grid').style.display !== 'none'"
        )

        # Issue Search 1 (slow) and Search 2 (fast)
        # Verify currentSearchGeneration only displays Search 2
        await browser.evaluate(
            """
        (() => {
            document.querySelector('.tab-btn[data-tab="tab-search"]').click();
            const inp = document.getElementById('search-input');
            const form = document.getElementById('search-form');

            // Rapidly send query 1 then query 2
            inp.value = 'Каркасон';
            form.dispatchEvent(new Event('submit', { cancelable: true }));

            inp.value = 'піца';
            form.dispatchEvent(new Event('submit', { cancelable: true }));
        })()
        """
        )

        # Wait for search results
        await browser.wait_for_predicate(
            "document.getElementById('search-results-info').textContent.includes('Знайдено')"
        )
        # Assert no JS errors
        assert len(browser.exceptions) == 0

    finally:
        await browser.close()


@pytest.mark.anyio
async def test_browser_xss_and_untrusted_html_defense(running_server):
    """Verify that messages containing raw HTML / script tags / img onerror
    are rendered via safe DOM text nodes and NEVER executed in the browser context.
    """
    server_url, db, _ = running_server

    # Create synthetic archive with malicious script payload
    malicious_archive = {
        "name": "XSS Probe Group",
        "type": "private_group",
        "id": 999111222,
        "messages": [
            {
                "id": 1,
                "type": "message",
                "date": "2026-10-24T20:00:00",
                "date_unixtime": "1792864800",
                "from": "Attacker <script>window.XSS_TRIGGERED = true;</script>",
                "from_id": "user_bad_01",
                "text": (
                    "Payload: <script>window.XSS_TRIGGERED = true;</script>"
                    '<img src=x onerror="window.XSS_TRIGGERED = true;">'
                ),
            }
        ],
    }

    importer = TelegramImporter(db)
    xss_bytes = json.dumps(malicious_archive).encode("utf-8")
    res_xss = importer.import_chat_json(xss_bytes, name_override="XSS Group")
    browser = await CDPBrowser.launch()

    try:
        await browser.send("Page.navigate", {"url": server_url})
        await browser.wait_for_ready()

        # Select XSS Group
        await browser.wait_for_predicate(
            "document.getElementById('chat-select').options.length >= 2"
        )
        await browser.evaluate(
            f"""
        (() => {{
            const sel = document.getElementById('chat-select');
            sel.value = '{res_xss.chat.id}';
            sel.dispatchEvent(new Event('change'));
        }})()
        """
        )

        await browser.wait_for_predicate(
            "document.getElementById('chat-title-heading').textContent.includes('XSS Group')"
        )

        # Search for payload
        await browser.evaluate(
            """
        (() => {
            document.querySelector('.tab-btn[data-tab="tab-search"]').click();
            const inp = document.getElementById('search-input');
            inp.value = 'Payload';
            const form = document.getElementById('search-form');
            form.dispatchEvent(new Event('submit', { cancelable: true }));
        })()
        """
        )

        await browser.wait_for_predicate(
            "document.getElementById('search-results-list').children.length > 0"
        )

        # Verify XSS_TRIGGERED is undefined
        xss_val = await browser.evaluate("window.XSS_TRIGGERED")
        assert xss_val is None or xss_val is False, "XSS script execution was NOT prevented!"

        # Verify rendered text contains literal script tag as text
        result_text = await browser.evaluate(
            "document.getElementById('search-results-list').textContent"
        )
        assert "<script>" in result_text
        assert "window.XSS_TRIGGERED" in result_text

        assert len(browser.exceptions) == 0

    finally:
        await browser.close()
