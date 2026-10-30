// Optional real-browser smoke test. The Python harness owns the local servers.
const fs = require('fs');
const { chromium } = require(process.env.STRIPE_DOUBLE_PLAYWRIGHT_MODULE);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext();
    await context.route('**/*', route => {
      const url = new URL(route.request().url());
      return url.hostname === '127.0.0.1' ? route.continue() : route.abort();
    });
    const page = await context.newPage();
    page.setDefaultTimeout(7000);
    page.setDefaultNavigationTimeout(7000);
    let stage = 'checkout';
    try {
    await page.goto(input.checkout);
    await page.getByRole('button', {name:'Complete checkout'}).click();
    await page.waitForURL(input.callback + '/success**');
    const checkout = page.url();
    stage = 'portal';
    await page.goto(input.portal);
    await page.locator('form').filter({has:page.locator('input[name="subscription"][value="'+input.subscription+'"]')}).getByRole('button', {name:'Cancel at period end'}).click();
    await page.waitForURL(input.callback + '/account');
    stage = 'oauth';
    await page.goto(input.oauth);
    await page.getByLabel('Account').selectOption(input.account);
    await page.getByRole('button', {name:'Authorize'}).click();
    await page.waitForURL(input.callback + '/oauth**');
    process.stdout.write(JSON.stringify({checkout, oauth:page.url()}));
    } catch (error) {
      throw new Error('Browser journey failed at '+stage+' ('+error.name+')');
    }
    await context.close();
  } finally {
    await browser.close();
  }
})().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
