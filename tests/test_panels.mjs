import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';

const page = readFileSync(new URL('../site/index.html', import.meta.url), 'utf8');
const app = readFileSync(new URL('../site/app.js', import.meta.url), 'utf8');
const mgmt = readFileSync(new URL('../site/management.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('../site/management.css', import.meta.url), 'utf8');

const views = ['overview','devices','history','plans','mail','settings','auth'];
const staticPanels = ['overview','devices','history','mail','auth'];
const navBlock = page.slice(page.indexOf('<nav id="primary-navigation"'), page.indexOf('</nav>'));
const allViews = [...page.matchAll(/data-view="([a-z]+)"/g)].map(m => m[1]);

test('seven navigation sections are declared in order', () => {
  const names = [...navBlock.matchAll(/data-view="([a-z]+)"/g)].map(m => m[1]);
  assert.deepEqual(names, views);
  for (const name of staticPanels) assert.match(page, new RegExp('data-view-panel="' + name + '"'), name);
  for (const name of ['plans','reports','settings']) assert.match(mgmt, new RegExp("data-view-panel','" + name + "'"), name);
});

test('page offers every navigation view plus reports, and no duplicates', () => {
  assert.deepEqual([...new Set(allViews)].sort(), [...views, 'reports'].sort());
  assert.equal((page.match(/data-view="reports"/g) || []).length, 1);
});

test('static panels are labelled and panel ids are unique', () => {
  for (const name of staticPanels) assert.match(page, new RegExp('<section id="' + name + '"[^>]*aria-label="[^"]+"'), name);
  const ids = [...page.matchAll(/id="([^"]+)"/g)].map(m => m[1]);
  assert.equal(new Set(ids).size, ids.length);
});

test('reports stay reachable from the overview without an eighth navigation item', () => {
  assert.doesNotMatch(navBlock, /data-view="reports"/);
  assert.match(page, /data-view-panel="overview"[\s\S]*data-view="reports"/);
});

test('app renders optional panels defensively so legacy tests and older pages keep working', () => {
  assert.match(app, /function optionalNode/);
  assert.match(app, /function renderExtras/);
  for (const id of ['overview-sources','overview-devices','history-content','mail-content','auth-content']) assert.ok(app.includes(id), id);
});

test('device inspector reports history sample count and last sample time', () => {
  assert.match(app, /histPoints/);
  assert.match(app, /历史采样/);
});

test('history, mail and auth styles exist and stack on narrow screens', () => {
  for (const selector of ['.source-strip','.status-chip','.quick-link','.mini-device','.history-row','.auth-card','.history-list']) assert.ok(css.includes(selector), selector);
  assert.match(css, /@media \(max-width:420px\)/);
});
