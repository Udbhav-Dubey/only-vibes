// ==UserScript==
// @name         Desk Buddy: LeetCode + Codeforces
// @namespace    desk-buddy
// @version      1.0
// @description  Tells Desk Buddy which problem you're on and how your submissions go (accepted / wrong answer / TLE ...)
// @match        https://leetcode.com/*
// @match        https://codeforces.com/*
// @match        https://*.codeforces.com/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==
//
// Install: Violentmonkey or Tampermonkey -> new script -> paste this file. Desk Buddy must be running on
// this computer (port 3030). It only sends the site, the problem name and the verdict word, nothing else.
(function () {
  'use strict';
  const BUDDY = 'http://127.0.0.1:3030/event';   // change the port if you run the server on another one
  const DEBUG = false;                           // true: log what is seen/sent to the browser console
  const log = (...a) => { if (DEBUG) console.log('[buddy]', ...a); };

  function send(ev) {
    const body = JSON.stringify(ev);
    log('send', body);
    try {
      GM_xmlhttpRequest({ method: 'POST', url: BUDDY, data: body, headers: { 'Content-Type': 'application/json' }, timeout: 3000 });
      return;
    } catch (_) { /* no GM_ API: fall back to a plain request below */ }
    try { fetch(BUDDY, { method: 'POST', mode: 'no-cors', body }).catch(() => {}); } catch (_) {}
  }

  const isLC = /(^|\.)leetcode\.com$/.test(location.hostname);
  const site = isLC ? 'leetcode' : 'codeforces';

  // ---- which problem are we on? ----
  function cfId(href) {
    const m = (href || '').match(/(?:contest|gym)\/(\d+)\/problem\/(\w+)|problemset\/problem\/(\d+)\/(\w+)/);
    return m ? (m[1] || m[3]) + (m[2] || m[4]) : '';
  }
  function problemTitle() {
    if (isLC) {
      const m = location.pathname.match(/^\/problems\/([^/]+)/);
      if (!m) return '';
      const t = document.title.replace(/\s*[-|–—].*$/, '').replace(/^\d+\.\s*/, '').trim();
      return t || m[1].replace(/-/g, ' ');
    }
    const id = cfId(location.pathname);
    if (!id) return '';
    const el = document.querySelector('.problem-statement .header .title');
    const name = el ? el.textContent.trim().replace(/^\w+\.\s*/, '') : '';
    return (id + ' ' + name).trim();
  }

  // ---- heartbeat: lets Buddy show the problem + a timer while this tab is in front ----
  let lastSent = 0, lastKey = null;
  function beat(force) {
    if (document.visibilityState !== 'visible') return;
    const title = problemTitle(), now = Date.now();
    if (force || title !== lastKey || now - lastSent > 15000) {
      lastSent = now; lastKey = title;
      send({ type: 'site', site, title });
    }
  }

  // ---- verdicts ----
  let lastEmit = { key: '', at: 0 };
  function emit(verdict, title) {
    const key = verdict + '|' + title, now = Date.now();
    if (key === lastEmit.key && now - lastEmit.at < 3000) return;
    lastEmit = { key, at: now };
    send({ type: 'judge', site, verdict, title: title || problemTitle() });
  }

  // LeetCode: the result banner after "Submit" (selector may change if LeetCode redesigns the page)
  const LC = [[/^accepted$/i, 'accepted'], [/^wrong answer$/i, 'wrong'], [/^time limit exceeded$/i, 'tle'],
              [/^output limit exceeded$/i, 'tle'], [/^memory limit exceeded$/i, 'mle'], [/^runtime error$/i, 'error'],
              [/^compile error$/i, 'ce']];
  let lcEl = null, lcText = null, lcSeeded = false;
  function checkLeetCode() {
    const el = document.querySelector('[data-e2e-locator="submission-result"]');
    if (!el) { lcEl = null; lcText = null; lcSeeded = true; return; }
    const text = el.textContent.trim();
    if (el === lcEl && text === lcText) return;
    const first = !lcSeeded;                                  // a result already on the page at load is old news
    lcEl = el; lcText = text; lcSeeded = true;
    if (first) return;
    for (const [re, v] of LC) if (re.test(text)) { emit(v, problemTitle()); return; }
  }

  // Codeforces: rows of the submissions table; react when a row goes pending -> final, or a new final row appears
  function cfVerdict(text) {
    if (/running|in queue|judging|pending|waiting/i.test(text)) return 'pending';
    if (/compilation error/i.test(text)) return 'ce';
    if (/wrong answer/i.test(text)) return 'wrong';
    if (/time limit|idleness/i.test(text)) return 'tle';
    if (/memory limit/i.test(text)) return 'mle';
    if (/runtime error/i.test(text)) return 'error';
    if (/accepted|pretests passed/i.test(text)) return 'accepted';
    return '';
  }
  function cfRowTitle(row) {
    const a = row.querySelector('a[href*="/problem/"]');
    if (!a) return '';
    const name = a.textContent.split('-').slice(1).join('-').trim();
    return (cfId(a.getAttribute('href')) + ' ' + name).trim();
  }
  const cfState = new Map();
  let cfSeeded = false, cfMax = 0;
  try { cfMax = Number(localStorage.getItem('buddyCfMax') || 0); } catch (_) {}
  function checkCodeforces() {
    let max = cfMax;
    document.querySelectorAll('tr[data-submission-id]').forEach(row => {
      const id = row.getAttribute('data-submission-id'), n = Number(id);
      const cell = row.querySelector('.status-verdict-cell') || row.querySelector('.submissionVerdictWrapper');
      if (!cell) return;
      const v = cfVerdict(cell.textContent.trim()), prev = cfState.get(id);
      cfState.set(id, v);
      const isNew = cfSeeded ? prev !== v : (cfMax > 0 && n > cfMax);   // at load: only rows newer than the last one we saw
      if (isNew && v && v !== 'pending') emit(v, cfRowTitle(row));
      if (n > max) max = n;
    });
    cfSeeded = true;
    if (max > cfMax) { cfMax = max; try { localStorage.setItem('buddyCfMax', String(max)); } catch (_) {} }
  }

  function check() { if (isLC) checkLeetCode(); else checkCodeforces(); }

  if (window.__BUDDY_TEST__) { window.__buddyTest = { check, beat, problemTitle, cfVerdict }; }
  beat(true); check();
  setInterval(() => beat(false), 2000);                       // also notices LeetCode's in-page navigation
  setInterval(check, 1000);
  try { new MutationObserver(() => setTimeout(check, 120)).observe(document.body, { childList: true, subtree: true, characterData: true }); } catch (_) {}
  document.addEventListener('visibilitychange', () => beat(true));
})();
