// static/app.js
// Vanilla JS controller for X Comment AI (bulk-enabled).
//
// Features:
// - Bulk URL input (one per line, no hard cap; warn above 100, confirm above 500)
// - Concurrency-capped parallel /generate calls (cap = 5)
// - Per-post card with author + clickable "View on X" + variants
// - Per-post Regenerate (calls /regenerate, no re-scrape)
// - Live char counter (warn at 240, danger at 280) per variant
// - Edit-in-place via contenteditable
// - Open reply on X deep-link with current edited text
// - localStorage history (last 10 generations)

(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);

  // ---------- DOM refs ----------
  const form         = $('generate-form');
  const urlsInput    = $('urls');
  const urlCount     = $('url-count');
  const langSelect   = $('lang');
  const toneSelect   = $('tone');
  const lengthSelect = $('length');
  const submitBtn    = $('submit-btn');
  const btnLabel     = $('btn-label');
  const btnLabelText = $('btn-label-text');
  const btnSpinner   = $('btn-spinner');
  const errorBox     = $('error-box');
  const errorText    = $('error-text');
  const results      = $('results');
  const postsList    = $('posts-list');

  const failedWrap   = $('failed-wrap');
  const failedList   = $('failed-list');
  const failedCount  = $('failed-count');
  const failedPlural = $('failed-plural');
  const failedCopy   = $('failed-copy');

  const progressWrap  = $('progress-wrap');
  const progressDone  = $('progress-done');
  const progressTotal = $('progress-total');
  const progressFill  = $('progress-fill');
  const progressStats = $('progress-stats');

  const postTpl    = $('post-template');
  const variantTpl = $('variant-template');

  const historyWrap  = $('history-wrap');
  const historyList  = $('history-list');
  const historyClear = $('history-clear');

  const toast        = $('toast');

  // ---------- constants ----------
  const MAX_POST_PREVIEW = 600;
  const HISTORY_KEY  = 'xcommentai.history.v1';
  const HISTORY_LIMIT = 10;
  const X_LIMIT = 280;
  const X_WARN  = 240;
  // No hard cap on URLs — the practical limit is Groq's daily token quota
  // and X's per-account rate limit, not the frontend.
  const SOFT_WARN_URLS = 100;
  const HARD_WARN_URLS = 500;
  // 3 URLs * 3 variants = 9 in-flight Groq calls peak, well under the
  // free-tier 30 RPM ceiling. Keeps bulk batches reliable.
  const CONCURRENCY = 3;
  const URL_RE = /(?:x\.com|twitter\.com)\/[^/]+\/status\/\d+/i;

  // ---------- helpers ----------
  const showError = (msg) => {
    errorText.textContent = msg;
    errorBox.classList.remove('hidden');
    errorBox.classList.remove('shake');
    void errorBox.offsetWidth;
    errorBox.classList.add('shake');
  };
  const clearError = () => {
    errorBox.classList.add('hidden');
    errorText.textContent = '';
  };

  const setLoading = (on) => {
    submitBtn.disabled = on;
    urlsInput.disabled = on;
    langSelect.disabled = on;
    toneSelect.disabled = on;
    lengthSelect.disabled = on;
    btnLabel.classList.toggle('hidden', on);
    btnSpinner.classList.toggle('hidden', !on);
    btnSpinner.classList.toggle('inline-flex', on);
  };

  const truncate = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + '…' : s);

  const showToast = (msg) => {
    toast.textContent = msg;
    toast.classList.remove('hidden');
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => toast.classList.add('hidden'), 1600);
  };

  // Copy text to clipboard. Returns true on success.
  const copyToClipboard = async (txt) => {
    if (!txt) return false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(txt);
        return true;
      }
      const ta = document.createElement('textarea');
      ta.value = txt;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch {
      return false;
    }
  };

  // ---------- URL parsing ----------
  const parseUrls = (raw) => {
    const seen = new Set();
    const out = [];
    raw.split(/\r?\n/).forEach((line) => {
      const u = line.trim();
      if (!u) return;
      if (!URL_RE.test(u)) return;
      const key = u.toLowerCase();
      if (seen.has(key)) return;
      seen.add(key);
      out.push(u);
    });
    return out;
  };

  const updateUrlCount = () => {
    const urls = parseUrls(urlsInput.value);
    let label = `${urls.length} URL${urls.length === 1 ? '' : 's'}`;
    if (urls.length > HARD_WARN_URLS) {
      label += ' — may hit Groq daily quota / X rate limits';
      urlCount.classList.add('text-rose-400');
      urlCount.classList.remove('text-amber-300', 'text-slate-500');
    } else if (urls.length > SOFT_WARN_URLS) {
      label += ' — large batch, will take a while';
      urlCount.classList.add('text-amber-300');
      urlCount.classList.remove('text-rose-400', 'text-slate-500');
    } else {
      urlCount.classList.add('text-slate-500');
      urlCount.classList.remove('text-rose-400', 'text-amber-300');
    }
    urlCount.textContent = label;
    btnLabelText.textContent = urls.length > 1
      ? `Generate Comments for ${urls.length} posts`
      : 'Generate Comments';
  };
  urlsInput.addEventListener('input', updateUrlCount);

  // ---------- variant rendering ----------
  const updateCounter = (counterEl, len) => {
    counterEl.textContent = `${len} / ${X_LIMIT}`;
    counterEl.classList.toggle('warn', len > X_WARN && len <= X_LIMIT);
    counterEl.classList.toggle('danger', len > X_LIMIT);
  };

  const renderVariant = (text, idx, getPostMeta) => {
    const node = variantTpl.content.firstElementChild.cloneNode(true);

    const badge   = node.querySelector('.variant-badge');
    const textEl  = node.querySelector('.variant-text');
    const counter = node.querySelector('.variant-counter');
    const copyBtn = node.querySelector('.copy-btn');
    const copyLbl = node.querySelector('.copy-label');
    const replyBtn = node.querySelector('.reply-btn');

    badge.textContent = `Variant ${idx + 1}`;
    textEl.textContent = text;
    updateCounter(counter, text.length);

    // "Reply on X" now opens the actual post URL (so user lands on the
    // comment section) and silently copies the variant text to the
    // clipboard so they can paste it into the X reply box.
    const refreshReply = () => {
      const meta = getPostMeta() || {};
      replyBtn.href = meta.url || '#';
    };
    refreshReply();

    textEl.addEventListener('input', () => {
      updateCounter(counter, (textEl.textContent || '').length);
      refreshReply();
    });

    replyBtn.addEventListener('click', async (e) => {
      const meta = getPostMeta() || {};
      const txt = textEl.textContent || '';
      // Don't block the new-tab navigation on clipboard write — fire and
      // forget. We DO want to mark this variant as 'used' in history.
      copyToClipboard(txt).then((ok) => {
        if (ok) showToast('Reply copied — paste it on X');
      });
      markUsed(meta.tweet_id, txt);
      // Anchor's target=_blank handles the navigation natively.
    });

    copyBtn.addEventListener('click', async () => {
      const txt = textEl.textContent || '';
      if (!txt.trim()) return;
      const ok = await copyToClipboard(txt);
      if (ok) {
        copyLbl.textContent = 'Copied!';
        showToast('Copied to clipboard');
        setTimeout(() => { copyLbl.textContent = 'Copy'; }, 1600);
        const meta = getPostMeta() || {};
        markUsed(meta.tweet_id, txt);
      } else {
        showError('Could not copy automatically. Please select and copy manually.');
      }
    });

    return node;
  };

  // ---------- post card ----------
  /**
   * Status: 'pending' (queued), 'loading' (in flight), 'done', 'error'.
   * Each post card is self-contained so failures stay isolated.
   */
  const createPostCard = (url) => {
    const node = postTpl.content.firstElementChild.cloneNode(true);

    const authorName = node.querySelector('.post-author-name');
    const authorHnd  = node.querySelector('.post-author-handle');
    const linkEl     = node.querySelector('.post-link');
    const textEl     = node.querySelector('.post-text');
    const statusIcon = node.querySelector('.post-status-icon');
    const statusText = node.querySelector('.post-status-text');
    const variantsEl = node.querySelector('.post-variants');
    const regenBtn   = node.querySelector('.post-regen-btn');
    const regenLabel = node.querySelector('.post-regen-label');

    // Initial pending state — link still works since we have the URL
    authorName.textContent = '—';
    authorHnd.textContent  = url;
    linkEl.href = url;
    textEl.textContent = '';
    statusIcon.innerHTML = '<span class="inline-block h-2 w-2 rounded-full bg-slate-500"></span>';
    statusText.textContent = 'Queued…';

    let postData = null; // populated on success

    const setStatus = (state, msg) => {
      const colors = {
        pending: 'bg-slate-500',
        loading: 'bg-sky-400 animate-pulse',
        done:    'bg-emerald-400',
        error:   'bg-rose-500',
      };
      statusIcon.innerHTML =
        `<span class="inline-block h-2 w-2 rounded-full ${colors[state] || colors.pending}"></span>`;
      statusText.textContent = msg;
      statusText.classList.toggle('text-rose-300', state === 'error');
      statusText.classList.toggle('text-slate-400', state !== 'error');
    };

    const getPostMeta = () => ({
      tweet_id: postData?.tweet_id,
      url: postData?.url || url,
    });

    const renderResult = (data) => {
      postData = data;
      authorName.textContent = data.author_name || data.author || '';
      authorHnd.textContent  = data.author || '';
      linkEl.href = data.url || url;
      textEl.textContent = truncate(data.original || '', MAX_POST_PREVIEW);
      variantsEl.innerHTML = '';
      (data.variants || []).forEach((v, i) => {
        variantsEl.appendChild(renderVariant(v, i, getPostMeta));
      });
      regenBtn.classList.remove('hidden');
    };

    regenBtn.addEventListener('click', async () => {
      if (!postData) return;
      regenBtn.disabled = true;
      regenLabel.textContent = 'Regenerating…';
      try {
        const data = await callJSON('/regenerate', {
          original: postData.original,
          thread:   postData.thread,
          lang:     langSelect.value || 'auto',
          tone:     toneSelect.value || 'witty',
          length:   lengthSelect.value || 'medium',
          n: 3,
        });
        postData = { ...postData, variants: data.variants || [] };
        variantsEl.innerHTML = '';
        (data.variants || []).forEach((v, i) => {
          variantsEl.appendChild(renderVariant(v, i, getPostMeta));
        });
        pushHistory({ ...postData, at: Date.now() });
      } catch (err) {
        console.error(err);
        setStatus('error', `Regenerate failed: ${err.message || err}`);
      } finally {
        regenBtn.disabled = false;
        regenLabel.textContent = 'Regenerate';
      }
    });

    return { node, setStatus, renderResult, getData: () => postData, url };
  };

  // ---------- failed-links summary ----------
  const failed = []; // [{ url, msg }]

  const renderFailed = () => {
    failedCount.textContent = String(failed.length);
    failedPlural.textContent = failed.length === 1 ? '' : 's';
    if (failed.length === 0) {
      failedWrap.classList.add('hidden');
      failedList.innerHTML = '';
      return;
    }
    failedWrap.classList.remove('hidden');
    failedList.innerHTML = '';
    failed.forEach(({ url, msg }) => {
      const li = document.createElement('li');
      li.className = 'rounded-lg border border-rose-500/20 bg-rose-950/20 px-3 py-2 space-y-1';
      const top = document.createElement('div');
      top.className = 'flex items-start justify-between gap-3';
      const a = document.createElement('a');
      a.href = url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.textContent = url;
      a.className = 'text-rose-200 hover:text-rose-100 underline-offset-4 hover:underline truncate text-xs sm:text-sm font-mono min-w-0';
      const reason = document.createElement('p');
      reason.className = 'text-xs text-rose-300/80 leading-snug';
      reason.textContent = msg || 'Failed';
      top.appendChild(a);
      li.appendChild(top);
      li.appendChild(reason);
      failedList.appendChild(li);
    });
  };

  const addFailed = (url, msg) => {
    failed.push({ url, msg });
    renderFailed();
  };

  const resetFailed = () => {
    failed.length = 0;
    renderFailed();
  };

  failedCopy.addEventListener('click', async (e) => {
    e.preventDefault();
    if (failed.length === 0) return;
    const text = failed.map((f) => f.url).join('\n');
    const ok = await copyToClipboard(text);
    showToast(ok ? `Copied ${failed.length} URL${failed.length === 1 ? '' : 's'}` : 'Copy failed');
  });

  // ---------- history (localStorage) ----------
  const loadHistory = () => {
    try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]'); }
    catch { return []; }
  };
  const saveHistory = (items) => {
    try { localStorage.setItem(HISTORY_KEY, JSON.stringify(items.slice(0, HISTORY_LIMIT))); }
    catch { /* quota — ignore */ }
  };
  const pushHistory = (entry) => {
    const items = loadHistory();
    const filtered = items.filter((it) => it.tweet_id !== entry.tweet_id);
    filtered.unshift(entry);
    saveHistory(filtered);
    renderHistory();
  };
  const markUsed = (tweet_id, used_text) => {
    if (!tweet_id) return;
    const items = loadHistory();
    const idx = items.findIndex((it) => it.tweet_id === tweet_id);
    if (idx === -1) return;
    items[idx].used = used_text;
    items[idx].at = Date.now();
    saveHistory(items);
    renderHistory();
  };
  const renderHistory = () => {
    const items = loadHistory();
    historyList.innerHTML = '';
    if (items.length === 0) {
      historyWrap.classList.add('hidden');
      return;
    }
    historyWrap.classList.remove('hidden');
    items.forEach((it) => {
      const li = document.createElement('li');
      li.className =
        'flex items-start justify-between gap-3 rounded-xl border border-white/5 bg-white/5 hover:bg-white/10 px-3 py-2 cursor-pointer transition';
      const left = document.createElement('div');
      left.className = 'min-w-0';
      const handle = document.createElement('p');
      handle.className = 'text-xs text-slate-400 truncate';
      handle.textContent = it.author || '@unknown';
      const preview = document.createElement('p');
      preview.className = 'text-sm text-slate-200 truncate';
      preview.textContent = truncate(it.original || '', 80);
      left.appendChild(handle);
      left.appendChild(preview);

      const right = document.createElement('span');
      right.className = 'text-xs text-slate-500 shrink-0';
      right.textContent = new Date(it.at || Date.now()).toLocaleDateString();

      li.appendChild(left);
      li.appendChild(right);
      li.addEventListener('click', () => {
        // Restore: render this single entry as a post card at top
        urlsInput.value = it.url || '';
        updateUrlCount();
        postsList.innerHTML = '';
        progressWrap.classList.add('hidden');
        const card = createPostCard(it.url || '');
        postsList.appendChild(card.node);
        card.renderResult(it);
        card.setStatus('done', 'From history');
        results.classList.remove('hidden');
        results.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
      historyList.appendChild(li);
    });
  };
  historyClear.addEventListener('click', () => {
    if (!confirm('Clear all history?')) return;
    localStorage.removeItem(HISTORY_KEY);
    renderHistory();
  });

  // ---------- network ----------
  const callJSON = async (path, body) => {
    const resp = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const detail = (data && (data.detail || data.message)) || `Request failed (${resp.status}).`;
      throw new Error(detail);
    }
    return data;
  };

  // ---------- bulk processing ----------
  /**
   * Process URLs with a fixed concurrency cap.
   * Each URL gets its own card; failures don't poison other URLs.
   */
  const runBulk = async (urls) => {
    const tone   = toneSelect.value || 'witty';
    const length = lengthSelect.value || 'medium';
    const lang   = langSelect.value   || 'auto';

    // Set up progress UI
    progressTotal.textContent = String(urls.length);
    progressDone.textContent = '0';
    progressFill.style.width = '0%';
    progressStats.textContent = '';
    progressWrap.classList.remove('hidden');

    // Build all cards up-front so user sees the full list immediately
    const cards = urls.map((u) => {
      const card = createPostCard(u);
      postsList.appendChild(card.node);
      return { url: u, card };
    });

    let done = 0;
    let okCount = 0;
    let errCount = 0;

    const runOne = async ({ url, card }) => {
      card.setStatus('loading', 'Fetching tweet + generating…');
      try {
        const data = await callJSON('/generate', { url, lang, tone, length, n: 3 });
        card.renderResult(data);
        card.setStatus('done', `Done · ${(data.variants || []).length} variants`);
        pushHistory({
          tweet_id: data.tweet_id,
          url: data.url,
          author: data.author,
          author_name: data.author_name,
          original: data.original,
          thread: data.thread || [],
          variants: data.variants || [],
          at: Date.now(),
        });
        okCount++;
      } catch (err) {
        console.error(url, err);
        const msg = err.message || 'Failed';
        card.setStatus('error', msg);
        addFailed(url, msg);
        errCount++;
      } finally {
        done++;
        progressDone.textContent = String(done);
        progressFill.style.width = `${(done / urls.length) * 100}%`;
        progressStats.textContent = `${okCount} ok · ${errCount} failed`;
      }
    };

    // Worker pool — each worker pulls jobs until queue empty
    const queue = [...cards];
    const worker = async () => {
      while (queue.length) {
        const job = queue.shift();
        if (!job) return;
        await runOne(job);
      }
    };
    const workers = Array.from({ length: Math.min(CONCURRENCY, urls.length) }, () => worker());
    await Promise.all(workers);
  };

  // ---------- submit ----------
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearError();

    const urls = parseUrls(urlsInput.value);
    if (urls.length === 0) {
      showError('Paste at least one X post URL (one per line). URLs must look like https://x.com/<user>/status/<id>.');
      return;
    }
    if (urls.length > HARD_WARN_URLS) {
      const proceed = window.confirm(
        `${urls.length} URLs is a very large batch. This may exhaust Groq's `
        + `free-tier daily quota and trigger X rate limits on your scraper `
        + `account, which can leave many cards as failed.\n\n`
        + `Estimated time: ~${Math.ceil((urls.length * 4) / 60)} min.\n\n`
        + `Continue?`
      );
      if (!proceed) return;
    }

    setLoading(true);
    postsList.innerHTML = '';
    resetFailed();
    results.classList.remove('hidden');
    results.scrollIntoView({ behavior: 'smooth', block: 'start' });

    try {
      await runBulk(urls);
    } catch (err) {
      console.error(err);
      showError(err.message || 'Something went wrong.');
    } finally {
      setLoading(false);
    }
  });

  // ---------- UX niceties ----------
  // Auto-paste an X URL from clipboard when focusing the empty textarea
  urlsInput.addEventListener('focus', async () => {
    if (urlsInput.value.trim()) return;
    try {
      const text = await navigator.clipboard.readText();
      if (text && URL_RE.test(text)) {
        urlsInput.value = text.trim();
        updateUrlCount();
      }
    } catch { /* ignore */ }
  });

  // Cmd/Ctrl + Enter submits from anywhere
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      form.requestSubmit();
    }
  });

  // Initial paint
  updateUrlCount();
  renderHistory();
})();
