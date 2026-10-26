/* The shipped dashboard page, and the source of the scripts it ships.
 *
 * dashboard.html keeps two tiny first-paint scripts inline in <head> and loads
 * everything else from assets/js/*.js and assets/css/*.css, in document order.
 * Every dashboard suite needs the same two things: the page with its code, and
 * that code joined the way the browser runs it. This module is that, and
 * nothing more: the fake DOM, the globals, the fetch stubs and the evaluation
 * stay in each suite, because those differ per suite.
 *
 *   const {dashboardScript} = require('./_dashboard_source.js');
 *
 * dashboardHtml() returns the page with each asset inlined back at the spot its
 * tag stands (as <style>/<script> blocks), so suites that pin markup, CSS rules
 * or script text keep reading one document.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const HTML_PATH = path.join(ROOT, 'dashboard.html');
const ASSET_SCRIPT = /<script src="assets\/js\/([\w-]+\.js)"><\/script>/g;
const ASSET_STYLE = /<link rel="stylesheet" href="assets\/css\/([\w-]+\.css)">/g;

function asset(kind, name) {
  return fs.readFileSync(path.join(ROOT, 'assets', kind, name), 'utf8');
}

// The page file as shipped (asset tags, not their contents).
function dashboardPage() {
  return fs.readFileSync(HTML_PATH, 'utf8');
}

// The asset files the page loads, in document order.
function dashboardAssets() {
  const page = dashboardPage();
  return {
    css: [...page.matchAll(ASSET_STYLE)].map(m => m[1]),
    js: [...page.matchAll(ASSET_SCRIPT)].map(m => m[1]),
  };
}

// The page with every asset inlined where its tag stands.
function dashboardHtml() {
  return dashboardPage()
    .replace(ASSET_STYLE, (_, name) => '<style>\n' + asset('css', name) + '</style>')
    .replace(ASSET_SCRIPT, (_, name) => '<script>\n' + asset('js', name) + '</script>');
}

// All of the page's scripts, joined in document order - what a suite evaluates.
function dashboardScript() {
  return [...dashboardHtml().matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)]
    .map(match => match[1]).join('\n');
}

module.exports = {dashboardHtml, dashboardScript, dashboardPage, dashboardAssets};
