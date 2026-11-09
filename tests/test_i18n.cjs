const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const test = require('node:test');

test('the dashboard exposes a Portuguese and English language control', () => {
  const html = readFileSync('explorer/index.html', 'utf8');
  assert.match(html, /<select\b[^>]*id="language"/);
  assert.match(html, /<option value="pt"[^>]*>Português<\/option>/);
  assert.match(html, /<option value="en"[^>]*>English<\/option>/);
});

test('a saved language wins, with browser negotiation and Portuguese fallback', () => {
  const {chooseLanguage} = require('../explorer/i18n.js');
  assert.equal(chooseLanguage('pt', ['en-US']), 'pt');
  assert.equal(chooseLanguage(null, ['fr-FR', 'en-GB']), 'en');
  assert.equal(chooseLanguage(null, ['pt-BR']), 'pt');
  assert.equal(chooseLanguage(null, ['es-ES']), 'pt');
});

test('live and archive messages translate while flow data stays intact', () => {
  const {translate} = require('../explorer/i18n.js');
  assert.equal(translate('Fluxos individuais', 'en'), 'Individual flows');
  assert.equal(translate('Fluxos individuais', 'pt'), 'Fluxos individuais');
  assert.equal(translate('Importação recusada. Arquivo XZ incompleto.', 'en'),
    'Import rejected. Incomplete XZ archive.');
  assert.equal(translate('203.0.113.25:443', 'en'), '203.0.113.25:443');
});
