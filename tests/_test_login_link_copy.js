/* The login-link copy control: wiring and localization, proven by behaviour.

   This replaces two exact page-substring assertions:

     html.includes('onclick="copyLoginLink()">复制登录链接</button>')
     html.includes("'复制登录链接': 'Copy sign-in link'")

   The first is brittle in one direction and blind in the other. Adding
   `type="button"`, a class, reordering attributes or reflowing whitespace breaks
   it although nothing about the control changed; and a decoy literal left
   anywhere else on the page keeps it green while the real login control loses
   its handler. The second pins how the dictionary is written in the source
   rather than what the user sees in English.

   So this suite drives the shipped page instead. It runs the real page script on
   the shared fake DOM (tests/_dom_stub.js), asks the page to render the
   login-link box, pulls the copy control out of the markup the page itself
   produced, and then *activates that control's own inline expression in the
   page's scope* - which is what a click does - and checks what reaches the
   clipboard. The English label is read back from the page's own localization
   pass over a text node, not from the dictionary text.

   The direct copyLoginLink() behaviour tests (success, failure, current href)
   are unchanged below.

   Run with Node: node tests/_test_login_link_copy.js
*/
const assert = require('assert');
const { dashboardHtml, dashboardScript } = require('./_dashboard_source.js');

// One shared fake DOM for every dashboard suite: tests/_dom_stub.js.
const dom = require('./_dom_stub.js');

const COPY_LABEL = '复制登录链接';
const COPY_LABEL_EN = 'Copy sign-in link';

// What GET /accounts/login/start answers; the second render regenerates it.
let authUrl = 'https://example.com/auth?state=first&redirect=%2Flogin';
const clipboard = [];

const installed = dom.installDom({
  fetch: url => {
    const target = String(url);
    const payload = target.indexOf('/accounts/login/start') !== -1
      ? { state: 'state-1', authUrl: authUrl, realm: 'intl', platform: 'CLI' }
      : { status: 'pending', message: 'waiting for token' };
    return Promise.resolve({ status: 200, ok: true,
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload)) });
  },
});
// writeClipboard() takes navigator.clipboard in a secure context and falls back
// to a textarea otherwise; take the clipboard branch so the copy is observable
// without modelling a textarea.
installed.window.isSecureContext = true;
installed.window.navigator.clipboard = { writeText: async text => { clipboard.push(text); } };

const api = new Function(dashboardScript() + `
  window.updateUI = updateUI;
  return {
    renderLoginLink: triggerLoginForSelectedRealm,
    translate: text => window.WB_I18N.translate(text),
    setLanguage: lang => window.WB_I18N.set(lang),
    // A click evaluates the attribute's expression in the page's global scope,
    // where copyLoginLink is defined. This is that evaluation.
    activate: expression => eval(expression),
  };`)();

/* The control, parsed out of the markup the page produced. Attribute order, extra
   attributes, classes and whitespace make no difference here. */
function parseButtons(markup) {
  return [...String(markup).matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)].map(match => {
    const attrs = match[1];
    const handlers = {};
    for (const hit of attrs.matchAll(/\son([a-z]+)\s*=\s*"([^"]*)"/g)) handlers[hit[1]] = hit[2];
    return {
      attrs,
      handlers,
      label: match[2].replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim(),
      hidden: /\shidden\b/.test(attrs) || /display\s*:\s*none/.test(attrs),
    };
  });
}

function copyControlIn(markup) {
  const controls = parseButtons(markup).filter(button => button.label === COPY_LABEL);
  assert.strictEqual(controls.length, 1,
    'the login-link area must expose exactly one ' + COPY_LABEL + ' control, saw: '
    + parseButtons(markup).map(button => JSON.stringify(button.label)).join(', '));
  assert.ok(!controls[0].hidden, 'the copy control must be visible');
  assert.ok(controls[0].handlers.click,
    'the copy control must carry a click handler: ' + controls[0].attrs);
  return controls[0];
}

/* The stub keeps innerHTML as text, so an attribute value arrives HTML-escaped;
   a real DOM parser decodes it on the way in. Decode what esc() can emit. */
function decodeAttr(text) {
  return String(text).replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
}

