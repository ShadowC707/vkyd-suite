/**
 * Vkyd — Attributable quote memory & group humor profile engine
 * Frontend Client Application
 * Strict safe DOM rendering: No raw innerHTML for untrusted data or model outputs.
 */

(function () {
  'use strict';

  // --- State & Isolation Barriers ---
  let activeChatId = null;
  let activeChatDetail = null;
  let activeProfile = null;
  let activeJobId = null;
  let pollIntervalId = null;
  let currentAbortController = null;
  let pendingSuggestion = null;
  let lastReplyData = null;

  // Generation barriers for chat state isolation (S3)
  let chatSelectionGeneration = 0;
  let currentSearchGeneration = 0;
  let currentPollGeneration = 0;

  // Search pagination state
  let searchPage = 0;
  const searchLimit = 20;
  let currentSearchQuery = '';

  // --- DOM Elements ---
  const chatSelect = document.getElementById('chat-select');
  const btnOpenImport = document.getElementById('btn-open-import');
  const btnCloseImport = document.getElementById('btn-close-import');
  const btnCancelImport = document.getElementById('btn-cancel-import');
  const btnFinishImport = document.getElementById('btn-finish-import');
  const importDialog = document.getElementById('import-dialog');
  const importForm = document.getElementById('import-form');
  const importFileInput = document.getElementById('import-file-input');
  const dropZone = document.getElementById('drop-zone');
  const importResultView = document.getElementById('import-result-view');
  const importStatsSummary = document.getElementById('import-stats-summary');

  const heroBadge = document.getElementById('hero-badge');
  const chatTitleHeading = document.getElementById('chat-title-heading');
  const chatMetaSubtitle = document.getElementById('chat-meta-subtitle');
  const btnExportProfileLink = document.getElementById('btn-export-profile-link');
  const coverageGrid = document.getElementById('coverage-grid');
  const statTotalMessages = document.getElementById('stat-total-messages');
  const statSupportedMessages = document.getElementById('stat-supported-messages');
  const statAnalyzedCoverage = document.getElementById('stat-analyzed-coverage');
  const statAnalyzedCount = document.getElementById('stat-analyzed-count');
  const statCorpusVersion = document.getElementById('stat-corpus-version');

  const tabButtons = document.querySelectorAll('.tab-btn');
  const tabContents = document.querySelectorAll('.tab-content');

  // Search
  const searchForm = document.getElementById('search-form');
  const searchInput = document.getElementById('search-input');
  const btnClearSearch = document.getElementById('btn-clear-search');
  const searchResultsInfo = document.getElementById('search-results-info');
  const searchResultsList = document.getElementById('search-results-list');
  const searchPagination = document.getElementById('search-pagination');
  const btnPrevPage = document.getElementById('btn-prev-page');
  const btnNextPage = document.getElementById('btn-next-page');
  const paginationIndicator = document.getElementById('pagination-indicator');

  // Analysis
  const estMessages = document.getElementById('est-messages');
  const estChunks = document.getElementById('est-chunks');
  const estMerges = document.getElementById('est-merges');
  const estTotalCalls = document.getElementById('est-total-calls');
  const estNote = document.getElementById('est-note');
  const inputMaxCalls = document.getElementById('input-max-calls');
  const btnStartAnalysis = document.getElementById('btn-start-analysis');
  const btnCancelAnalysis = document.getElementById('btn-cancel-analysis');
  const btnResumeAnalysis = document.getElementById('btn-resume-analysis');
  const analysisProgressCard = document.getElementById('analysis-progress-card');
  const analysisStatusBadge = document.getElementById('analysis-status-badge');
  const analysisProgressFill = document.getElementById('analysis-progress-fill');
  const progMessages = document.getElementById('prog-messages');
  const progChunks = document.getElementById('prog-chunks');
  const progCalls = document.getElementById('prog-calls');
  const analysisErrorBanner = document.getElementById('analysis-error-banner');
  const analysisErrorTitle = document.getElementById('analysis-error-title');
  const analysisErrorMessage = document.getElementById('analysis-error-message');
  const analysisErrorAction = document.getElementById('analysis-error-action');

  // Profile
  const suggestionBanner = document.getElementById('suggestion-banner');
  const suggVersionTag = document.getElementById('sugg-version-tag');
  const btnInspectSuggestion = document.getElementById('btn-inspect-suggestion');
  const btnApplySuggestion = document.getElementById('btn-apply-suggestion');
  const profileVersionBadge = document.getElementById('profile-version-badge');
  const profileInstructionsText = document.getElementById('profile-instructions-text');
  const profileEditorForm = document.getElementById('profile-editor-form');
  const btnSaveProfile = document.getElementById('btn-save-profile');
  const profileSaveStatus = document.getElementById('profile-save-status');
  const versionConflictAlert = document.getElementById('version-conflict-alert');
  const versionConflictMsg = document.getElementById('version-conflict-msg');
  const btnReloadProfile = document.getElementById('btn-reload-profile');
  const memesContainer = document.getElementById('memes-container');
  const profileStructuredBox = document.getElementById('profile-structured-box');
  const profileStructuredContent = document.getElementById('profile-structured-content');

  // Suggestion Modal
  const suggestionDialog = document.getElementById('suggestion-dialog');
  const btnCloseSuggestionModal = document.getElementById('btn-close-suggestion-modal');
  const btnModalCancelSugg = document.getElementById('btn-modal-cancel-sugg');
  const btnModalApplySugg = document.getElementById('btn-modal-apply-sugg');
  const modalActiveInstructions = document.getElementById('modal-active-instructions');
  const modalSuggestedInstructions = document.getElementById('modal-suggested-instructions');
  const modalSuggestedMemes = document.getElementById('modal-suggested-memes');

  // Playground
  const replyForm = document.getElementById('reply-form');
  const replyModeSelect = document.getElementById('reply-mode-select');
  const replyContextInput = document.getElementById('reply-context-input');
  const contextCharCount = document.getElementById('context-char-count');
  const replyOutputCard = document.getElementById('reply-output-card');
  const replyActionBadge = document.getElementById('reply-action-badge');
  const replyBodyText = document.getElementById('reply-body-text');
  const replyAttribution = document.getElementById('reply-attribution');
  const replySourceDrawer = document.getElementById('reply-source-drawer');
  const replyDrawerContent = document.getElementById('reply-drawer-content');
  const feedbackButtons = document.querySelectorAll('.btn-feedback');
  const feedbackAcknowledgement = document.getElementById('feedback-acknowledgement');

  // Telegram
  const tgTokenStatus = document.getElementById('tg-token-status');
  const tgAllowedGroups = document.getElementById('tg-allowed-groups');
  const tgBindingForm = document.getElementById('telegram-binding-form');
  const tgInputChatId = document.getElementById('tg-input-chat-id');
  const tgInputCooldown = document.getElementById('tg-input-cooldown');
  const tgInputEnabled = document.getElementById('tg-input-enabled');
  const tgBindStatus = document.getElementById('tg-bind-status');
  const tgBindingsTbody = document.getElementById('tg-bindings-tbody');

  // --- Safe Text Node Utilities ---
  function safeText(str) {
    return document.createTextNode(str == null ? '' : String(str));
  }

  function clearElement(el) {
    if (!el) return;
    while (el.firstChild) {
      el.removeChild(el.firstChild);
    }
  }

  function setElementText(el, text) {
    if (!el) return;
    clearElement(el);
    el.appendChild(safeText(text));
  }

  function formatTimestamp(isoStr) {
    if (!isoStr) return '—';
    try {
      const d = new Date(isoStr);
      if (isNaN(d.getTime())) return String(isoStr);
      return d.toLocaleString('uk-UA', {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch {
      return String(isoStr);
    }
  }

  function isCurrentChat(chatId, gen) {
    return activeChatId === chatId && chatSelectionGeneration === gen;
  }

  // --- Tab Navigation ---
  function initTabs() {
    tabButtons.forEach((btn) => {
      btn.addEventListener('click', () => {
        const targetTab = btn.getAttribute('data-tab');
        tabButtons.forEach((b) => b.classList.toggle('active', b === btn));
        tabContents.forEach((c) => {
          c.classList.toggle('active', c.id === targetTab);
        });
      });
    });
  }

  // --- Fetch Helper with Abort Support ---
  async function apiFetch(url, options = {}) {
    const signal = currentAbortController ? currentAbortController.signal : undefined;
    const res = await fetch(url, { ...options, signal });
    if (!res.ok) {
      let errData = {};
      try {
        errData = await res.json();
      } catch {
        // ignore json parse error
      }
      const err = new Error(
        (errData.error && errData.error.message) ||
          errData.detail ||
          `HTTP ${res.status}: ${res.statusText}`
      );
      err.code = (errData.error && errData.error.code) || 'http_error';
      err.status = res.status;
      err.details = errData;
      throw err;
    }
    return res;
  }

  // --- Chat Selection & Loading ---
  async function loadChatList(preselectChatId = null) {
    try {
      const res = await apiFetch('/api/chats?limit=100');
      const data = await res.json();

      clearElement(chatSelect);
      if (!data.items || data.items.length === 0) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.appendChild(safeText('Немає імпортованих чатів'));
        chatSelect.appendChild(opt);
        resetChatView();
        return;
      }

      const defaultOpt = document.createElement('option');
      defaultOpt.value = '';
      defaultOpt.appendChild(safeText('— Оберіть чат із списку —'));
      chatSelect.appendChild(defaultOpt);

      data.items.forEach((c) => {
        const opt = document.createElement('option');
        opt.value = c.id;
        opt.appendChild(safeText(`${c.name} (${c.total_messages} пов.)`));
        chatSelect.appendChild(opt);
      });

      if (preselectChatId) {
        chatSelect.value = preselectChatId;
        await selectChat(preselectChatId);
      } else if (activeChatId && data.items.some((c) => c.id === activeChatId)) {
        chatSelect.value = activeChatId;
      } else if (data.items.length === 1) {
        chatSelect.value = data.items[0].id;
        await selectChat(data.items[0].id);
      }
    } catch (err) {
      if (err.name === 'AbortError') return;
      console.error('Failed to load chat list:', err);
      clearElement(chatSelect);
      const opt = document.createElement('option');
      opt.value = '';
      opt.appendChild(safeText('Помилка завантаження чатів'));
      chatSelect.appendChild(opt);
    }
  }

  function resetChatView() {
    activeChatId = null;
    activeChatDetail = null;
    activeProfile = null;
    pendingSuggestion = null;
    activeJobId = null;
    lastReplyData = null;
    currentPollGeneration++;
    if (pollIntervalId) {
      clearInterval(pollIntervalId);
      pollIntervalId = null;
    }

    // Close modal dialogs if open
    if (suggestionDialog && suggestionDialog.open) {
      suggestionDialog.close();
    }

    setElementText(heroBadge, 'Не обрано чат');
    setElementText(chatTitleHeading, 'Оберіть або імпортуйте архів чату');
    setElementText(
      chatMetaSubtitle,
      'Імпортуйте експорт Telegram у форматі JSON для початку роботи.'
    );
    coverageGrid.style.display = 'none';
    btnExportProfileLink.style.display = 'none';

    // Clear Search
    clearElement(searchResultsList);
    clearElement(searchResultsInfo);
    searchPagination.style.display = 'none';

    // Reset Analysis
    setElementText(estMessages, '—');
    setElementText(estChunks, '—');
    setElementText(estMerges, '—');
    setElementText(estTotalCalls, '—');
    analysisProgressCard.style.display = 'none';
    btnStartAnalysis.style.display = 'inline-block';
    btnCancelAnalysis.style.display = 'none';
    btnResumeAnalysis.style.display = 'none';
    analysisErrorBanner.style.display = 'none';

    // Reset Profile
    suggestionBanner.style.display = 'none';
    setElementText(profileVersionBadge, 'Версія: —');
    profileInstructionsText.value = '';
    clearElement(profileSaveStatus);
    btnSaveProfile.disabled = false;
    versionConflictAlert.style.display = 'none';
    clearElement(memesContainer);
    if (profileStructuredBox) {
      profileStructuredBox.style.display = 'none';
    }
    if (profileStructuredContent) {
      clearElement(profileStructuredContent);
    }
    const emptyMemes = document.createElement('p');
    emptyMemes.className = 'empty-state';
    emptyMemes.appendChild(safeText('Меми ще не виявлені або профіль не сформовано.'));
    memesContainer.appendChild(emptyMemes);

    // Reset Playground
    replyContextInput.value = '';
    setElementText(contextCharCount, '0');
    replyOutputCard.style.display = 'none';
    clearElement(replyAttribution);
    clearElement(replyDrawerContent);
    replySourceDrawer.removeAttribute('open');
    setElementText(feedbackAcknowledgement, '');
    feedbackButtons.forEach((b) => (b.disabled = true));
  }

  async function selectChat(chatId) {
    if (currentAbortController) {
      currentAbortController.abort();
    }
    currentAbortController = new AbortController();

    chatSelectionGeneration++;
    const thisGen = chatSelectionGeneration;
    const thisChatId = chatId || null;

    resetChatView();

    if (!thisChatId) {
      return;
    }

    activeChatId = thisChatId;

    setElementText(heroBadge, 'Завантаження...');
    setElementText(chatTitleHeading, 'Завантаження даних чату...');

    try {
      const res = await apiFetch(`/api/chats/${thisChatId}`);
      if (!isCurrentChat(thisChatId, thisGen)) return;
      const chat = await res.json();
      if (!isCurrentChat(thisChatId, thisGen)) return;
      activeChatDetail = chat;

      // Update Hero
      setElementText(heroBadge, chat.corpus_version ? `v${chat.corpus_version}` : 'v1');
      setElementText(chatTitleHeading, chat.name || 'Без назви');
      setElementText(
        chatMetaSubtitle,
        `ID: ${chat.id} | Telegram Source: ${chat.source_chat_id || '—'} | Імпортовано: ${formatTimestamp(chat.created_at)}`
      );

      coverageGrid.style.display = 'grid';
      setElementText(statTotalMessages, (chat.total_messages ?? 0).toLocaleString('uk-UA'));
      setElementText(statSupportedMessages, (chat.supported_text_messages ?? 0).toLocaleString('uk-UA'));

      const covPct = chat.coverage ? chat.coverage.ai_analysis_coverage_percent : 0;
      setElementText(statAnalyzedCoverage, `${covPct}%`);
      const analyzedCount = chat.analyzed_messages || 0;
      setElementText(statAnalyzedCount, `${analyzedCount} повідомлень`);
      setElementText(statCorpusVersion, chat.corpus_version ? `v${chat.corpus_version}` : 'v1');

      // Update instructions export button
      btnExportProfileLink.href = `/api/chats/${thisChatId}/profile/export`;
      btnExportProfileLink.setAttribute('download', `profile_${thisChatId}.md`);
      btnExportProfileLink.style.display = 'inline-flex';

      // Restore latest job progress state if available
      if (chat.latest_job) {
        activeJobId = chat.latest_job.id;
        renderJobProgress(chat.latest_job);
        if (chat.latest_job.status === 'running') {
          startPollingProgress(thisChatId, chat.latest_job.id);
        }
      }

      // Load Estimates, Profile & Suggestions concurrently (without starting paid analysis)
      await Promise.allSettled([
        loadAnalysisEstimate(thisChatId, thisGen),
        loadProfile(thisChatId, thisGen),
        checkProfileSuggestion(thisChatId, thisGen),
        loadMemories(thisChatId, '', 0, thisGen),
      ]);
    } catch (err) {
      if (!isCurrentChat(thisChatId, thisGen)) return;
      if (err.name === 'AbortError') return;
      console.error('Error selecting chat:', err);
      setElementText(heroBadge, 'Помилка');
      setElementText(chatTitleHeading, 'Не вдалося завантажити чат');
      setElementText(chatMetaSubtitle, err.message);
    }
  }

  async function refreshActiveChatInPlace(chatId) {
    const thisGen = chatSelectionGeneration;
    try {
      const res = await apiFetch(`/api/chats/${chatId}`);
      if (!isCurrentChat(chatId, thisGen)) return;
      const chat = await res.json();
      if (!isCurrentChat(chatId, thisGen)) return;
      activeChatDetail = chat;

      const covPct = chat.coverage ? chat.coverage.ai_analysis_coverage_percent : 0;
      setElementText(statAnalyzedCoverage, `${covPct}%`);
      const analyzedCount = chat.analyzed_messages || 0;
      setElementText(statAnalyzedCount, `${analyzedCount} повідомлень`);

      await Promise.allSettled([
        loadProfile(chatId, thisGen),
        checkProfileSuggestion(chatId, thisGen),
      ]);
    } catch {
      // Ignore background refresh errors
    }
  }

  // --- Search Memories ---
  async function loadMemories(chatId, query, page = 0, chatGen = null) {
    if (!chatId) return;
    const targetGen = chatGen !== null ? chatGen : chatSelectionGeneration;
    if (!isCurrentChat(chatId, targetGen)) return;

    currentSearchGeneration++;
    const thisSearchGen = currentSearchGeneration;
    currentSearchQuery = query || '';
    searchPage = page;
    const offset = page * searchLimit;

    setElementText(searchResultsInfo, 'Пошук цитат...');
    clearElement(searchResultsList);

    try {
      const url = `/api/chats/${chatId}/memories?q=${encodeURIComponent(currentSearchQuery)}&limit=${searchLimit}&offset=${offset}`;
      const res = await apiFetch(url);
      if (!isCurrentChat(chatId, targetGen) || currentSearchGeneration !== thisSearchGen) return;
      const data = await res.json();
      if (!isCurrentChat(chatId, targetGen) || currentSearchGeneration !== thisSearchGen) return;

      clearElement(searchResultsList);

      if (!data.items || data.items.length === 0) {
        setElementText(
          searchResultsInfo,
          currentSearchQuery ? `Нічого не знайдено за запитом "${currentSearchQuery}".` : 'У цьому чаті поки немає текстових повідомлень.'
        );
        searchPagination.style.display = 'none';
        return;
      }

      const total = data.total || 0;
      const startIdx = offset + 1;
      const endIdx = Math.min(offset + data.items.length, total);
      setElementText(
        searchResultsInfo,
        `Знайдено ${total} цитат (показано ${startIdx}–${endIdx}):`
      );

      data.items.forEach((m) => {
        const card = createQuoteCard(m);
        searchResultsList.appendChild(card);
      });

      // Pagination Controls
      if (total > searchLimit) {
        searchPagination.style.display = 'flex';
        setElementText(
          paginationIndicator,
          `Сторінка ${page + 1} з ${Math.ceil(total / searchLimit)}`
        );
        btnPrevPage.disabled = page === 0;
        btnNextPage.disabled = endIdx >= total;
      } else {
        searchPagination.style.display = 'none';
      }
    } catch (err) {
      if (!isCurrentChat(chatId, targetGen) || currentSearchGeneration !== thisSearchGen) return;
      if (err.name === 'AbortError') return;
      setElementText(searchResultsInfo, `Помилка пошуку: ${err.message}`);
      searchPagination.style.display = 'none';
    }
  }

  function createQuoteCard(m) {
    const card = document.createElement('article');
    card.className = 'quote-card';

    const header = document.createElement('div');
    header.className = 'quote-meta';

    const authorSpan = document.createElement('span');
    authorSpan.className = 'quote-author';
    authorSpan.appendChild(safeText(m.sender_name || 'Невідомий'));

    const dateSpan = document.createElement('span');
    dateSpan.className = 'quote-date';
    dateSpan.appendChild(safeText(formatTimestamp(m.sent_at || m.raw_date)));

    header.appendChild(authorSpan);
    header.appendChild(dateSpan);

    const body = document.createElement('blockquote');
    body.className = 'quote-text';
    body.appendChild(safeText(m.text || ''));

    const footer = document.createElement('div');
    footer.className = 'quote-actions';

    const idSpan = document.createElement('span');
    idSpan.className = 'quote-msg-id';
    idSpan.appendChild(safeText(`№ ${m.source_message_id || m.id}`));
    footer.appendChild(idSpan);

    const btnUseContext = document.createElement('button');
    btnUseContext.className = 'btn-link';
    btnUseContext.type = 'button';
    btnUseContext.appendChild(safeText('Вставити як контекст →'));
    btnUseContext.addEventListener('click', () => {
      // Switch to playground tab and insert text
      const targetTabBtn = document.querySelector('[data-tab="tab-playground"]');
      if (targetTabBtn) targetTabBtn.click();
      replyContextInput.value = m.text || '';
      setElementText(contextCharCount, String(replyContextInput.value.length));
      replyContextInput.focus();
    });
    footer.appendChild(btnUseContext);

    const btnCopy = document.createElement('button');
    btnCopy.className = 'btn-link';
    btnCopy.type = 'button';
    btnCopy.appendChild(safeText('Копіювати'));
    btnCopy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(m.text || '');
        setElementText(btnCopy, 'Скопійовано! ✓');
        setTimeout(() => setElementText(btnCopy, 'Копіювати'), 2000);
      } catch {
        // Fallback or ignore
      }
    });
    footer.appendChild(btnCopy);

    card.appendChild(header);
    card.appendChild(body);
    card.appendChild(footer);
    return card;
  }

  // --- Analysis & Call Budgeting ---
  async function loadAnalysisEstimate(chatId, chatGen = null) {
    const targetGen = chatGen !== null ? chatGen : chatSelectionGeneration;
    try {
      const res = await apiFetch(`/api/chats/${chatId}/analysis/estimate`, { method: 'POST' });
      if (!isCurrentChat(chatId, targetGen)) return;
      const est = await res.json();
      if (!isCurrentChat(chatId, targetGen)) return;

      setElementText(estMessages, (est.total_supported_messages || 0).toLocaleString('uk-UA'));
      setElementText(estChunks, (est.estimated_chunks || 0).toLocaleString('uk-UA'));
      setElementText(estMerges, (est.estimated_merge_calls || 0).toLocaleString('uk-UA'));
      setElementText(estTotalCalls, (est.estimated_total_calls || 0).toLocaleString('uk-UA'));
      setElementText(
        estNote,
        est.merge_estimate_note || 'Оцінка злиття є базовою нижньою межею за ступенем розгалуження.'
      );
    } catch (err) {
      if (!isCurrentChat(chatId, targetGen)) return;
      if (err.name === 'AbortError') return;
      console.warn('Could not fetch analysis estimate:', err);
    }
  }

  async function startAnalysis() {
    if (!activeChatId) return;
    const targetChatId = activeChatId;
    const targetGen = chatSelectionGeneration;
    const maxCallsVal = parseInt(inputMaxCalls.value, 10) || 50;

    analysisErrorBanner.style.display = 'none';
    analysisProgressCard.style.display = 'block';
    setElementText(analysisStatusBadge, 'Ініціалізація...');
    analysisStatusBadge.className = 'badge';

    btnStartAnalysis.style.display = 'none';
    btnCancelAnalysis.style.display = 'inline-block';
    btnResumeAnalysis.style.display = 'none';

    try {
      const res = await apiFetch(`/api/chats/${targetChatId}/analysis`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ max_calls: maxCallsVal }),
      });
      if (!isCurrentChat(targetChatId, targetGen)) return;
      const job = await res.json();
      if (!isCurrentChat(targetChatId, targetGen)) return;

      activeJobId = job.id;
      renderJobProgress(job);
      startPollingProgress(targetChatId, job.id);
    } catch (err) {
      if (!isCurrentChat(targetChatId, targetGen)) return;
      handleAnalysisError(err);
    }
  }

  async function cancelAnalysis() {
    if (!activeChatId || !activeJobId) return;
    const targetChatId = activeChatId;
    const targetGen = chatSelectionGeneration;
    const targetJobId = activeJobId;

    try {
      const res = await apiFetch(`/api/chats/${targetChatId}/analysis/${targetJobId}/cancel`, {
        method: 'POST',
      });
      if (!isCurrentChat(targetChatId, targetGen)) return;
      const job = await res.json();
      if (!isCurrentChat(targetChatId, targetGen)) return;

      renderJobProgress(job);
      if (pollIntervalId) {
        clearInterval(pollIntervalId);
        pollIntervalId = null;
      }
    } catch (err) {
      if (!isCurrentChat(targetChatId, targetGen)) return;
      console.error('Cancel analysis error:', err);
    }
  }

  function startPollingProgress(chatId, jobId) {
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
      if (!isCurrentChat(chatId, chatSelectionGeneration) || currentPollGeneration !== pollGen) {
        stopThisPoll();
        return;
      }

      try {
        const res = await apiFetch(`/api/chats/${chatId}/analysis/${jobId}`);
        if (!isCurrentChat(chatId, chatSelectionGeneration) || currentPollGeneration !== pollGen) {
          stopThisPoll();
          return;
        }
        const job = await res.json();
        if (!isCurrentChat(chatId, chatSelectionGeneration) || currentPollGeneration !== pollGen) {
          stopThisPoll();
          return;
        }

        renderJobProgress(job);

        const isTerminalOrPaused =
          job.status === 'completed' ||
          job.status === 'paused_budget' ||
          job.status === 'paused_cancelled' ||
          job.status === 'failed';

        if (isTerminalOrPaused) {
          stopThisPoll();

          if (job.status === 'completed') {
            // Only refresh in-place if user is STILL on this chat
            if (isCurrentChat(chatId, chatSelectionGeneration) && currentPollGeneration === pollGen) {
              await refreshActiveChatInPlace(chatId);
            }
          }
        }
      } catch (err) {
        if (!isCurrentChat(chatId, chatSelectionGeneration) || currentPollGeneration !== pollGen) {
          stopThisPoll();
          return;
        }
        if (err.name === 'AbortError') {
          stopThisPoll();
          return;
        }
        console.error('Progress polling error:', err);
      }
    }, 1000);
    pollIntervalId = thisIntervalId;
  }

  function renderJobProgress(job) {
    analysisProgressCard.style.display = 'block';

    const statusMap = {
      pending: { label: 'ОЧІКУВАННЯ', cls: 'badge-secondary' },
      running: { label: 'ВИКОНУЄТЬСЯ', cls: 'badge-accent' },
      completed: { label: 'ЗАВЕРШЕНО', cls: 'badge-success' },
      paused_budget: { label: 'ПАУЗА (БЮДЖЕТ)', cls: 'badge-warning' },
      paused_cancelled: { label: 'СКАСОВАНО', cls: 'badge-warning' },
      failed: { label: 'ПОМИЛКА', cls: 'badge-danger' },
    };

    const info = statusMap[job.status] || {
      label: (job.status || 'STATUS').toUpperCase(),
      cls: 'badge',
    };
    setElementText(analysisStatusBadge, info.label);
    analysisStatusBadge.className = `badge ${info.cls}`;

    // Progress bar percent
    const pct =
      job.total_chunks > 0
        ? Math.round(((job.completed_chunks ?? 0) / job.total_chunks) * 100)
        : 0;
    analysisProgressFill.style.width = `${Math.min(pct, 100)}%`;

    setElementText(
      progMessages,
      `${job.processed_unique_messages} / ${activeChatDetail ? activeChatDetail.supported_text_messages : '—'}`
    );
    setElementText(progChunks, `${job.completed_chunks ?? 0} / ${job.total_chunks}`);
    setElementText(progCalls, `${job.provider_calls} / ${job.max_calls}`);

    if (job.status === 'running') {
      btnStartAnalysis.style.display = 'none';
      btnCancelAnalysis.style.display = 'inline-block';
      btnResumeAnalysis.style.display = 'none';
    } else if (job.can_resume) {
      btnStartAnalysis.style.display = 'none';
      btnCancelAnalysis.style.display = 'none';
      btnResumeAnalysis.style.display = 'inline-block';
    } else {
      btnStartAnalysis.style.display = 'inline-block';
      btnCancelAnalysis.style.display = 'none';
      btnResumeAnalysis.style.display = 'none';
    }

    if (
      job.last_error_code ||
      job.status === 'failed' ||
      job.status === 'paused_budget' ||
      job.status === 'paused_cancelled'
    ) {
      analysisErrorBanner.style.display = 'block';

      if (job.status === 'paused_budget') {
        setElementText(analysisErrorTitle, 'Бюджет вичерпано');
        setElementText(
          analysisErrorMessage,
          'Досягнуто встановленого ліміту викликів моделі. Вкажіть більше число у полі «Ліміт викликів» вище та натисніть «Продовжити аналіз».'
        );
      } else if (job.status === 'paused_cancelled') {
        setElementText(analysisErrorTitle, 'Аналіз зупинено');
        setElementText(
          analysisErrorMessage,
          job.can_resume
            ? 'Аналіз скасовано. Усі готові фрагменти збережено в базі; ви можете продовжити роботу кнопкою «Продовжити аналіз».'
            : 'Аналіз було скасовано.'
        );
      } else {
        setElementText(analysisErrorTitle, 'Помилка виконання аналізу');
        const codeMessages = {
          recovery_required:
            'Виявлено відсутні або несумісні фрагменти повідомлень. Необхідно перезапустити аналіз корпусу.',
          envelope_exceeded: 'Окремий фрагмент перевищує ліміт розміру контексту.',
          merge_envelope_exceeded: 'Загальний розмір проміжного злиття перевищив ліміт конверта.',
          interrupted_restart: 'Аналіз було перервано перезапуском сервера. Роботу можна продовжити.',
          credential_missing: 'Ключ AI-провайдера (GEMINI_API_KEY) не налаштовано.',
          provider_error: 'Помилка взаємодії з AI-провайдером.',
          internal_error: 'Внутрішня помилка обробки даних.',
        };
        setElementText(
          analysisErrorMessage,
          (job.last_error_code && codeMessages[job.last_error_code]) ||
            (job.last_error_code
              ? `Помилка [${job.last_error_code}]`
              : 'Сталася невідома помилка під час аналізу.')
        );
      }

      clearElement(analysisErrorAction);
      if (job.last_error_code === 'recovery_required') {
        const hint = document.createElement('span');
        hint.appendChild(
          safeText(
            '⚠️ Запустіть повторний аналіз для повного безпечного оновлення корпусу повідомлень.'
          )
        );
        analysisErrorAction.appendChild(hint);
      } else if (job.can_resume) {
        const hint = document.createElement('span');
        hint.appendChild(
          safeText(
            '💡 Щоб продовжити виконання з місця зупинки, скористайтеся кнопкою «Продовжити аналіз».'
          )
        );
        analysisErrorAction.appendChild(hint);
      }
    } else {
      analysisErrorBanner.style.display = 'none';
    }
  }

  function handleAnalysisError(err) {
    btnStartAnalysis.style.display = 'inline-block';
    btnCancelAnalysis.style.display = 'none';
    btnResumeAnalysis.style.display = 'none';
    analysisProgressCard.style.display = 'block';
    analysisErrorBanner.style.display = 'block';

    setElementText(analysisStatusBadge, 'ПОМИЛКА');
    analysisStatusBadge.className = 'badge badge-danger';

    if (err.code === 'credential_missing') {
      setElementText(analysisErrorTitle, 'AI-провайдер не налаштовано');
      setElementText(
        analysisErrorMessage,
        'Для запуску аналізу необхідний ключ Gemini API. Додайте GEMINI_API_KEY до змінних середовища.'
      );
    } else {
      setElementText(analysisErrorTitle, 'Не вдалося запустити аналіз');
      setElementText(analysisErrorMessage, err.message);
    }
    clearElement(analysisErrorAction);
  }

  // --- Profile Editor & Memes ---
  async function loadProfile(chatId, chatGen = null) {
    const targetGen = chatGen !== null ? chatGen : chatSelectionGeneration;
    versionConflictAlert.style.display = 'none';
    clearElement(profileSaveStatus);

    try {
      const res = await apiFetch(`/api/chats/${chatId}/profile`);
      if (!isCurrentChat(chatId, targetGen)) return;
      const profile = await res.json();
      if (!isCurrentChat(chatId, targetGen)) return;

      activeProfile = profile;

      setElementText(profileVersionBadge, `Версія: v${profile.version}`);
      profileInstructionsText.value = profile.instructions_md || '';

      renderStructuredProfile(profile.structured || {});
      renderMemes(profile.memes || []);
    } catch (err) {
      if (!isCurrentChat(chatId, targetGen)) return;
      if (err.name === 'AbortError') return;
      if (err.status === 404) {
        setElementText(profileVersionBadge, 'Версія: v0 (не згенеровано)');
        profileInstructionsText.value =
          '# Інструкції стилю\nПрофіль гумору ще не згенеровано. Запустіть аналіз чату у вкладці "Аналіз та бюджет".';
        if (profileStructuredBox) {
          profileStructuredBox.style.display = 'none';
        }
        clearElement(memesContainer);
        const p = document.createElement('p');
        p.className = 'empty-state';
        p.appendChild(safeText('Профіль ще не створено. Запустіть аналіз корпусу.'));
        memesContainer.appendChild(p);
      } else {
        console.error('Failed to load profile:', err);
      }
    }
  }

  function renderStructuredProfile(structured) {
    if (!profileStructuredBox || !profileStructuredContent) return;
    clearElement(profileStructuredContent);

    if (!structured || typeof structured !== 'object' || Object.keys(structured).length === 0) {
      profileStructuredBox.style.display = 'none';
      return;
    }

    const labels = {
      tone_and_register: 'Тон та мовний регістр',
      languages: 'Мови та суржик / діалекти',
      formality: 'Рівень формальності',
      pacing: 'Темп та довжина реплік',
      humor_style: 'Стиль гумору та сарказм',
      inside_references: 'Внутрішні відсилання',
      reaction_triggers: 'Тригери реакцій',
    };

    let count = 0;
    for (const [key, val] of Object.entries(structured)) {
      if (!val) continue;
      count++;
      const item = document.createElement('div');
      item.className = 'structured-item-card';

      const labelEl = document.createElement('span');
      labelEl.className = 'structured-label';
      labelEl.appendChild(safeText(labels[key] || key.replace(/_/g, ' ')));
      item.appendChild(labelEl);

      const valEl = document.createElement('div');
      valEl.className = 'structured-value';
      if (Array.isArray(val)) {
        valEl.appendChild(safeText(val.join(', ')));
      } else if (typeof val === 'object') {
        valEl.appendChild(safeText(JSON.stringify(val)));
      } else {
        valEl.appendChild(safeText(String(val)));
      }
      item.appendChild(valEl);

      profileStructuredContent.appendChild(item);
    }

    profileStructuredBox.style.display = count > 0 ? 'block' : 'none';
  }

  function renderMemes(memes) {
    clearElement(memesContainer);
    if (!memes || memes.length === 0) {
      const p = document.createElement('p');
      p.className = 'empty-state';
      p.appendChild(safeText('Меми ще не виявлені або профіль порожній.'));
      memesContainer.appendChild(p);
      return;
    }

    memes.forEach((m) => {
      const card = document.createElement('article');
      card.className = 'meme-card';

      const title = document.createElement('h5');
      title.className = 'meme-title';
      title.appendChild(safeText(m.label || m.name || 'Жарт / Мем'));
      card.appendChild(title);

      if (m.description) {
        const desc = document.createElement('p');
        desc.className = 'meme-desc';
        desc.appendChild(safeText(m.description));
        card.appendChild(desc);
      }

      if (m.usage_context) {
        const usage = document.createElement('p');
        usage.className = 'meme-usage';
        const strong = document.createElement('strong');
        strong.appendChild(safeText('Контекст: '));
        usage.appendChild(strong);
        usage.appendChild(safeText(m.usage_context));
        card.appendChild(usage);
      }

      if (m.evidence_message_ids && m.evidence_message_ids.length > 0) {
        const ev = document.createElement('div');
        ev.className = 'meme-evidence';
        const evLabel = document.createElement('span');
        evLabel.className = 'meme-ev-label';
        evLabel.appendChild(safeText('Докази в чаті: '));
        ev.appendChild(evLabel);
        m.evidence_message_ids.forEach((id) => {
          const chip = document.createElement('span');
          chip.className = 'evidence-chip';
          chip.appendChild(safeText(`#${id}`));
          ev.appendChild(chip);
        });
        card.appendChild(ev);
      }

      memesContainer.appendChild(card);
    });
  }

  async function saveProfileEdits(e) {
    e.preventDefault();
    if (!activeChatId) return;

    const targetChatId = activeChatId;
    const targetGen = chatSelectionGeneration;

    versionConflictAlert.style.display = 'none';
    setElementText(profileSaveStatus, 'Збереження...');
    btnSaveProfile.disabled = true;

    const expectedVersion = activeProfile ? activeProfile.version : 0;
    const instructions = profileInstructionsText.value;

    try {
      const res = await apiFetch(`/api/chats/${targetChatId}/profile`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_version: expectedVersion,
          instructions_md: instructions,
        }),
      });
      if (!isCurrentChat(targetChatId, targetGen)) return;
      const updated = await res.json();
      if (!isCurrentChat(targetChatId, targetGen)) return;

      activeProfile = updated;
      setElementText(profileVersionBadge, `Версія: v${updated.version}`);
      setElementText(profileSaveStatus, 'Збережено успішно! ✓');
      setTimeout(() => {
        if (isCurrentChat(targetChatId, targetGen)) clearElement(profileSaveStatus);
      }, 3000);
    } catch (err) {
      if (!isCurrentChat(targetChatId, targetGen)) return;
      if (err.status === 409 || err.code === 'version_conflict') {
        versionConflictAlert.style.display = 'block';
        setElementText(
          versionConflictMsg,
          'Профіль було оновлено паралельно (іншим завданням або вкладкою). Оновіть дані, щоб побачити свіжу версію.'
        );
        setElementText(profileSaveStatus, 'Помилка: конфлікт версій');
      } else {
        setElementText(profileSaveStatus, `Помилка збереження: ${err.message}`);
      }
    } finally {
      if (isCurrentChat(targetChatId, targetGen)) {
        btnSaveProfile.disabled = false;
      }
    }
  }

  // --- AI Profile Suggestions Inspect & Apply ---
  async function checkProfileSuggestion(chatId, chatGen = null) {
    const targetGen = chatGen !== null ? chatGen : chatSelectionGeneration;
    suggestionBanner.style.display = 'none';
    pendingSuggestion = null;

    try {
      const res = await apiFetch(`/api/chats/${chatId}/profile/suggestion`);
      if (!isCurrentChat(chatId, targetGen)) return;
      const sugg = await res.json();
      if (!isCurrentChat(chatId, targetGen)) return;

      pendingSuggestion = sugg;
      suggestionBanner.style.display = 'block';
      setElementText(suggVersionTag, `Пропозиція AI (до v${sugg.version})`);
    } catch (err) {
      if (!isCurrentChat(chatId, targetGen)) return;
      if (err.name === 'AbortError') return;
      // 404 is normal when there are no pending suggestions
      suggestionBanner.style.display = 'none';
    }
  }

  function openSuggestionInspector() {
    if (!pendingSuggestion) return;

    setElementText(
      modalActiveInstructions,
      activeProfile ? activeProfile.instructions_md || '(порожньо)' : '(немає активного профілю)'
    );
    setElementText(
      modalSuggestedInstructions,
      pendingSuggestion.instructions_md || '(порожньо)'
    );

    clearElement(modalSuggestedMemes);
    if (pendingSuggestion.memes && pendingSuggestion.memes.length > 0) {
      pendingSuggestion.memes.forEach((m) => {
        const card = document.createElement('div');
        card.className = 'meme-card';
        const h = document.createElement('strong');
        h.appendChild(safeText(m.label || m.name || 'Мем'));
        card.appendChild(h);
        if (m.description) {
          const p = document.createElement('p');
          p.className = 'meme-desc';
          p.appendChild(safeText(m.description));
          card.appendChild(p);
        }
        modalSuggestedMemes.appendChild(card);
      });
    } else {
      const p = document.createElement('p');
      p.className = 'empty-state';
      p.appendChild(safeText('Нових мемів у цій пропозиції немає.'));
      modalSuggestedMemes.appendChild(p);
    }

    if (suggestionDialog && typeof suggestionDialog.showModal === 'function') {
      suggestionDialog.showModal();
    }
  }

  async function applyPendingSuggestion() {
    if (!activeChatId || !pendingSuggestion) return;

    const targetChatId = activeChatId;
    const targetGen = chatSelectionGeneration;
    const expectedActiveVersion = activeProfile ? activeProfile.version : 0;
    try {
      const res = await apiFetch(`/api/chats/${targetChatId}/profile/apply-suggestion`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_active_version: expectedActiveVersion,
          suggestion_id: pendingSuggestion.id,
        }),
      });
      if (!isCurrentChat(targetChatId, targetGen)) return;
      const applied = await res.json();
      if (!isCurrentChat(targetChatId, targetGen)) return;

      activeProfile = applied;

      // Close modal if open
      if (suggestionDialog && suggestionDialog.open) {
        suggestionDialog.close();
      }

      // Hide suggestion banner and update active profile UI
      suggestionBanner.style.display = 'none';
      pendingSuggestion = null;
      setElementText(profileVersionBadge, `Версія: v${applied.version}`);
      profileInstructionsText.value = applied.instructions_md || '';
      renderMemes(applied.memes || []);

      setElementText(profileSaveStatus, 'Пропозицію AI успішно застосовано! ✓');
      setTimeout(() => {
        if (isCurrentChat(targetChatId, targetGen)) clearElement(profileSaveStatus);
      }, 3000);
    } catch (err) {
      if (!isCurrentChat(targetChatId, targetGen)) return;
      if (err.status === 409 || err.code === 'version_conflict') {
        alert(
          'Конфлікт версій: активний профіль змінився з моменту формування пропозиції. Оновіть сторінку перед застосуванням.'
        );
      } else {
        alert(`Помилка застосування пропозиції: ${err.message}`);
      }
    }
  }

  // --- Playground & Grounded Reply ---
  async function generatePlaygroundReply(e) {
    e.preventDefault();
    if (!activeChatId) {
      alert('Будь ласка, оберіть чат перед генерацією.');
      return;
    }

    const targetChatId = activeChatId;
    const targetGen = chatSelectionGeneration;
    const mode = replyModeSelect.value;
    const contextText = replyContextInput.value.trim();

    replyOutputCard.style.display = 'block';
    setElementText(replyActionBadge, 'Генерація...');
    replyActionBadge.className = 'badge';
    setElementText(replyBodyText, 'Пошук цитат та генерація відповіді...');
    clearElement(replyAttribution);
    clearElement(replyDrawerContent);
    replySourceDrawer.removeAttribute('open');
    setElementText(feedbackAcknowledgement, '');

    try {
      const res = await apiFetch(`/api/chats/${targetChatId}/reply`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mode: mode,
          context: contextText || 'контекст',
        }),
      });
      if (!isCurrentChat(targetChatId, targetGen)) return;
      const data = await res.json();
      if (!isCurrentChat(targetChatId, targetGen)) return;

      lastReplyData = data;
      renderPlaygroundReply(data);
    } catch (err) {
      if (!isCurrentChat(targetChatId, targetGen)) return;
      setElementText(replyActionBadge, 'ПОМИЛКА');
      replyActionBadge.className = 'badge badge-danger';
      if (err.code === 'credential_missing') {
        setElementText(
          replyBodyText,
          'Провайдер AI не налаштований. Режим цитат ("Тільки цитати") працює офлайн без ключа. Для AI-режимів налаштуйте GEMINI_API_KEY.'
        );
      } else {
        setElementText(replyBodyText, `Помилка генерації: ${err.message}`);
      }
    }
  }

  function renderPlaygroundReply(data) {
    const actionMap = {
      quote: { label: 'ЦИТАТА', cls: 'badge-accent' },
      generate: { label: 'ВІДПОВІДЬ', cls: 'badge-success' },
      silent: { label: 'ТИША', cls: 'badge-secondary' },
    };
    const actInfo = actionMap[data.action] || {
      label: (data.action || 'REPLY').toUpperCase(),
      cls: 'badge',
    };
    setElementText(replyActionBadge, actInfo.label);
    replyActionBadge.className = `badge ${actInfo.cls}`;

    if (data.action === 'silent') {
      setElementText(
        replyBodyText,
        '(Бот вирішив промовчати: релевантних цитат або контексту не виявлено)'
      );
    } else if (data.quote && data.quote.text) {
      setElementText(replyBodyText, data.quote.text);
    } else {
      setElementText(replyBodyText, data.text || '');
    }

    // Attribution
    clearElement(replyAttribution);
    if (data.quote) {
      const attrP = document.createElement('p');
      attrP.className = 'attr-line';
      const qDate = data.quote.sent_at ? formatTimestamp(data.quote.sent_at) : '';
      const datePart = qDate && qDate !== '—' ? `, ${qDate}` : '';
      attrP.appendChild(
        safeText(
          `— ${data.quote.sender_name || 'Невідомий'}${datePart}, #${data.quote.source_message_id}`
        )
      );
      replyAttribution.appendChild(attrP);
    } else if (data.attribution) {
      const attrP = document.createElement('p');
      attrP.className = 'attr-line';
      attrP.appendChild(safeText(data.attribution));
      replyAttribution.appendChild(attrP);
    }

    // Source Drawer: render actual selected quote and/or evidence IDs
    clearElement(replyDrawerContent);
    let hasSources = false;

    if (data.quote) {
      hasSources = true;
      const item = document.createElement('div');
      item.className = 'source-item';

      const head = document.createElement('div');
      head.className = 'source-meta';
      const qDate = data.quote.sent_at ? formatTimestamp(data.quote.sent_at) : '';
      const dateStr = qDate && qDate !== '—' ? ` (${qDate})` : '';
      head.appendChild(
        safeText(
          `${data.quote.sender_name || 'Невідомий'}${dateStr} [№${data.quote.source_message_id || data.quote.id}]`
        )
      );

      const body = document.createElement('blockquote');
      body.className = 'source-snippet';
      body.appendChild(safeText(data.quote.text || ''));

      item.appendChild(head);
      item.appendChild(body);
      replyDrawerContent.appendChild(item);
    }

    if (data.evidence_ids && data.evidence_ids.length > 0) {
      hasSources = true;
      const evItem = document.createElement('div');
      evItem.className = 'source-item evidence-summary';
      const head = document.createElement('div');
      head.className = 'source-meta';
      head.appendChild(safeText(`Доказові повідомлення (${data.evidence_ids.length}):`));
      const body = document.createElement('p');
      body.className = 'source-evidence-ids';
      body.appendChild(safeText(`#${data.evidence_ids.join(', #')}`));
      evItem.appendChild(head);
      evItem.appendChild(body);
      replyDrawerContent.appendChild(evItem);
    }

    if (!hasSources) {
      const p = document.createElement('p');
      p.className = 'empty-state';
      p.appendChild(safeText('Немає знайдених цитат або доказів для цієї відповіді.'));
      replyDrawerContent.appendChild(p);
    }

    // Enable feedback buttons if reply has valid id
    feedbackButtons.forEach((b) => (b.disabled = !data.id));
  }

  async function submitFeedback(verdict) {
    if (!activeChatId || !lastReplyData || !lastReplyData.id) return;
    const targetChatId = activeChatId;
    const targetGen = chatSelectionGeneration;
    const replyId = lastReplyData.id;

    setElementText(feedbackAcknowledgement, 'Збереження відгуку...');

    try {
      const res = await apiFetch(
        `/api/chats/${targetChatId}/replies/${encodeURIComponent(replyId)}/feedback`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ verdict }),
        }
      );
      if (!isCurrentChat(targetChatId, targetGen)) return;
      await res.json();
      if (!isCurrentChat(targetChatId, targetGen)) return;

      const verdictLabel =
        verdict === 'landed' ? '👍 Влучно' : verdict === 'missed' ? '👎 Невлучно' : '🔁 Заїжджено';
      setElementText(feedbackAcknowledgement, `Відгук враховано: ${verdictLabel}`);
    } catch (err) {
      if (!isCurrentChat(targetChatId, targetGen)) return;
      setElementText(feedbackAcknowledgement, `Помилка збереження: ${err.message}`);
    }
  }

  // --- Telegram Tab & Bindings ---
  async function loadTelegramStatus() {
    setElementText(tgTokenStatus, 'Перевірка...');
    tgTokenStatus.className = 'badge';

    try {
      const res = await apiFetch('/api/telegram/status');
      const data = await res.json();

      if (data.configured) {
        setElementText(tgTokenStatus, 'Токен налаштовано ✓');
        tgTokenStatus.className = 'badge badge-success';
      } else {
        setElementText(tgTokenStatus, 'Токен відсутній у .env');
        tgTokenStatus.className = 'badge badge-warning';
      }

      clearElement(tgAllowedGroups);
      if (!data.allowed_chat_ids || data.allowed_chat_ids.length === 0) {
        tgAllowedGroups.appendChild(safeText('(Список порожній — бот не відповідатиме в групах)'));
      } else {
        tgAllowedGroups.appendChild(safeText(data.allowed_chat_ids.join(', ')));
      }

      renderTelegramBindings(data.bindings || []);
    } catch (err) {
      console.error('Failed to load Telegram status:', err);
      setElementText(tgTokenStatus, 'Помилка');
      tgTokenStatus.className = 'badge badge-danger';
    }
  }

  function renderTelegramBindings(bindings) {
    clearElement(tgBindingsTbody);
    if (!bindings || bindings.length === 0) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 6;
      td.className = 'text-center';
      td.appendChild(safeText('Немає збережених прив\'язок груп.'));
      tr.appendChild(td);
      tgBindingsTbody.appendChild(tr);
      return;
    }

    bindings.forEach((b) => {
      const tr = document.createElement('tr');

      const tdTgId = document.createElement('td');
      tdTgId.appendChild(safeText(b.telegram_chat_id));

      const tdChat = document.createElement('td');
      tdChat.appendChild(safeText(b.chat_name || b.chat_id));

      const tdState = document.createElement('td');
      const stateBadge = document.createElement('span');
      stateBadge.className = b.enabled ? 'badge badge-success' : 'badge badge-secondary';
      stateBadge.appendChild(safeText(b.enabled ? 'Увімкнено' : 'Вимкнено'));
      tdState.appendChild(stateBadge);

      const tdCooldown = document.createElement('td');
      tdCooldown.appendChild(safeText(`${b.cooldown_seconds} с`));

      const tdLast = document.createElement('td');
      tdLast.appendChild(safeText(b.last_sent_at ? formatTimestamp(b.last_sent_at) : '—'));

      const tdAction = document.createElement('td');
      const btnDelete = document.createElement('button');
      btnDelete.className = 'btn-link text-danger';
      btnDelete.type = 'button';
      btnDelete.appendChild(safeText('Видалити'));
      btnDelete.addEventListener('click', () => deleteTelegramBinding(b.telegram_chat_id));
      tdAction.appendChild(btnDelete);

      tr.appendChild(tdTgId);
      tr.appendChild(tdChat);
      tr.appendChild(tdState);
      tr.appendChild(tdCooldown);
      tr.appendChild(tdLast);
      tr.appendChild(tdAction);

      tgBindingsTbody.appendChild(tr);
    });
  }

  async function saveTelegramBinding(e) {
    e.preventDefault();
    if (!activeChatId) {
      alert('Будь ласка, спершу оберіть активний профіль/чат угорі сторінки.');
      return;
    }

    const tgChatId = tgInputChatId.value.trim();
    const cooldown = parseInt(tgInputCooldown.value, 10) || 10;
    const enabled = tgInputEnabled.checked;

    if (!tgChatId) return;

    setElementText(tgBindStatus, 'Збереження...');
    try {
      const res = await apiFetch(`/api/telegram/bindings/${encodeURIComponent(tgChatId)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          chat_id: activeChatId,
          cooldown_seconds: cooldown,
          enabled: enabled,
        }),
      });
      await res.json();
      setElementText(tgBindStatus, 'Прив\'язку збережено! ✓');
      setTimeout(() => clearElement(tgBindStatus), 3000);
      await loadTelegramStatus();
    } catch (err) {
      setElementText(tgBindStatus, `Помилка: ${err.message}`);
    }
  }

  async function deleteTelegramBinding(tgChatId) {
    if (!confirm(`Видалити прив'язку для Telegram групи ${tgChatId}?`)) return;

    try {
      await apiFetch(`/api/telegram/bindings/${encodeURIComponent(tgChatId)}`, {
        method: 'DELETE',
      });
      await loadTelegramStatus();
    } catch (err) {
      alert(`Не вдалося видалити прив'язку: ${err.message}`);
    }
  }

  // --- Import Modal Handling ---
  function openImportDialog() {
    importResultView.style.display = 'none';
    importForm.style.display = 'block';
    importForm.reset();
    if (importDialog && typeof importDialog.showModal === 'function') {
      importDialog.showModal();
    }
  }

  function closeImportDialog() {
    if (importDialog && importDialog.open) {
      importDialog.close();
    }
  }

  async function handleImportSubmit(e) {
    e.preventDefault();
    const file = importFileInput.files[0];
    if (!file) {
      alert('Оберіть JSON файл експорту.');
      return;
    }

    const submitBtn = document.getElementById('btn-submit-import');
    submitBtn.disabled = true;
    setElementText(submitBtn, 'Імпортування...');

    const formData = new FormData();
    formData.append('file', file);
    const nameOverride = document.getElementById('import-name-override').value.trim();
    if (nameOverride) formData.append('name', nameOverride);
    const tzOverride = document.getElementById('import-timezone').value.trim();
    if (tzOverride) formData.append('timezone', tzOverride);

    try {
      const res = await fetch('/api/chats/import', {
        method: 'POST',
        body: formData,
      });

      if (!res.ok) {
        let errData = {};
        try {
          errData = await res.json();
        } catch {
          // ignore
        }
        throw new Error(
          (errData.error && errData.error.message) || `Помилка імпорту HTTP ${res.status}`
        );
      }

      const report = await res.json();
      renderImportResult(report);
    } catch (err) {
      alert(`Помилка імпорту: ${err.message}`);
    } finally {
      submitBtn.disabled = false;
      setElementText(submitBtn, 'Імпортувати архів');
    }
  }

  function renderImportResult(data) {
    const chat = data.chat || {};
    const rep = data.report || {};

    importForm.style.display = 'none';
    importResultView.style.display = 'block';
    clearElement(importStatsSummary);

    let periodVal = '—';
    if (rep.earliest_sent_at && rep.latest_sent_at) {
      periodVal = `${formatTimestamp(rep.earliest_sent_at)} — ${formatTimestamp(rep.latest_sent_at)}`;
    } else if (rep.earliest_sent_at || rep.latest_sent_at) {
      periodVal = formatTimestamp(rep.earliest_sent_at || rep.latest_sent_at);
    }

    const stats = [
      { label: 'Назва чату', val: chat.name || rep.chat_name || '—' },
      { label: 'Усього записів у файлі', val: rep.total_records ?? 0 },
      { label: 'Імпортовано текстових повідомлень', val: rep.supported_text_messages ?? 0 },
      { label: 'Пропущено медіа', val: rep.unsupported_media_count ?? 0 },
      { label: 'Стікерів пропущено', val: rep.stickers_count ?? 0 },
      { label: 'Дублікатів пропущено', val: rep.duplicate_records ?? 0 },
      { label: 'Період повідомлень', val: periodVal },
    ];

    stats.forEach((s) => {
      const row = document.createElement('div');
      row.className = 'stat-line';
      const b = document.createElement('strong');
      b.appendChild(safeText(`${s.label}: `));
      row.appendChild(b);
      row.appendChild(safeText(String(s.val)));
      importStatsSummary.appendChild(row);
    });

    // When clicking "Перейти до чату", reload chat list and select new chat
    btnFinishImport.onclick = async () => {
      closeImportDialog();
      const targetId = chat.id || (data.chat && data.chat.id);
      if (targetId) {
        await loadChatList(targetId);
      } else {
        await loadChatList();
      }
    };
  }

  // Drag and drop handlers
  if (dropZone) {
    ['dragenter', 'dragover'].forEach((eventName) => {
      dropZone.addEventListener(
        eventName,
        (e) => {
          e.preventDefault();
          dropZone.classList.add('drag-over');
        },
        false
      );
    });
    ['dragleave', 'drop'].forEach((eventName) => {
      dropZone.addEventListener(
        eventName,
        (e) => {
          e.preventDefault();
          dropZone.classList.remove('drag-over');
        },
        false
      );
    });
    dropZone.addEventListener('drop', (e) => {
      const dt = e.dataTransfer;
      const files = dt.files;
      if (files && files.length > 0) {
        importFileInput.files = files;
      }
    });
  }

  // --- Event Listeners Initialization ---
  function initEventListeners() {
    // Chat Picker
    chatSelect.addEventListener('change', (e) => {
      selectChat(e.target.value);
    });

    // Import Dialog Triggers
    btnOpenImport.addEventListener('click', openImportDialog);
    btnCloseImport.addEventListener('click', closeImportDialog);
    btnCancelImport.addEventListener('click', closeImportDialog);
    importForm.addEventListener('submit', handleImportSubmit);

    // Search
    searchForm.addEventListener('submit', (e) => {
      e.preventDefault();
      loadMemories(activeChatId, searchInput.value.trim(), 0);
    });
    btnClearSearch.addEventListener('click', () => {
      searchInput.value = '';
      loadMemories(activeChatId, '', 0);
    });
    btnPrevPage.addEventListener('click', () => {
      if (searchPage > 0) loadMemories(activeChatId, currentSearchQuery, searchPage - 1);
    });
    btnNextPage.addEventListener('click', () => {
      loadMemories(activeChatId, currentSearchQuery, searchPage + 1);
    });

    // Analysis
    btnStartAnalysis.addEventListener('click', startAnalysis);
    btnCancelAnalysis.addEventListener('click', cancelAnalysis);
    btnResumeAnalysis.addEventListener('click', startAnalysis);

    // Profile
    profileEditorForm.addEventListener('submit', saveProfileEdits);
    btnReloadProfile.addEventListener('click', () => {
      if (activeChatId) loadProfile(activeChatId);
    });
    btnInspectSuggestion.addEventListener('click', openSuggestionInspector);
    btnApplySuggestion.addEventListener('click', applyPendingSuggestion);

    // Suggestion Modal
    btnCloseSuggestionModal.addEventListener('click', () => {
      if (suggestionDialog && suggestionDialog.open) suggestionDialog.close();
    });
    btnModalCancelSugg.addEventListener('click', () => {
      if (suggestionDialog && suggestionDialog.open) suggestionDialog.close();
    });
    btnModalApplySugg.addEventListener('click', applyPendingSuggestion);

    // Playground
    replyContextInput.addEventListener('input', () => {
      setElementText(contextCharCount, String(replyContextInput.value.length));
    });
    replyForm.addEventListener('submit', generatePlaygroundReply);
    feedbackButtons.forEach((btn) => {
      btn.addEventListener('click', () => {
        const verdict = btn.getAttribute('data-verdict');
        if (verdict) submitFeedback(verdict);
      });
    });

    // Telegram
    tgBindingForm.addEventListener('submit', saveTelegramBinding);
  }

  // --- Initial Boot ---
  document.addEventListener('DOMContentLoaded', async () => {
    initTabs();
    initEventListeners();
    await loadChatList();
    await loadTelegramStatus();
  });
})();
