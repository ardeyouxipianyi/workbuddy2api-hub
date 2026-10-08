/* The shipped dashboard page, and the source of the inline scripts it ships.
 *
 * Every dashboard suite opened with the same two lines: read the real
 * ../dashboard.html, then join the body of every <script> block. That is all
 * this module is. The fake DOM, the globals, the fetch stubs and the
 * new Function() call stay in each suite, because those differ per suite.
 *
 *   const {dashboardScript} = require('./_dashboard_source.js');
 */
'use strict';

const fs = require('fs');
const path = require('path');

const HTML_PATH = path.join(__dirname, '..', 'dashboard.html');

// The page next to this file, so a suite always runs the shipped code.
function dashboardHtml() {
  return fs.readFileSync(HTML_PATH, 'utf8');
}

// Its inline scripts, joined in document order - what a suite evaluates.
function dashboardScript() {
  return [...dashboardHtml().matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)]
    .map(match => match[1]).join('\n');
}

module.exports = {dashboardHtml, dashboardScript};