function loginHrefIn(markup) {
  const anchor = String(markup).match(/<a\b[^>]*\bid\s*=\s*"loginLink"[^>]*>/);
  assert.ok(anchor, 'the login-link area must carry the #loginLink anchor');
  const href = anchor[0].match(/\shref\s*=\s*"([^"]*)"/);
  assert.ok(href, 'the #loginLink anchor must carry an href: ' + anchor[0]);
  return decodeAttr(href[1]);
}

/* The stub cannot parse markup into nodes, so the anchor the page rendered is
   materialised by id: this is the element copyLoginLink() reads. */
function loginLinkElement(href) {
  const link = document.getElementById('loginLink');
  link.setAttribute('href', href);
  return link;
}

// Part 1 runs first; the behaviour tests below await it so the two cannot
// interleave their output.
const wiringAndLocalization = (async () => {
  // 1. The real login flow renders the control.
  await api.renderLoginLink();
  const box = dom.byId('loginLinkBox');
  const control = copyControlIn(box.innerHTML);

  // 2. Activating that control reaches the copy behaviour and copies the href the
  //    page just rendered.
  const rendered = loginHrefIn(box.innerHTML);
  loginLinkElement(rendered);
  clipboard.length = 0;
  await api.activate(control.handlers.click);
  assert.deepStrictEqual(clipboard, [rendered],
    'activating the rendered control must copy the current #loginLink href');

  // 3. A regenerated link is what the control copies next: the wiring is live, not
  //    a value captured when the markup was written.
  authUrl = 'https://example.com/auth?state=second&redirect=%2Flogin';
  await api.renderLoginLink();
  const regenerated = loginHrefIn(box.innerHTML);
  assert.notStrictEqual(regenerated, rendered, 'the renderer must publish the new link');
  loginLinkElement(regenerated);
  clipboard.length = 0;
  await api.activate(copyControlIn(box.innerHTML).handlers.click);
  assert.deepStrictEqual(clipboard, [regenerated]);

  // 4. The English label for that control comes from the page's localization pass
  //    over a text node - the path the user sees - not from the dictionary text.
  const labelNode = document.createTextNode(control.label);
  const holder = dom.createElement('button');
  holder.appendChild(labelNode);
  const container = dom.createElement('div');
  container.appendChild(holder);
  document.documentElement.appendChild(container);
  // The i18n pass keeps its bookkeeping on the nodes it touches (node.__wbAttr).
  // A real DOM allows those expandos; the shared stub throws on reads of members
  // it has not seen, so seed the objects the page expects to find.
  for (const element of [document.documentElement, container, holder]) element.__wbAttr = {};
  api.setLanguage('en');
  assert.strictEqual(labelNode.data, COPY_LABEL_EN,
    'the page must render the control as "' + COPY_LABEL_EN + '" in English');
  assert.strictEqual(api.translate(control.label), COPY_LABEL_EN);
})();

const html = dashboardHtml();
const start = html.indexOf('async function copyLoginLink(){');
const end = html.indexOf('async function pollLogin(){', start);
assert.ok(start > 0 && end > start);
const source = html.slice(start, end);

(async () => {
  await wiringAndLocalization;
  let url = 'https://example.com/auth?state=first&redirect=%2Flogin';
  let link = { getAttribute: name => { assert.strictEqual(name, 'href'); return url; } };
  let failure = null;
  const copied = [], toasts = [];
  const copy = new Function('document', 'writeClipboard', 'toast',
    source + '\nreturn copyLoginLink;')(
    { getElementById: id => { assert.strictEqual(id, 'loginLink'); return link; } },
    async value => { if (failure) throw failure; copied.push(value); },
    (message, kind) => toasts.push({ message, kind })
  );

  await copy();
  assert.deepStrictEqual(copied, [url]);
  assert.deepStrictEqual(toasts.pop(), { message: '已复制到剪贴板', kind: 'ok' });

  // A regenerated link must replace the value copied on the next click.
  url = 'https://example.com/auth?state=second&redirect=%2Flogin';
  await copy();
  assert.strictEqual(copied[1], url);
  toasts.length = 0;

  failure = new Error('clipboard denied');
  await copy();
  assert.strictEqual(copied.length, 2);
  assert.deepStrictEqual(toasts.pop(), { message: '复制失败: clipboard denied', kind: 'bad' });

  link = null;
  await copy();
  assert.strictEqual(copied.length, 2);
  assert.strictEqual(toasts.length, 0);
  console.log('login link copy assertions passed');
})().catch(err => { console.error(err); process.exit(1); });
