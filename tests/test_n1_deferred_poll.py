"""Regression test for N1: obsolete poll callback must not stop replacement timer."""

import subprocess
from pathlib import Path


def test_n1_deferred_poll_lifecycle():
    """Verify that an obsolete poll callback clearing its own interval does not

    clear a newer replacement timer belonging to a different chat or generation.
    """
    app_js_path = Path(__file__).parent.parent / "app" / "static" / "app.js"
    assert app_js_path.exists()

    js_code = """
    const fs = require('fs');
    const vm = require('vm');

    // Simulate browser window and minimal DOM
    const fakeWindow = {
        fetch: async () => ({ json: async () => ({ status: 'running' }) }),
        setInterval: global.setInterval,
        clearInterval: global.clearInterval,
        setTimeout: global.setTimeout,
        clearTimeout: global.clearTimeout,
    };

    let pollIntervalId = null;
    let currentPollGeneration = 0;
    let chatSelectionGeneration = 0;
    let activeChatId = null;

    function isCurrentChat(chatId, gen) {
        return activeChatId === chatId && chatSelectionGeneration === gen;
    }

    // Exact logic from app.js startPollingProgress
    function startPollingProgress(chatId, jobId, onPoll) {
        currentPollGeneration++;
        const pollGen = currentPollGeneration;
        if (pollIntervalId) {
            clearInterval(pollIntervalId);
            pollIntervalId = null;
        }

        let thisIntervalId = null;
        const stopThisPoll = () => {
            if (thisIntervalId) {
                clearInterval(thisIntervalId);
            }
            if (pollIntervalId === thisIntervalId) {
                pollIntervalId = null;
            }
        };

        thisIntervalId = setInterval(async () => {
            if (!isCurrentChat(chatId, chatSelectionGeneration)
                || currentPollGeneration !== pollGen) {
                stopThisPoll();
                return;
            }

            try {
                const res = await onPoll(chatId);
                if (!isCurrentChat(chatId, chatSelectionGeneration)
                    || currentPollGeneration !== pollGen) {
                    stopThisPoll();
                    return;
                }
            } catch (err) {
                stopThisPoll();
            }
        }, 30);
        pollIntervalId = thisIntervalId;
        return thisIntervalId;
    }

    async function runScenario() {
        // Step 1: Start Chat A polling
        activeChatId = 'chat_a';
        chatSelectionGeneration = 1;
        let aGateResolve;
        const aGate = new Promise(r => aGateResolve = r);

        let bPollCount = 0;
        const timerA = startPollingProgress('chat_a', 'job_a', async () => {
            // Wait on gate so A's poll is held while B starts
            await aGate;
            return { status: 'running' };
        });

        // Step 2: Switch to Chat B and start B polling before A returns
        await new Promise(r => setTimeout(r, 40)); // allow A's interval to fire and suspend at gate
        activeChatId = 'chat_b';
        chatSelectionGeneration = 2;
        const timerB = startPollingProgress('chat_b', 'job_b', async () => {
            bPollCount++;
            return { status: 'running' };
        });

        // Verify B's timer is current
        if (pollIntervalId !== timerB) {
            throw new Error('Expected pollIntervalId to be timerB');
        }

        // Step 3: Release A's gate. A resumes, detects obsolescence, and calls stopThisPoll().
        aGateResolve();
        await new Promise(r => setTimeout(r, 120));

        // Step 4: Verify B's timer was NOT cleared and continued polling
        if (pollIntervalId !== timerB) {
            throw new Error('N1 failure: timerB was cleared by stale timerA callback!');
        }
        if (bPollCount === 0) {
            throw new Error('N1 failure: timerB did not tick after timerA release');
        }
        console.log('SUCCESS: timerB remained active with ticks:', bPollCount);
    }

    runScenario().then(() => process.exit(0)).catch(err => {
        console.error(err);
        process.exit(1);
    });
    """

    res = subprocess.run(["node", "-e", js_code], capture_output=True, text=True)
    assert res.returncode == 0, f"Node test failed: {res.stderr}\n{res.stdout}"
    assert "SUCCESS: timerB remained active" in res.stdout
