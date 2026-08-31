const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');
const { chromium } = require('playwright');

async function main() {
  const project = process.argv[2];
  const source = path.join(project, 'svg_output');
  const output = path.join(project, 'validation', 'review_png');
  fs.mkdirSync(output, { recursive: true });
  const files = fs.readdirSync(source).filter((name) => name.endsWith('.svg')).sort();
  const browser = await chromium.launch({
    headless: true,
    executablePath: 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 1 });
  for (const file of files) {
    await page.goto(pathToFileURL(path.join(source, file)).href, { waitUntil: 'load' });
    await page.screenshot({ path: path.join(output, file.replace(/\.svg$/i, '.png')) });
    process.stdout.write(`rendered ${file}\n`);
  }
  await browser.close();
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
