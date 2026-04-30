// static/app.js
// Vanilla JS controller for RIFAT < AI (bulk-enabled).
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

  // ---------- PWA: service worker + install prompt ----------
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {});
    });
  }

  let deferredInstallPrompt = null;
  const installBtn  = $('install-app');
  const installHint = $('install-hint');

  // Detect already-installed standalone mode (iOS + Android/Chrome).
  const isStandalone =
    window.matchMedia?.('(display-mode: standalone)').matches ||
    window.navigator.standalone === true;

  // iOS Safari does NOT fire beforeinstallprompt — show a manual hint instead.
  const ua = navigator.userAgent || '';
  const isIOS = /iPad|iPhone|iPod/.test(ua) && !window.MSStream;
  const isSafari = /Safari/.test(ua) && !/Chrome|CriOS|FxiOS|EdgiOS/.test(ua);

  if (!isStandalone && isIOS && isSafari && installHint) {
    installHint.hidden = false;
  }

  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferredInstallPrompt = e;
    if (installBtn && !isStandalone) installBtn.hidden = false;
  });

  installBtn?.addEventListener('click', async () => {
    if (!deferredInstallPrompt) return;
    deferredInstallPrompt.prompt();
    const { outcome } = await deferredInstallPrompt.userChoice;
    deferredInstallPrompt = null;
    installBtn.hidden = true;
    if (outcome !== 'accepted' && installHint) installHint.hidden = false;
  });

  window.addEventListener('appinstalled', () => {
    if (installBtn) installBtn.hidden = true;
    if (installHint) installHint.hidden = true;
  });

  // ---------- DOM refs ----------
  const form         = $('generate-form');
  const urlsInput    = $('urls');
  const urlCount     = $('url-count');
  const langSelect   = $('lang');
  const toneSelect   = $('tone');
  const lengthSelect = $('length');
  const variantsSelect = $('variants');
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
  const failedRetry  = $('failed-retry');
  const failedRetryLabel = $('failed-retry-label');

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
    if (variantsSelect) variantsSelect.disabled = on;
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

  // ---------- auto-paste preview cache ----------
  // url → { ok, author, author_name, text_preview, tweet_id, url }
  // Populated by debounced /preview calls fired on textarea input. Used by
  // createPostCard to skip the header skeleton and show real metadata as
  // soon as the URL is resolved.
  const previewCache = new Map();
  const previewInflight = new Set();
  const PREVIEW_BATCH_LIMIT = 10;

  let previewDebounce = null;
  const schedulePreviewFetch = () => {
    clearTimeout(previewDebounce);
    previewDebounce = setTimeout(runPreviewFetch, 600);
  };

  const runPreviewFetch = async () => {
    const urls = parseUrls(urlsInput.value);
    // Only fetch what we don't already have / aren't already fetching.
    const fresh = urls.filter(
      (u) => !previewCache.has(u) && !previewInflight.has(u)
    ).slice(0, PREVIEW_BATCH_LIMIT);
    if (fresh.length === 0) return;
    fresh.forEach((u) => previewInflight.add(u));
    try {
      const r = await fetch('/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ urls: fresh }),
      });
      if (!r.ok) return;
      const data = await r.json().catch(() => ({}));
      (data.results || []).forEach((item, idx) => {
        if (item && item.ok) {
          previewCache.set(item.url, item);
          // Also key by the originally-pasted URL so cache lookups by either
          // form succeed. parseUrls normalises but server may canonicalise
          // (e.g. https://twitter.com/... → https://x.com/...).
          const requested = fresh[idx];
          if (requested && requested !== item.url) previewCache.set(requested, item);
        }
      });
      updatePreviewBadge();
    } catch (_) { /* ignore — preview is best-effort */ }
    finally { fresh.forEach((u) => previewInflight.delete(u)); }
  };

  // Show a small "Found N tweets" badge near the URL counter so users see
  // the previews are working.
  const updatePreviewBadge = () => {
    const urls = parseUrls(urlsInput.value);
    if (!urls.length) {
      if (urlPreviewBadge) urlPreviewBadge.classList.add('hidden');
      return;
    }
    const ready = urls.filter((u) => previewCache.has(u)).length;
    if (ready === 0) {
      urlPreviewBadge?.classList.add('hidden');
      return;
    }
    if (!urlPreviewBadge) return;
    urlPreviewBadge.textContent = `${ready}/${urls.length} ready`;
    urlPreviewBadge.classList.remove('hidden');
  };

  const urlPreviewBadge = document.getElementById('url-preview-badge');

  // Trigger preview on input (debounced) and on paste (immediate-ish).
  urlsInput.addEventListener('input', schedulePreviewFetch);
  urlsInput.addEventListener('paste', () => setTimeout(schedulePreviewFetch, 50));

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

    // Skeleton placeholders shown while pending/loading. They replace
    // themselves with real text once renderResult() runs.
    const applyHeaderSkeleton = () => {
      authorName.innerHTML = '<span class="skeleton skeleton-line" style="width:110px;"></span>';
      authorHnd.innerHTML  = '<span class="skeleton skeleton-line-sm" style="width:160px;"></span>';
      textEl.innerHTML =
        '<span class="skeleton skeleton-block" style="width:100%;"></span>' +
        '<span class="skeleton skeleton-block" style="width:88%;"></span>' +
        '<span class="skeleton skeleton-block" style="width:62%;"></span>';
    };

    const applyHeader = (preview) => {
      authorName.textContent = preview.author_name || preview.author || '—';
      authorHnd.textContent  = preview.author || '';
      linkEl.href            = preview.url || url;
      textEl.textContent     = truncate(preview.text_preview || '', MAX_POST_PREVIEW);
    };

    const setSkeleton = (on, variantCount = 0) => {
      if (!on) return;
      // If we have a server-resolved preview for this URL, render the real
      // header + body now and only skeletonise the (still-loading) variants.
      const preview = (typeof previewCache !== 'undefined') ? previewCache.get(url) : null;
      if (preview) applyHeader(preview);
      else applyHeaderSkeleton();

      if (variantCount > 0) {
        variantsEl.innerHTML = '';
        for (let i = 0; i < variantCount; i++) {
          variantsEl.insertAdjacentHTML('beforeend',
            '<div class="skeleton-variant">' +
              '<span class="skeleton skeleton-line" style="width:30%;"></span>' +
              '<span class="skeleton skeleton-block" style="width:100%;"></span>' +
              '<span class="skeleton skeleton-block" style="width:92%;"></span>' +
              '<span class="skeleton skeleton-block" style="width:48%;"></span>' +
            '</div>');
        }
      }
    };

    linkEl.href = url;
    statusIcon.classList.add('pending');
    statusText.textContent = 'Queued…';
    setSkeleton(true);

    let postData = null; // populated on success

    const setStatus = (state, msg) => {
      // The .post-status-icon element is styled via CSS based on the
      // active state class — pending/loading/done/error.
      statusIcon.classList.remove('pending', 'loading', 'done', 'error');
      statusIcon.classList.add(state || 'pending');
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

    // ---- Streaming-mode helpers (used by /generate/stream consumer) ----
    // After applyMeta, we still hold the N skeleton variant placeholders.
    // Each `streamDelta(idx, txt)` swaps the i-th skeleton for a live
    // container the first time, then appends text. `streamVariantDone`
    // upgrades it to a full renderVariant card with copy/reply controls.
    const liveSlots = new Map();  // idx → { container, textEl }

    const ensureLiveSlot = (idx) => {
      let slot = liveSlots.get(idx);
      if (slot) return slot;
      const container = document.createElement('article');
      container.className = 'variant rounded-2xl border border-white/[0.07] bg-ink-800/60 backdrop-blur-xl p-5 sm:p-6 shadow-soft';
      container.innerHTML = `
        <header class="flex items-center justify-between mb-3">
          <span class="text-[10.5px] font-semibold uppercase tracking-[0.12em] text-accent-400">Variant ${idx + 1}</span>
          <span class="text-[11px] tabular-nums text-slate-500 inline-flex items-center gap-1.5">
            <span class="spinner" style="width:10px;height:10px;border-width:1.5px"></span>
            <span class="streaming-counter">0 / ${X_LIMIT}</span>
          </span>
        </header>
        <div class="variant-text whitespace-pre-wrap text-slate-100 text-[17px] leading-relaxed p-2 -mx-1 caret-accent-400"></div>
        <div class="streaming-cursor inline-block w-[2px] h-[18px] -ml-[2px] align-text-bottom bg-accent-400/80 animate-pulse"></div>
      `;
      const textEl = container.querySelector('.variant-text');
      const counter = container.querySelector('.streaming-counter');
      // Replace the i-th skeleton (or append if none).
      const skeletons = variantsEl.querySelectorAll('.skeleton-variant');
      if (skeletons[idx]) {
        skeletons[idx].replaceWith(container);
      } else {
        variantsEl.appendChild(container);
      }
      slot = { container, textEl, counter, accumulated: '' };
      liveSlots.set(idx, slot);
      return slot;
    };

    const applyMeta = (meta) => {
      // Treat meta the same as renderResult, but with empty variants — the
      // skeleton variant placeholders stay in place until streamDelta swaps
      // them.
      postData = {
        url: meta.url || url,
        tweet_id: meta.tweet_id,
        author: meta.author,
        author_name: meta.author_name,
        original: meta.original,
        thread: meta.thread || [],
        variants: [],
      };
      authorName.textContent = meta.author_name || meta.author || '';
      authorHnd.textContent  = meta.author || '';
      linkEl.href            = meta.url || url;
      textEl.textContent     = truncate(meta.original || '', MAX_POST_PREVIEW);
    };

    const streamDelta = (idx, delta) => {
      const slot = ensureLiveSlot(idx);
      slot.accumulated += delta;
      slot.textEl.textContent = slot.accumulated;
      const len = slot.accumulated.length;
      if (slot.counter) slot.counter.textContent = `${len} / ${X_LIMIT}`;
    };

    const streamVariantDone = (idx, finalText) => {
      const slot = liveSlots.get(idx);
      const fullCard = renderVariant(finalText, idx, getPostMeta);
      if (slot && slot.container.parentNode) {
        slot.container.replaceWith(fullCard);
      } else {
        // No prior delta — just replace skeleton[idx] or append.
        const skeletons = variantsEl.querySelectorAll('.skeleton-variant');
        if (skeletons[idx]) skeletons[idx].replaceWith(fullCard);
        else variantsEl.appendChild(fullCard);
      }
      liveSlots.delete(idx);
      if (postData) postData.variants[idx] = finalText;
    };

    const streamVariantError = (idx, errMsg) => {
      const slot = liveSlots.get(idx);
      const errCard = document.createElement('article');
      errCard.className = 'variant rounded-2xl border border-rose-500/25 bg-rose-500/[0.06] p-4 text-[13px] text-rose-200';
      errCard.textContent = `Variant ${idx + 1} failed: ${errMsg}`;
      if (slot && slot.container.parentNode) {
        slot.container.replaceWith(errCard);
      } else {
        const skeletons = variantsEl.querySelectorAll('.skeleton-variant');
        if (skeletons[idx]) skeletons[idx].replaceWith(errCard);
        else variantsEl.appendChild(errCard);
      }
      liveSlots.delete(idx);
    };

    const streamFinish = (variants) => {
      // Clean up any leftover skeletons (shouldn't happen, but be safe)
      variantsEl.querySelectorAll('.skeleton-variant').forEach((el) => el.remove());
      regenBtn.classList.remove('hidden');
      if (postData && Array.isArray(variants) && variants.length) {
        postData.variants = variants;
      }
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
          n: parseInt(variantsSelect?.value, 10) || 2,
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

    return {
      node,
      setStatus,
      renderResult,
      setSkeleton,
      applyMeta,
      streamDelta,
      streamVariantDone,
      streamVariantError,
      streamFinish,
      getData: () => postData,
      url,
    };
  };

  // ---------- failed-links summary ----------
  const failed = []; // [{ url, msg, card }]
  let bulkRunning = false;

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
    failedRetry.disabled = bulkRunning;
    failed.forEach(({ url, msg }) => {
      const li = document.createElement('li');
      li.className = 'rounded-xl border border-rose-500/15 bg-rose-950/25 px-3.5 py-2.5 space-y-1';
      const top = document.createElement('div');
      top.className = 'flex items-start justify-between gap-3';
      const a = document.createElement('a');
      a.href = url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.textContent = url;
      a.className = 'text-rose-200 hover:text-rose-100 underline-offset-4 hover:underline truncate text-[12px] font-mono min-w-0';
      const reason = document.createElement('p');
      reason.className = 'text-[11.5px] text-rose-300/80 leading-snug';
      reason.textContent = msg || 'Failed';
      top.appendChild(a);
      li.appendChild(top);
      li.appendChild(reason);
      failedList.appendChild(li);
    });
  };

  const addFailed = (url, msg, card) => {
    failed.push({ url, msg, card });
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

  failedRetry.addEventListener('click', async () => {
    if (bulkRunning || failed.length === 0) return;
    // Snapshot the failures we're retrying — the array will be cleared and
    // refilled with any new failures from the retry pass.
    const toRetry = failed.slice();
    // Remove the old failed cards from the DOM; new ones will replace them
    // at the bottom of the post list as they get processed.
    toRetry.forEach((f) => {
      if (f.card?.node?.parentNode) f.card.node.parentNode.removeChild(f.card.node);
    });
    failed.length = 0;
    renderFailed();

    failedRetryLabel.textContent = `Retrying ${toRetry.length}…`;
    failedRetry.disabled = true;
    try {
      await runBulk(toRetry.map((f) => f.url), { append: true });
    } finally {
      failedRetryLabel.textContent = 'Retry all failed';
      failedRetry.disabled = bulkRunning || failed.length === 0;
    }
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
        'flex items-start justify-between gap-3 rounded-2xl border border-white/[0.06] bg-white/[0.025] hover:bg-white/[0.06] px-4 py-3 cursor-pointer';
      const left = document.createElement('div');
      left.className = 'min-w-0';
      const handle = document.createElement('p');
      handle.className = 'text-[11.5px] text-slate-500 truncate';
      handle.textContent = it.author || '@unknown';
      const preview = document.createElement('p');
      preview.className = 'text-[13.5px] text-slate-200 truncate mt-0.5';
      preview.textContent = truncate(it.original || '', 80);
      left.appendChild(handle);
      left.appendChild(preview);

      const right = document.createElement('span');
      right.className = 'text-[11px] text-slate-600 shrink-0 mt-0.5 tabular-nums';
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

  // ---------- streaming ----------
  /**
   * Consume a Server-Sent-Events stream from /generate/stream and dispatch
   * each event to the right card hook. Resolves when the stream ends; rejects
   * on a terminal error event.
   *
   * Hooks called on `card`:
   *   meta          -> applyMeta
   *   delta         -> streamDelta
   *   variant_done  -> streamVariantDone
   *   variant_error -> streamVariantError
   *   done          -> streamFinish
   */
  const streamGenerate = async (card, body) => {
    const resp = await fetch('/generate/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body),
    });
    if (!resp.ok || !resp.body) {
      // Fallback: server didn't return a stream (proxy stripped it, etc.)
      const text = await resp.text().catch(() => '');
      throw new Error(text || `Request failed (${resp.status}).`);
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder('utf-8');
    let buf = '';
    let finalVariants = null;
    let terminalError = null;

    const handle = (event, json) => {
      let data;
      try { data = JSON.parse(json); } catch { return; }
      switch (event) {
        case 'meta':          card.applyMeta(data); break;
        case 'delta':         card.streamDelta(data.idx, data.delta); break;
        case 'variant_done':  card.streamVariantDone(data.idx, data.text); break;
        case 'variant_error': card.streamVariantError(data.idx, data.error); break;
        case 'done':          finalVariants = data.variants || []; break;
        case 'error':         terminalError = data.error || 'Streaming failed.'; break;
      }
    };

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });

      // SSE events are separated by a blank line. Each event consists of
      // optional `event: type\n` followed by `data: payload\n` lines.
      let sep;
      while ((sep = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        let evType = 'message';
        let dataLines = [];
        for (const line of block.split('\n')) {
          if (line.startsWith('event:')) evType = line.slice(6).trim();
          else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
        }
        if (dataLines.length) handle(evType, dataLines.join('\n'));
      }
    }

    if (terminalError) throw new Error(terminalError);
    return { variants: finalVariants || [] };
  };

  // ---------- bulk processing ----------
  /**
   * Process URLs with a fixed concurrency cap.
   * Each URL gets its own card; failures don't poison other URLs.
   */
  const runBulk = async (urls, opts = {}) => {
    const append = !!opts.append;
    if (urls.length === 0) return;

    const tone   = toneSelect.value || 'witty';
    const length = lengthSelect.value || 'medium';
    const n = parseInt(variantsSelect?.value, 10) || 2;
    const lang   = langSelect.value   || 'auto';

    // Set up progress UI — always scoped to *this* batch.
    progressTotal.textContent = String(urls.length);
    progressDone.textContent = '0';
    progressFill.style.width = '0%';
    progressStats.textContent = '';
    progressWrap.classList.remove('hidden');

    // Build all cards up-front so user sees the full list immediately.
    // For retries we append at the bottom rather than wiping prior results.
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
      card.setSkeleton(true, n);
      try {
        const stream = await streamGenerate(card, { url, lang, tone, length, n });
        const data = card.getData() || {};
        card.streamFinish(stream.variants);
        card.setStatus('done', `Done · ${(stream.variants || []).length} variants`);
        pushHistory({
          tweet_id: data.tweet_id,
          url: data.url || url,
          author: data.author,
          author_name: data.author_name,
          original: data.original,
          thread: data.thread || [],
          variants: stream.variants || data.variants || [],
          at: Date.now(),
        });
        okCount++;
      } catch (err) {
        console.error(url, err);
        const msg = err.message || 'Failed';
        card.setStatus('error', msg);
        card.streamFinish([]);  // tear down skeleton placeholders on terminal stream error
        addFailed(url, msg, card);
        errCount++;
      } finally {
        done++;
        progressDone.textContent = String(done);
        progressFill.style.width = `${(done / urls.length) * 100}%`;
        progressStats.textContent = `${okCount} ok · ${errCount} failed`;
      }
    };

    bulkRunning = true;
    renderFailed();
    try {
      // Worker pool — each worker pulls jobs until queue empty
      const queue = [...cards];
      const worker = async () => {
        while (queue.length) {
          const job = queue.shift();
          if (!job) return;
          await runOne(job);
        }
      };
      const workers = Array.from(
        { length: Math.min(CONCURRENCY, urls.length) },
        () => worker()
      );
      await Promise.all(workers);
    } finally {
      bulkRunning = false;
      renderFailed();
    }
    // Reference `append` to silence unused-var warnings; reserved for future
    // tweaks (e.g. continuing progress bar across batches).
    void append;
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
        schedulePreviewFetch();
      }
    } catch { /* ignore */ }
  });

  // (Cmd/Ctrl+Enter submit is wired up in the keyboard-shortcuts block
  //  below — alongside Cmd/Ctrl+1/2 and ?, so all modifier shortcuts share
  //  one preventDefault path.)

  // Initial paint
  updateUrlCount();
  renderHistory();

  // ===========================================================================
  //  CONTESTS PANEL
  //  Discover currently-running X contests, ranked by engagement.
  //  Sister-feature to the reply generator: each contest card has a one-click
  //  "Generate reply" that flips back to the Generate panel and pre-fills the URL.
  // ===========================================================================
  const tabGenerate = $('tab-generate');
  const tabContests = $('tab-contests');
  const tabTools    = $('tab-tools');
  const panelGenerate = $('panel-generate');
  const panelContests = $('panel-contests');
  const panelTools    = $('panel-tools');

  const contestForm    = $('contest-form');
  const contestSubmit  = $('contest-submit');
  const contestBtnLbl  = $('contest-btn-label');
  const contestBtnSpin = $('contest-btn-spinner');
  const contestError   = $('contest-error');
  const contestResultsWrap = $('contest-results-wrap');
  const contestList    = $('contest-list');
  const contestEmpty   = $('contest-empty');
  const contestCountEl = $('contest-result-count');
  const contestPlural  = $('contest-result-plural');
  const contestMeta    = $('contest-result-meta');
  const contestCustomWrap = $('contest-custom-wrap');
  const contestCustomTxt  = $('contest-custom');
  const contestRecency = $('contest-recency');
  const contestMinEng  = $('contest-min-eng');
  const contestLimit   = $('contest-limit');
  const contestStrict  = $('contest-strict');
  const contestAutoref = $('contest-autorefresh');
  const contestModeBtns = document.querySelectorAll('.contest-mode-btn');
  const contestTpl = $('contest-template');

  let contestMode = 'ai';
  let contestAutoTimer = null;

  // ---- Tab switching ----
  function switchTab(target) {
    const tab = target === 'contests' || target === 'tools' ? target : 'generate';
    panelGenerate.classList.toggle('hidden', tab !== 'generate');
    panelContests.classList.toggle('hidden', tab !== 'contests');
    if (panelTools) panelTools.classList.toggle('hidden', tab !== 'tools');
    tabGenerate.classList.toggle('tab-pill-active', tab === 'generate');
    tabContests.classList.toggle('tab-pill-active', tab === 'contests');
    if (tabTools) tabTools.classList.toggle('tab-pill-active', tab === 'tools');
    tabGenerate.setAttribute('aria-selected', String(tab === 'generate'));
    tabContests.setAttribute('aria-selected', String(tab === 'contests'));
    if (tabTools) tabTools.setAttribute('aria-selected', String(tab === 'tools'));
    // Stop any running auto-refresh when leaving the contests tab.
    if (tab !== 'contests' && contestAutoTimer) {
      clearInterval(contestAutoTimer);
      contestAutoTimer = null;
      if (contestAutoref) contestAutoref.checked = false;
    }
  }
  tabGenerate?.addEventListener('click', () => switchTab('generate'));
  tabContests?.addEventListener('click', () => switchTab('contests'));
  tabTools?.addEventListener('click', () => switchTab('tools'));

  // ---- Mode toggle (AI / All / Custom) ----
  contestModeBtns.forEach((btn) => {
    btn.addEventListener('click', () => {
      contestModeBtns.forEach((b) => b.classList.remove('contest-mode-active'));
      btn.classList.add('contest-mode-active');
      contestMode = btn.dataset.mode || 'ai';
      contestCustomWrap.classList.toggle('hidden', contestMode !== 'custom');
    });
  });

  // ---- Helpers ----
  const fmtCount = (n) => {
    if (!n) return '0';
    if (n < 1000) return String(n);
    if (n < 1_000_000) return (n / 1000).toFixed(n < 10_000 ? 1 : 0).replace(/\.0$/, '') + 'K';
    return (n / 1_000_000).toFixed(1).replace(/\.0$/, '') + 'M';
  };
  const fmtAge = (h) => {
    if (h < 1) return `${Math.max(1, Math.round(h * 60))}m ago`;
    if (h < 24) return `${Math.round(h)}h ago`;
    const d = Math.round(h / 24);
    return `${d}d ago`;
  };
  const TYPE_LABEL = { ai: 'AI', video: 'Video', meme: 'Meme', art: 'Art', general: 'Contest' };

  const setContestLoading = (on) => {
    contestSubmit.disabled = on;
    contestBtnLbl.classList.toggle('hidden', on);
    contestBtnSpin.classList.toggle('hidden', !on);
    contestBtnSpin.classList.toggle('inline-flex', on);
  };
  const showContestError = (msg) => {
    contestError.textContent = msg;
    contestError.classList.remove('hidden');
  };
  const clearContestError = () => {
    contestError.classList.add('hidden');
    contestError.textContent = '';
  };

  // ---- Render one contest card from a server result ----
  function renderContestCard(c) {
    const node = contestTpl.content.firstElementChild.cloneNode(true);
    node.querySelector('.contest-author-name').textContent = c.author_name || c.author || 'Unknown';
    node.querySelector('.contest-author-handle').textContent = c.author || '';
    node.querySelector('.contest-text').textContent =
      (c.content || '').slice(0, MAX_POST_PREVIEW) + ((c.content || '').length > MAX_POST_PREVIEW ? '…' : '');

    const badge = node.querySelector('.contest-type-badge');
    badge.textContent = TYPE_LABEL[c.contest_type] || 'Contest';
    badge.classList.add(`contest-badge-${c.contest_type || 'general'}`);

    node.querySelector('.contest-likes').textContent    = fmtCount(c.likes);
    node.querySelector('.contest-retweets').textContent = fmtCount(c.retweets);
    node.querySelector('.contest-replies').textContent  = fmtCount(c.replies);
    node.querySelector('.contest-views').textContent    = fmtCount(c.views);
    node.querySelector('.contest-age').textContent      = fmtAge(c.age_hours || 0);
    node.querySelector('.contest-score-val').textContent = fmtCount(Math.round(c.engagement_score || 0));

    if (c.deadline_hint) {
      const dl = node.querySelector('.contest-deadline');
      dl.classList.remove('hidden');
      dl.classList.add('inline-flex');
      node.querySelector('.contest-deadline-val').textContent = c.deadline_hint;
    }

    const link = node.querySelector('.contest-link');
    link.href = c.url || `https://x.com/i/web/status/${c.id}`;

    const replyBtn = node.querySelector('.contest-reply-btn');
    replyBtn.addEventListener('click', () => {
      // Switch to Generate tab and pre-fill URL with this contest.
      switchTab('generate');
      urlsInput.value = c.url || `https://x.com/i/web/status/${c.id}`;
      updateUrlCount();
      window.scrollTo({ top: 0, behavior: 'smooth' });
      // Auto-submit so the user lands directly on results.
      setTimeout(() => form.requestSubmit(), 200);
    });

    return node;
  }

  // ---- Search ----
  async function runContestSearch({ silent = false } = {}) {
    if (!silent) {
      clearContestError();
      setContestLoading(true);
    }

    const payload = {
      mode: contestMode,
      custom_queries:
        contestMode === 'custom' || contestCustomTxt.value.trim()
          ? contestCustomTxt.value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean)
          : [],
      min_engagement: parseFloat(contestMinEng.value || '0'),
      limit: parseInt(contestLimit.value || '30', 10),
      recency_hours: parseInt(contestRecency.value || '72', 10),
      require_contest_keywords: contestStrict.value === '1',
    };

    const t0 = performance.now();
    try {
      const r = await fetch('/contests/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        throw new Error(data.detail || `Search failed (${r.status}).`);
      }

      const items = data.results || [];
      contestCountEl.textContent = String(items.length);
      contestPlural.textContent = items.length === 1 ? '' : 's';
      const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
      contestMeta.textContent = `${elapsed}s · ${data.mode || contestMode} mode`;

      contestList.innerHTML = '';
      if (items.length === 0) {
        contestResultsWrap.classList.add('hidden');
        contestEmpty.classList.remove('hidden');
      } else {
        contestEmpty.classList.add('hidden');
        contestResultsWrap.classList.remove('hidden');
        const frag = document.createDocumentFragment();
        items.forEach((c) => frag.appendChild(renderContestCard(c)));
        contestList.appendChild(frag);
      }
    } catch (err) {
      if (!silent) showContestError(err.message || 'Search failed.');
    } finally {
      if (!silent) setContestLoading(false);
    }
  }

  contestForm?.addEventListener('submit', (e) => {
    e.preventDefault();
    runContestSearch();
  });

  // ---- Auto-refresh (every 60s while toggle is on) ----
  contestAutoref?.addEventListener('change', () => {
    if (contestAutoTimer) {
      clearInterval(contestAutoTimer);
      contestAutoTimer = null;
    }
    if (contestAutoref.checked) {
      contestAutoTimer = setInterval(() => runContestSearch({ silent: true }), 60_000);
    }
  });

  // ===========================================================================
  // Haptics + sound feedback
  // Single mute toggle persisted to localStorage. Default: on (premium feel).
  // ===========================================================================
  const FX_KEY = 'rifat-ai:fx-muted';
  const isMuted = () => localStorage.getItem(FX_KEY) === '1';
  const setMuted = (v) => localStorage.setItem(FX_KEY, v ? '1' : '0');

  // Lazily-created shared AudioContext — browsers throttle them on first
  // load until a user gesture. We init on first call.
  let audioCtx = null;
  const getAudioCtx = () => {
    if (audioCtx) return audioCtx;
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return null;
    audioCtx = new Ctor();
    return audioCtx;
  };

  // Tiny synthesized "tick" — single sine ping with a fast decay envelope.
  // Bytes-free vs. shipping a WAV asset, and tweakable per-event.
  const playTick = (freq, duration, gain = 0.06) => {
    if (isMuted()) return;
    const ctx = getAudioCtx();
    if (!ctx) return;
    try {
      const osc = ctx.createOscillator();
      const env = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = freq;
      env.gain.value = 0;
      env.gain.linearRampToValueAtTime(gain, ctx.currentTime + 0.005);
      env.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + duration);
      osc.connect(env).connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + duration + 0.02);
    } catch (_) { /* ignore */ }
  };

  const buzz = (ms) => {
    if (isMuted()) return;
    if (navigator.vibrate) navigator.vibrate(ms);
  };

  const fx = {
    tap:     () => { buzz(8);  playTick(880, 0.06, 0.05); },   // generic press
    success: () => { buzz(15); playTick(880, 0.05, 0.06); setTimeout(() => playTick(1320, 0.07, 0.05), 60); }, // two-note up
    error:   () => { buzz([12, 60, 12]); playTick(220, 0.18, 0.07); },
    swoosh:  () => { buzz(6);  playTick(560, 0.05, 0.04); setTimeout(() => playTick(720, 0.05, 0.03), 35); }, // tab switch
    copy:    () => { buzz(10); playTick(1100, 0.04, 0.05); },
  };

  // ---- Wire fx into existing actions ----
  // 1. Tab switches (swoosh)
  tabGenerate?.addEventListener('click', () => fx.swoosh());
  tabContests?.addEventListener('click', () => fx.swoosh());

  // 2. Form submit success/error — hook progress completion via mutation
  //    on progressStats text. Cleaner than threading callbacks through.
  const progressStatsEl = $('progress-stats');
  if (progressStatsEl) {
    let lastText = '';
    new MutationObserver(() => {
      const t = progressStatsEl.textContent || '';
      if (t === lastText) return;
      lastText = t;
      const m = t.match(/^(\d+)\s+ok\s+·\s+(\d+)\s+failed$/);
      if (!m) return;
      const ok = parseInt(m[1], 10), bad = parseInt(m[2], 10);
      const total = ok + bad;
      const expected = parseInt(progressTotal?.textContent || '0', 10);
      // Only fire once per batch — when done count hits expected.
      if (total > 0 && total === expected) {
        if (bad === 0) fx.success();
        else if (ok === 0) fx.error();
        else fx.tap(); // partial — neutral
      }
    }).observe(progressStatsEl, { childList: true, characterData: true, subtree: true });
  }

  // 3. Copy buttons — delegate-fire on click anywhere in posts list
  postsList?.addEventListener('click', (e) => {
    if (e.target.closest('.copy-btn')) fx.copy();
  });

  // ===========================================================================
  // Keyboard shortcuts
  // Cmd/Ctrl + Enter      → submit Generate form (works inside textarea too)
  // Cmd/Ctrl + 1          → switch to Generate tab
  // Cmd/Ctrl + 2          → switch to Find Contests tab
  // R                     → regenerate the post card under cursor / focused
  // Esc                   → blur active input or close shortcut sheet
  // ?  (Shift + /)        → toggle the shortcut cheatsheet
  // ===========================================================================
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform || '');
  const modKey = (e) => (isMac ? e.metaKey : e.ctrlKey);

  // Update the inline kbd hint on the submit button to match platform.
  const kbdMod = document.getElementById('kbd-mod');
  if (kbdMod) kbdMod.textContent = isMac ? '⌘' : 'Ctrl';

  const cheatsheet = document.createElement('div');
  cheatsheet.id = 'shortcut-sheet';
  cheatsheet.className = 'hidden fixed inset-0 z-50 flex items-center justify-center px-4';
  cheatsheet.innerHTML = `
    <div class="absolute inset-0 bg-black/60 backdrop-blur-sm" data-close></div>
    <div class="relative glass rounded-3xl p-7 sm:p-8 max-w-md w-full shadow-card text-[14px]">
      <div class="flex items-center justify-between mb-5">
        <h3 class="text-[18px] font-semibold tracking-tight">Keyboard shortcuts</h3>
        <button type="button" data-close class="btn-ghost rounded-full px-2.5 py-1 text-[12px]">Esc</button>
      </div>
      <ul class="space-y-2.5">
        <li class="flex items-center justify-between gap-4">
          <span class="text-slate-300">Generate replies</span>
          <kbd class="kbd">${isMac ? '⌘' : 'Ctrl'}</kbd><kbd class="kbd">Enter</kbd>
        </li>
        <li class="flex items-center justify-between gap-4">
          <span class="text-slate-300">Switch to Generate tab</span>
          <kbd class="kbd">${isMac ? '⌘' : 'Ctrl'}</kbd><kbd class="kbd">1</kbd>
        </li>
        <li class="flex items-center justify-between gap-4">
          <span class="text-slate-300">Switch to Find Contests tab</span>
          <kbd class="kbd">${isMac ? '⌘' : 'Ctrl'}</kbd><kbd class="kbd">2</kbd>
        </li>
        <li class="flex items-center justify-between gap-4">
          <span class="text-slate-300">Switch to Tools tab</span>
          <kbd class="kbd">${isMac ? '⌘' : 'Ctrl'}</kbd><kbd class="kbd">3</kbd>
        </li>
        <li class="flex items-center justify-between gap-4">
          <span class="text-slate-300">Toggle this sheet</span>
          <kbd class="kbd">?</kbd>
        </li>
        <li class="flex items-center justify-between gap-4">
          <span class="text-slate-300">Mute / unmute sound + haptics</span>
          <kbd class="kbd">M</kbd>
        </li>
      </ul>
      <div class="mt-5 pt-4 border-t border-white/[0.06] flex items-center justify-between text-[12px]">
        <label class="flex items-center gap-2 text-slate-400 cursor-pointer">
          <input type="checkbox" id="fx-mute-toggle" class="accent-accent-500">
          Mute sound + haptics
        </label>
        <span class="text-slate-500">Press <kbd class="kbd">?</kbd> any time</span>
      </div>
    </div>`;
  document.body.appendChild(cheatsheet);

  const showSheet = () => {
    cheatsheet.classList.remove('hidden');
    document.getElementById('fx-mute-toggle').checked = isMuted();
  };
  const hideSheet = () => cheatsheet.classList.add('hidden');
  const sheetVisible = () => !cheatsheet.classList.contains('hidden');

  cheatsheet.addEventListener('click', (e) => {
    if (e.target.matches('[data-close]')) hideSheet();
  });
  document.getElementById('fx-mute-toggle')?.addEventListener('change', (e) => {
    setMuted(e.target.checked);
    if (!e.target.checked) fx.tap(); // confirm unmute
  });

  // Subtle floating ? button bottom-right so the shortcuts are discoverable.
  const helpBtn = document.createElement('button');
  helpBtn.type = 'button';
  helpBtn.id = 'shortcut-help-btn';
  helpBtn.title = 'Keyboard shortcuts (?)';
  helpBtn.className = 'fixed bottom-4 right-4 z-40 h-10 w-10 rounded-full glass shadow-lift text-slate-300 hover:text-white text-[16px] font-semibold transition-colors';
  helpBtn.textContent = '?';
  helpBtn.addEventListener('click', () => { fx.tap(); showSheet(); });
  document.body.appendChild(helpBtn);

  document.addEventListener('keydown', (e) => {
    // Don't intercept shortcuts when user is typing inside contenteditable
    // variants (lets them edit freely).
    const inEditable = e.target?.isContentEditable;

    if (e.key === 'Escape') {
      if (sheetVisible()) { hideSheet(); e.preventDefault(); return; }
      if (document.activeElement && document.activeElement !== document.body) {
        document.activeElement.blur();
      }
      return;
    }

    // Cmd/Ctrl + Enter → submit Generate form (even from inside textarea)
    if (modKey(e) && e.key === 'Enter') {
      e.preventDefault();
      // Only fire if Generate panel is visible.
      if (panelGenerate && !panelGenerate.classList.contains('hidden')) {
        fx.tap();
        form?.requestSubmit?.() || form?.submit?.();
      } else if (panelContests && !panelContests.classList.contains('hidden')) {
        fx.tap();
        contestForm?.requestSubmit?.() || contestForm?.submit?.();
      }
      return;
    }

    // Cmd/Ctrl + 1 / 2 / 3 → tab switch
    if (modKey(e) && (e.key === '1' || e.key === '2' || e.key === '3')) {
      e.preventDefault();
      const target = { '1': 'generate', '2': 'contests', '3': 'tools' }[e.key];
      switchTab(target);
      return;
    }

    // Single-letter shortcuts skipped while typing in plain inputs/contenteditable
    const tag = (e.target?.tagName || '').toLowerCase();
    const inText = inEditable || tag === 'input' || tag === 'textarea' || tag === 'select';
    if (inText) return;

    if (e.key === '?' || (e.shiftKey && e.key === '/')) {
      e.preventDefault();
      sheetVisible() ? hideSheet() : showSheet();
      fx.tap();
      return;
    }

    if (e.key === 'm' || e.key === 'M') {
      const next = !isMuted();
      setMuted(next);
      // Confirm with a tap if we just unmuted (otherwise muted = no feedback)
      if (!next) fx.tap();
      return;
    }
  });

  // ===========================================================================
  //  TOOLS PANEL — Image → PDF (offline, jsPDF) and Image → Text (Groq vision).
  // ===========================================================================

  // Lazy-load jsPDF the first time it's needed.
  const JSPDF_URL = 'https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.2/jspdf.umd.min.js';
  let _jsPDFPromise = null;
  function loadJsPDF() {
    if (window.jspdf?.jsPDF) return Promise.resolve(window.jspdf.jsPDF);
    if (_jsPDFPromise) return _jsPDFPromise;
    _jsPDFPromise = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = JSPDF_URL;
      s.async = true;
      s.onload = () => {
        if (window.jspdf?.jsPDF) resolve(window.jspdf.jsPDF);
        else reject(new Error('jsPDF failed to load'));
      };
      s.onerror = () => reject(new Error('Could not fetch jsPDF (offline?)'));
      document.head.appendChild(s);
    });
    return _jsPDFPromise;
  }

  // -------- File helpers --------
  const readFileAsDataURL = (file) => new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onerror = () => rej(new Error('Could not read ' + file.name));
    fr.onload = () => res(fr.result);
    fr.readAsDataURL(file);
  });

  const loadImage = (src) => new Promise((res, rej) => {
    const img = new Image();
    img.onload = () => res(img);
    img.onerror = () => rej(new Error('Image failed to load (unsupported format?)'));
    img.src = src;
  });

  const fmtBytes = (n) => {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(2)} MB`;
  };

  // Downscale an image to a target max edge, return base64 JPEG (smaller payload for OCR).
  async function downscaleImageToJpeg(file, maxEdge = 1600, quality = 0.85) {
    const dataUrl = await readFileAsDataURL(file);
    const img = await loadImage(dataUrl);
    const ratio = Math.min(1, maxEdge / Math.max(img.naturalWidth, img.naturalHeight));
    const w = Math.max(1, Math.round(img.naturalWidth * ratio));
    const h = Math.max(1, Math.round(img.naturalHeight * ratio));
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#ffffff';  // flatten transparency for JPEG
    ctx.fillRect(0, 0, w, h);
    ctx.drawImage(img, 0, 0, w, h);
    return canvas.toDataURL('image/jpeg', quality);
  }

  // ============================ Image → PDF ===================================
  const pdfDrop      = $('pdf-drop');
  const pdfFiles     = $('pdf-files');
  const pdfList      = $('pdf-list');
  const pdfActions   = $('pdf-actions');
  const pdfBuild     = $('pdf-build');
  const pdfPageSize  = $('pdf-page-size');
  const pdfOrient    = $('pdf-orientation');
  const pdfStatus    = $('pdf-status');
  const pdfError     = $('pdf-error');

  /** @type {{id:string,file:File,dataUrl:string,w:number,h:number}[]} */
  const pdfItems = [];
  let pdfDragId = null;

  const showPdfError = (msg) => {
    pdfError.textContent = msg;
    pdfError.classList.remove('hidden');
    setTimeout(() => pdfError.classList.add('hidden'), 6000);
  };
  const setPdfStatus = (msg) => {
    if (msg) {
      pdfStatus.textContent = msg;
      pdfStatus.classList.remove('hidden');
    } else {
      pdfStatus.classList.add('hidden');
    }
  };

  function renderPdfList() {
    pdfList.innerHTML = '';
    if (!pdfItems.length) {
      pdfList.classList.add('hidden');
      pdfActions.classList.add('hidden');
      pdfActions.classList.remove('flex');
      return;
    }
    pdfList.classList.remove('hidden');
    pdfActions.classList.remove('hidden');
    pdfActions.classList.add('flex');

    pdfItems.forEach((item, idx) => {
      const li = document.createElement('li');
      li.draggable = true;
      li.dataset.id = item.id;
      li.className = 'flex items-center gap-3 rounded-2xl border border-white/[0.07] bg-ink-800/60 p-3 cursor-grab active:cursor-grabbing transition-colors';
      li.innerHTML = `
        <span class="shrink-0 w-6 text-center text-[12px] tabular-nums text-slate-500">${idx + 1}</span>
        <img src="${item.dataUrl}" alt="" class="h-12 w-12 object-cover rounded-lg border border-white/10 shrink-0" />
        <div class="min-w-0 flex-1">
          <p class="text-[13px] text-slate-200 truncate">${item.file.name}</p>
          <p class="text-[11px] text-slate-500">${item.w}×${item.h} · ${fmtBytes(item.file.size)}</p>
        </div>
        <div class="flex items-center gap-1 shrink-0">
          <button type="button" data-action="up"     aria-label="Move up"     class="btn-ghost h-7 w-7 inline-flex items-center justify-center text-[14px]">↑</button>
          <button type="button" data-action="down"   aria-label="Move down"   class="btn-ghost h-7 w-7 inline-flex items-center justify-center text-[14px]">↓</button>
          <button type="button" data-action="remove" aria-label="Remove"      class="btn-ghost h-7 w-7 inline-flex items-center justify-center text-rose-300">×</button>
        </div>
      `;
      // Buttons
      li.querySelector('[data-action="up"]').addEventListener('click', () => movePdfItem(idx, idx - 1));
      li.querySelector('[data-action="down"]').addEventListener('click', () => movePdfItem(idx, idx + 1));
      li.querySelector('[data-action="remove"]').addEventListener('click', () => {
        pdfItems.splice(idx, 1);
        renderPdfList();
        fx.tap();
      });
      // Drag-reorder
      li.addEventListener('dragstart', () => { pdfDragId = item.id; li.classList.add('opacity-50'); });
      li.addEventListener('dragend',   () => { pdfDragId = null;    li.classList.remove('opacity-50'); });
      li.addEventListener('dragover',  (e) => { e.preventDefault(); });
      li.addEventListener('drop',      (e) => {
        e.preventDefault();
        if (!pdfDragId || pdfDragId === item.id) return;
        const from = pdfItems.findIndex((x) => x.id === pdfDragId);
        const to   = pdfItems.findIndex((x) => x.id === item.id);
        if (from < 0 || to < 0) return;
        const [moved] = pdfItems.splice(from, 1);
        pdfItems.splice(to, 0, moved);
        renderPdfList();
      });
      pdfList.appendChild(li);
    });
  }

  function movePdfItem(from, to) {
    if (to < 0 || to >= pdfItems.length) return;
    const [m] = pdfItems.splice(from, 1);
    pdfItems.splice(to, 0, m);
    renderPdfList();
    fx.tap();
  }

  async function addPdfFiles(fileList) {
    const files = Array.from(fileList || []).filter((f) => f.type.startsWith('image/'));
    if (!files.length) {
      showPdfError('No supported images in selection.');
      return;
    }
    let added = 0;
    for (const file of files) {
      if (file.size > 25 * 1024 * 1024) {
        showPdfError(`Skipped ${file.name} (over 25 MB).`);
        continue;
      }
      try {
        const dataUrl = await readFileAsDataURL(file);
        const img = await loadImage(dataUrl);
        pdfItems.push({
          id: 'img_' + Math.random().toString(36).slice(2),
          file, dataUrl,
          w: img.naturalWidth, h: img.naturalHeight,
        });
        added += 1;
      } catch (err) {
        showPdfError(`${file.name}: ${err.message}`);
      }
    }
    if (added) {
      renderPdfList();
      setPdfStatus(`${pdfItems.length} image${pdfItems.length === 1 ? '' : 's'} ready.`);
      fx.tap();
    }
  }

  pdfFiles?.addEventListener('change', (e) => {
    addPdfFiles(e.target.files);
    e.target.value = '';  // allow re-adding same file
  });
  ['dragenter', 'dragover'].forEach((ev) => {
    pdfDrop?.addEventListener(ev, (e) => {
      e.preventDefault(); e.stopPropagation();
      pdfDrop.classList.add('border-accent-500/60', 'bg-ink-800/70');
    });
  });
  ['dragleave', 'drop'].forEach((ev) => {
    pdfDrop?.addEventListener(ev, (e) => {
      e.preventDefault(); e.stopPropagation();
      pdfDrop.classList.remove('border-accent-500/60', 'bg-ink-800/70');
    });
  });
  pdfDrop?.addEventListener('drop', (e) => {
    if (e.dataTransfer?.files?.length) addPdfFiles(e.dataTransfer.files);
  });

  pdfBuild?.addEventListener('click', async () => {
    if (!pdfItems.length) return;
    pdfBuild.disabled = true;
    setPdfStatus('Building PDF…');
    try {
      const jsPDF = await loadJsPDF();
      const pageMode = pdfPageSize.value;       // 'a4' | 'letter' | 'fit'
      const orientPref = pdfOrient.value;        // 'auto' | 'portrait' | 'landscape'

      // mm dimensions
      const PAGES = {
        a4:     { p: [210, 297], l: [297, 210] },
        letter: { p: [216, 279], l: [279, 216] },
      };

      // Decide first page layout from first image so jsPDF gets a consistent format.
      const first = pdfItems[0];
      const firstLandscape = first.w > first.h;
      let firstFmt, firstOrient;
      if (pageMode === 'fit') {
        // Use pixels → mm at 96dpi to keep image native size on page.
        const pxToMm = 25.4 / 96;
        firstFmt = [first.w * pxToMm, first.h * pxToMm];
        firstOrient = firstLandscape ? 'l' : 'p';
      } else {
        const pages = PAGES[pageMode];
        firstOrient = orientPref === 'auto' ? (firstLandscape ? 'l' : 'p') : orientPref[0];
        firstFmt = pages[firstOrient];
      }
      const pdf = new jsPDF({ unit: 'mm', format: firstFmt, orientation: firstOrient === 'l' ? 'landscape' : 'portrait' });

      const drawImage = (item, isFirst) => {
        const landscape = item.w > item.h;
        let pageW, pageH;
        if (pageMode === 'fit') {
          const pxToMm = 25.4 / 96;
          pageW = item.w * pxToMm;
          pageH = item.h * pxToMm;
          if (!isFirst) pdf.addPage([pageW, pageH], landscape ? 'landscape' : 'portrait');
        } else {
          const pages = PAGES[pageMode];
          const orient = orientPref === 'auto' ? (landscape ? 'l' : 'p') : orientPref[0];
          [pageW, pageH] = pages[orient];
          if (!isFirst) pdf.addPage(pages[orient], orient === 'l' ? 'landscape' : 'portrait');
        }
        // Fit-with-margin
        const margin = pageMode === 'fit' ? 0 : 8;
        const maxW = pageW - margin * 2;
        const maxH = pageH - margin * 2;
        const imgRatio = item.w / item.h;
        const pageRatio = maxW / maxH;
        let drawW, drawH;
        if (imgRatio > pageRatio) { drawW = maxW; drawH = maxW / imgRatio; }
        else                       { drawH = maxH; drawW = maxH * imgRatio; }
        const x = (pageW - drawW) / 2;
        const y = (pageH - drawH) / 2;
        // jsPDF will infer format from data URL prefix.
        pdf.addImage(item.dataUrl, undefined, x, y, drawW, drawH, undefined, 'FAST');
      };

      pdfItems.forEach((item, i) => drawImage(item, i === 0));
      const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
      pdf.save(`rifat-ai-${stamp}.pdf`);
      fx.success();
      setPdfStatus(`Saved · ${pdfItems.length} page${pdfItems.length === 1 ? '' : 's'}.`);
    } catch (err) {
      showPdfError('PDF build failed: ' + err.message);
      fx.error();
    } finally {
      pdfBuild.disabled = false;
    }
  });

  // ============================ Image → Text (OCR) ============================
  const ocrDrop       = $('ocr-drop');
  const ocrFile       = $('ocr-file');
  const ocrPreviewWrap= $('ocr-preview-wrap');
  const ocrPreview    = $('ocr-preview');
  const ocrFilename   = $('ocr-filename');
  const ocrFilesize   = $('ocr-filesize');
  const ocrClear      = $('ocr-clear');
  const ocrExtract    = $('ocr-extract');
  const ocrExtractLbl = $('ocr-extract-label');
  const ocrExtractSpn = $('ocr-extract-spinner');
  const ocrResultWrap = $('ocr-result-wrap');
  const ocrResult     = $('ocr-result');
  const ocrCopy       = $('ocr-copy');
  const ocrDownload   = $('ocr-download');
  const ocrToGenerate = $('ocr-to-generate');
  const ocrStatus     = $('ocr-status');
  const ocrError      = $('ocr-error');

  let ocrCurrentFile = null;
  let ocrCurrentDataUrl = null;

  const showOcrError = (msg) => {
    ocrError.textContent = msg;
    ocrError.classList.remove('hidden');
    setTimeout(() => ocrError.classList.add('hidden'), 7000);
  };
  const setOcrStatus = (msg) => {
    if (msg) { ocrStatus.textContent = msg; ocrStatus.classList.remove('hidden'); }
    else     { ocrStatus.classList.add('hidden'); }
  };
  const setOcrLoading = (on) => {
    ocrExtract.disabled = on;
    ocrExtractLbl.classList.toggle('hidden', on);
    ocrExtractSpn.classList.toggle('hidden', !on);
  };

  function setOcrFile(file) {
    if (!file || !file.type.startsWith('image/')) {
      showOcrError('Please pick an image file.');
      return;
    }
    if (file.size > 12 * 1024 * 1024) {
      showOcrError('Image is too large (max 12 MB before resize).');
      return;
    }
    ocrCurrentFile = file;
    ocrCurrentDataUrl = null;
    ocrFilename.textContent = file.name || 'pasted image';
    ocrFilesize.textContent = `${file.type || 'image'} · ${fmtBytes(file.size)}`;
    const blobUrl = URL.createObjectURL(file);
    ocrPreview.src = blobUrl;
    ocrPreviewWrap.classList.remove('hidden');
    ocrResultWrap.classList.add('hidden');
    setOcrStatus('');
    fx.tap();
  }

  ocrFile?.addEventListener('change', (e) => {
    if (e.target.files?.[0]) setOcrFile(e.target.files[0]);
    e.target.value = '';
  });
  ocrClear?.addEventListener('click', () => {
    ocrCurrentFile = null;
    ocrCurrentDataUrl = null;
    ocrPreviewWrap.classList.add('hidden');
    ocrResultWrap.classList.add('hidden');
  });
  ['dragenter', 'dragover'].forEach((ev) => {
    ocrDrop?.addEventListener(ev, (e) => {
      e.preventDefault(); e.stopPropagation();
      ocrDrop.classList.add('border-accent-500/60', 'bg-ink-800/70');
    });
  });
  ['dragleave', 'drop'].forEach((ev) => {
    ocrDrop?.addEventListener(ev, (e) => {
      e.preventDefault(); e.stopPropagation();
      ocrDrop.classList.remove('border-accent-500/60', 'bg-ink-800/70');
    });
  });
  ocrDrop?.addEventListener('drop', (e) => {
    if (e.dataTransfer?.files?.[0]) setOcrFile(e.dataTransfer.files[0]);
  });

  // Clipboard paste — only when Tools panel is visible.
  document.addEventListener('paste', (e) => {
    if (!panelTools || panelTools.classList.contains('hidden')) return;
    const items = e.clipboardData?.items || [];
    for (const it of items) {
      if (it.type?.startsWith('image/')) {
        const f = it.getAsFile();
        if (f) {
          e.preventDefault();
          setOcrFile(f);
          break;
        }
      }
    }
  });

  ocrExtract?.addEventListener('click', async () => {
    if (!ocrCurrentFile) return;
    setOcrLoading(true);
    setOcrStatus('Resizing image…');
    try {
      if (!ocrCurrentDataUrl) {
        ocrCurrentDataUrl = await downscaleImageToJpeg(ocrCurrentFile, 1600, 0.85);
      }
      setOcrStatus('Extracting text with Groq vision…');
      const res = await fetch('/ocr', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ image: ocrCurrentDataUrl }),
      });
      if (!res.ok) {
        let msg = `OCR failed (HTTP ${res.status})`;
        try { const j = await res.json(); if (j.detail) msg = j.detail; } catch {}
        throw new Error(msg);
      }
      const data = await res.json();
      const text = (data.text || '').trim();
      ocrResult.value = text || '(No readable text found in this image.)';
      ocrResultWrap.classList.remove('hidden');
      // Show "Generate replies for this" only if it looks like there's content.
      ocrToGenerate.classList.toggle('hidden', !text);
      setOcrStatus(text ? `Extracted ${text.length.toLocaleString()} chars.` : '');
      fx.success();
    } catch (err) {
      showOcrError(err.message || String(err));
      fx.error();
      setOcrStatus('');
    } finally {
      setOcrLoading(false);
    }
  });

  ocrCopy?.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(ocrResult.value || '');
      ocrCopy.querySelector('span').textContent = 'Copied';
      fx.tap();
      setTimeout(() => { ocrCopy.querySelector('span').textContent = 'Copy'; }, 1500);
    } catch {
      showOcrError('Clipboard copy was blocked. Select the text and copy manually.');
    }
  });

  ocrDownload?.addEventListener('click', () => {
    const blob = new Blob([ocrResult.value || ''], { type: 'text/plain;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `rifat-ai-ocr-${Date.now()}.txt`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    fx.tap();
  });

  ocrToGenerate?.addEventListener('click', () => {
    const text = (ocrResult.value || '').trim();
    if (!text) return;
    // Look for an X URL in the OCR'd text — if found, send that to Generate.
    const m = text.match(/https?:\/\/(?:x|twitter)\.com\/[^\s]+\/status\/\d+/i);
    if (m) {
      switchTab('generate');
      urlsInput.value = m[0];
      urlsInput.dispatchEvent(new Event('input', { bubbles: true }));
      window.scrollTo({ top: 0, behavior: 'smooth' });
      setTimeout(() => form?.requestSubmit?.(), 200);
    } else {
      // No URL — copy the text to clipboard and tell the user.
      navigator.clipboard.writeText(text).catch(() => {});
      showOcrError('No X post URL detected in the text. Copied the text to your clipboard instead — paste it into the URL field on Generate Replies, or paste a tweet URL.');
    }
  });
})();
