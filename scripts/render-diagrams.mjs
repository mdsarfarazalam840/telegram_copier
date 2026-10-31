// Renders the delivered Archify diagrams to PNG for README embedding.
// Usage: node scripts/render-diagrams.mjs
import { chromium } from '../frontend/node_modules/playwright/index.mjs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');

const diagrams = ['architecture', 'data-flow', 'run-lifecycle'];

const browser = await chromium.launch();
const page = await browser.newPage({
  viewport: { width: 2200, height: 1400 },
  deviceScaleFactor: 2,
  colorScheme: 'light',
});

for (const name of diagrams) {
  const file = path.join(root, 'docs', 'diagrams', `${name}.html`);
  await page.goto(`file://${file.replace(/\\/g, '/')}`);
  // Hide viewer chrome so it never paints over the SVG element screenshot.
  await page.addStyleTag({
    content:
      '.no-print,.toolbar,.archify-toast,.header,.cards{display:none !important}',
  });
  await page.evaluate(() => document.fonts.ready);
  const svg = page.locator('svg').first();
  await svg.waitFor({ state: 'visible' });
  const out = path.join(root, 'docs', 'diagrams', `${name}.png`);
  await svg.screenshot({ path: out, scale: 'device' });
  console.log('rendered', path.relative(root, out));
}

await browser.close();
