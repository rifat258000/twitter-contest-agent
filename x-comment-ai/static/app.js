// static/app.js
// Vanilla JS controller for X Comment AI (Tier-1 enhanced).
//
// Features:
// - Multiple variants rendered from a <template>
// - Tone / length / language selectors
// - Regenerate button (calls /regenerate without re-scraping)
// - Live char counter (warn at 240, danger at 280) per variant
// - Edit-in-place via contenteditable
// - Open reply on X deep-link with current edited text
// - localStorage history (last 10 generations)

(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);

  // ---------- DOM refs ----------
  const form         = $('generate-form');
  const urlInput     = $('url');
  const langSelect   = $('lang');
  const toneSelect   = $('tone');
  const lengthSelect = $('length');
  const submitBtn    = $('submit-btn');
  const btnLabel     = $('btn-label');
  const btnSpinner   = $('btn-spinner');
  const errorBox     = $('error-box');
  const errorText    = $('error-text');
  const results      = $('results');

  const postAuthor   = $('post-author-name');
  const postHandle   = $('post-author-handle');
  const postLink     = $('post-link');
  const postText     = $('post-text');
  const threadWrap   = $('thread-wrap');
  const threadList   = $('thread-list');

  const variantsEl   = $('variants');
  const variantTpl   = $('variant-template');
  const regenBtn     = $('regenerate-btn');
  const regenLabel   = $('regen-label');

  const historyWrap  = $('history-wrap');
  const historyList  = $('history-list');
  const historyClear = $('history-clear');

  const toast        = $('toast');

  // ---------- constants ----------
  const MAX_POST_PREVIEW = 600;
  const HISTORY_KEY = 'xcommentai.history.v1';
  const HISTORY_LIMIT = 10;
  const X_LIMIT = 280;
  const X_WARN  = 240;

  // ---------- state ----------
  /** @type {{tweet_id:number,url:string,author:string,author_name:string,original:string,thread:string[]}|null} */
  let currentPost = null;

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
    urlInput.disabled = on;
    langSelect.disabled = on;
    toneSelect.disabled = on;
    lengthSelect.disabled = on;
    btnLabel.classList.toggle('hidden', on);
    btnSpinner.classList.toggle('hidden', on);
    btnSpinner.classList.toggle('inline-flex', on);
  };

  const setRegenLoading = (on) => {
    regenBtn.disabled = on;
    regenLabel.textContent = on ? 'Regenerating…' : 'Regenerate';
  };

  const truncate = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + '…' : s);

  const showToast = (msg) => {
    toast.textContent = msg;
    toast.classList.remove('hidden');
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => toast.classList.add('hidden'), 1600);
  };

  const buildReplyUrl = (text, tweetId) => {
    const params = new URLSearchParams({ text: text || '' });
    if (tweetId) params.set('in_reply_to', String(tweetId));
    return `https://x.com/intent/post?${params.toString()}`;
  };

  // ---------- variant rendering ----------
  const updateCounter = (counterEl, len) => {
    counterEl.textContent = `${len} / ${X_LIMIT}`;
    counterEl.classList.toggle('warn', len > X_WARN && len <= X_LIMIT);
    counterEl.classList.toggle('danger', len > X_LIMIT);
  };

  const renderVariant = (text, idx) => {
    const node = variantTpl.content.firstElementChild.cloneNode(true);

    const badge   = node.querySelector('.variant-badge');
    const textEl  = node.querySelector('.variant-text');
    const counter = node.querySelector('.variant-counter');
    const copyBtn = node.querySelector('.copy-btn');
    const copyLbl = node.querySelector('.copy-label');
    const replyBtn = node.querySelector('.reply-btn');

    badge.textContent = `Variant ${idx + 1}`;
    textEl.textContent = text;
    textEl.dataset.placeholder = 'Write your reply…';
    updateCounter(counter, text.length);

    const refreshReply = () => {
      replyBtn.href = buildReplyUrl(textEl.textContent || '', currentPost?.tweet_id);
    };
    refreshReply();

    textEl.addEventListener('input', () => {
      updateCounter(counter, (textEl.textContent || '').length);
      refreshReply();
    });

    copyBtn.addEventListener('click', async () => {
      const txt = textEl.textContent || '';
      if (!txt.trim()) return;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(txt);
        } else {
          const ta = document.createElement('textarea');
          ta.value = txt;
          ta.setAttribute('readonly', '');
          ta.style.position = 'fixed';
          ta.style.opacity = '0';
          document.body.appendChild(ta);
          ta.select();
          document.execCommand('copy');
          document.body.removeChild(ta);
        }
        copyLbl.textContent = 'Copied!';
        showToast('Copied to clipboard');
        setTimeout(() => { copyLbl.textContent = 'Copy'; }, 1600);
        // Mark this variant as "used" in history
        markUsed(currentPost?.tweet_id, txt);
      } catch (err) {
        console.error(err);
        showError('Could not copy automatically. Please select and copy manually.');
      }
    });

    return node;
  };

  const renderVariants = (variants) => {
    variantsEl.innerHTML = '';
    variants.forEach((v, i) => variantsEl.appendChild(renderVariant(v, i)));
  };

  const renderPost = (data) => {
    postAuthor.textContent = data.author_name || '';
    postHandle.textContent = data.author || '';
    postLink.href          = data.url || '#';
    postText.textContent   = truncate(data.original || '', MAX_POST_PREVIEW);

    threadList.innerHTML = '';
    if (Array.isArray(data.thread) && data.thread.length > 0) {
      data.thread.forEach((t) => {
        const li = document.createElement('li');
        li.className = 'whitespace-pre-wrap';
        li.textContent = truncate(t, 280);
        threadList.appendChild(li);
      });
      threadWrap.classList.remove('hidden');
    } else {
      threadWrap.classList.add('hidden');
    }
  };

  const showResults = () => {
    results.classList.remove('hidden');
    requestAnimationFrame(() => {
      results.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  };

  // ---------- history (localStorage) ----------
  const loadHistory = () => {
    try {
      return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
    } catch {
      return [];
    }
  };
  const saveHistory = (items) => {
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(items.slice(0, HISTORY_LIMIT)));
    } catch { /* quota — ignore */ }
  };

  const pushHistory = (entry) => {
    const items = loadHistory();
    // Replace any existing entry with the same tweet_id
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
        currentPost = {
          tweet_id: it.tweet_id,
          url: it.url,
          author: it.author,
          author_name: it.author_name,
          original: it.original,
          thread: it.thread || [],
        };
        renderPost(currentPost);
        renderVariants(it.variants || []);
        showResults();
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

  // ---------- submit (generate) ----------
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearError();

    const url = urlInput.value.trim();
    if (!url) {
      showError('Please paste an X post URL.');
      return;
    }

    setLoading(true);
    results.classList.add('hidden');

    try {
      const data = await callJSON('/generate', {
        url,
        lang:   langSelect.value || 'auto',
        tone:   toneSelect.value || 'witty',
        length: lengthSelect.value || 'medium',
        n: 3,
      });

      currentPost = {
        tweet_id: data.tweet_id,
        url: data.url,
        author: data.author,
        author_name: data.author_name,
        original: data.original,
        thread: data.thread || [],
      };

      renderPost(currentPost);
      renderVariants(data.variants || []);
      showResults();

      pushHistory({
        ...currentPost,
        variants: data.variants || [],
        at: Date.now(),
      });
    } catch (err) {
      console.error(err);
      showError(err.message || 'Something went wrong.');
    } finally {
      setLoading(false);
    }
  });

  // ---------- regenerate ----------
  regenBtn.addEventListener('click', async () => {
    if (!currentPost) return;
    clearError();
    setRegenLoading(true);
    try {
      const data = await callJSON('/regenerate', {
        original: currentPost.original,
        thread:   currentPost.thread,
        lang:     langSelect.value || 'auto',
        tone:     toneSelect.value || 'witty',
        length:   lengthSelect.value || 'medium',
        n: 3,
      });
      renderVariants(data.variants || []);
      // Refresh history entry with new variants
      pushHistory({
        ...currentPost,
        variants: data.variants || [],
        at: Date.now(),
      });
    } catch (err) {
      console.error(err);
      showError(err.message || 'Regenerate failed.');
    } finally {
      setRegenLoading(false);
    }
  });

  // ---------- UX niceties ----------
  // Auto-paste an X URL from clipboard when focusing the empty input
  urlInput.addEventListener('focus', async () => {
    if (urlInput.value.trim()) return;
    try {
      const text = await navigator.clipboard.readText();
      if (text && /(?:x\.com|twitter\.com)\/[^/]+\/status\/\d+/i.test(text)) {
        urlInput.value = text.trim();
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
  renderHistory();
})();
