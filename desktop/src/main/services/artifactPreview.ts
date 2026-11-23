import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';
import type { PreviewFrame, PreviewInput } from '../../shared/artifacts';
import type { ArtifactBrowserCheckStep } from '../../shared/artifactAppMcp';
import { runLogged, nodeRuntime } from './artifactProcess';
import path from 'node:path';

export class ArtifactPreviewService {
  private browser: Promise<Browser> | undefined;
  private pages = new Map<string, { context: BrowserContext; page: Page; origin: string }>();
  private queues = new Map<string, Promise<unknown>>();
  private install: Promise<void> | undefined;
  async installBrowser(log: (text: string) => void = () => {}): Promise<void> {
    if (!this.install) this.install = (async () => {
      const { node } = process.versions.electron ? { node: process.execPath } : await nodeRuntime();
      const cli = path.join(path.dirname(require.resolve('playwright/package.json').replace('app.asar', 'app.asar.unpacked')), 'cli.js');
      await runLogged(node, [cli, 'install', 'chromium'], process.cwd(), log);
    })().finally(() => { this.install = undefined; });
    return this.install;
  }
  async browserReady(): Promise<boolean> {
    try { const browser = await chromium.launch({ headless: true, timeout: 10_000 }); await browser.close(); return true; }
    catch { return false; }
  }
  private serial<T>(id: string, action: () => Promise<T>): Promise<T> {
    const result = (this.queues.get(id) || Promise.resolve()).then(action);
    const settled = result.catch(() => {}); this.queues.set(id, settled);
    void settled.then(() => { if (this.queues.get(id) === settled) this.queues.delete(id); });
    return result;
  }
  private async context(origin: string): Promise<BrowserContext> {
    if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(origin)) throw new Error('Preview requires a local artifact runtime.');
    if (!this.browser) this.browser = chromium.launch({ headless: true }).catch((error) => { this.browser = undefined; throw new Error(`Inspection browser unavailable. Install Playwright inspection browser in artifact Setup, then retry. ${String(error)}`); });
    const browser = await this.browser;
    const context = await browser.newContext({ viewport: { width: 1200, height: 800 }, deviceScaleFactor: 1, serviceWorkers: 'block', acceptDownloads: false });
    try {
      // Artifact frontend calls its own configured gateway. It cannot reach the workbench IPC or arbitrary websites.
      await context.route('**/*', (route) => {
        const target = new URL(route.request().url());
        return target.origin === origin || ['data:', 'blob:'].includes(target.protocol) ? route.continue() : route.abort();
      });
      await context.routeWebSocket('**/*', (socket) => { if (new URL(socket.url()).origin === origin.replace('http:', 'ws:')) socket.connectToServer(); else socket.close(); });
      return context;
    } catch (error) { await context.close(); throw error; }
  }
  private async page(id: string, url: string): Promise<Page> {
    const origin = new URL(url).origin;
    const existing = this.pages.get(id);
    if (existing && !existing.page.isClosed() && existing.origin === origin) return existing.page;
    await existing?.context.close(); this.pages.delete(id);
    const context = await this.context(origin);
    try {
      const page = await context.newPage();
      page.on('dialog', (dialog) => void dialog.dismiss());
      page.on('popup', (popup) => void popup.close());
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
      this.pages.set(id, { page, context, origin });
      return page;
    } catch (error) { await context.close(); throw error; }
  }
  async checkRoute(previewUrl: string, route: string, steps: ArtifactBrowserCheckStep[] = []): Promise<Record<string, unknown>> {
    const origin = new URL(previewUrl).origin;
    const url = new URL(route, origin);
    if (url.origin !== origin) throw new Error('Route checks must stay on this artifact preview.');
    let context: BrowserContext;
    try { context = await this.context(origin); }
    catch (error) { return { ok: false, unavailable: true, error: String(error) }; }
    const consoleMessages: object[] = [], pageErrors: object[] = [], requestFailures: object[] = [], responseErrors: object[] = [];
    const observations: { type: ArtifactBrowserCheckStep['type']; label: string; selector?: string }[] = [];
    const collect = (list: object[], value: object): void => { if (list.length < 32) list.push(value); };
    try {
      const page = await context.newPage();
      page.on('dialog', (dialog) => void dialog.dismiss());
      page.on('popup', (popup) => void popup.close());
      page.on('console', (entry) => {
        if (['warning', 'error'].includes(entry.type())) collect(consoleMessages, { type: entry.type(), text: entry.text().slice(0, 2000), location: entry.location() });
      });
      page.on('pageerror', (error) => collect(pageErrors, { message: error.message.slice(0, 2000), stack: error.stack?.slice(0, 2000) }));
      page.on('requestfailed', (request) => collect(requestFailures, { url: request.url().slice(0, 2048), method: request.method(), errorText: request.failure()?.errorText }));
      page.on('response', (response) => {
        if (response.status() >= 400) collect(responseErrors, { url: response.url().slice(0, 2048), status: response.status(), statusText: response.statusText() });
      });
      let error: string | undefined;
      let stepLabel = 'Initial route';
      try {
        await page.goto(url.href, { waitUntil: 'domcontentloaded', timeout: 15000 });
        const assertOrigin = () => {
          if (new URL(page.url()).origin !== origin) throw new Error('Browser checks must stay on this artifact preview.');
        };
        if (steps.length) assertOrigin();
        const deadline = Date.now() + 15000;
        for (const step of steps) {
          stepLabel = step.label;
          const remaining = deadline - Date.now();
          if (remaining <= 0) throw new Error('Browser interaction timed out.');
          if (step.type === 'goto') {
            const next = new URL(step.route, origin);
            if (next.origin !== origin) throw new Error('Browser checks must stay on this artifact preview.');
            await page.goto(next.href, { waitUntil: 'domcontentloaded', timeout: Math.min(remaining, 5000) });
          } else if (step.type === 'click') {
            await page.locator(step.selector).click({ timeout: Math.min(remaining, 3500) });
          } else {
            await page.locator(step.selector).waitFor({ state: 'visible', timeout: Math.min(remaining, 3500) });
          }
          assertOrigin();
          observations.push({ type: step.type, label: step.label, selector: 'selector' in step ? step.selector : undefined });
        }
      } catch (failure) {
        if (!steps.length) throw failure;
        error = `${stepLabel}: ${String(failure).slice(0, 600)}`;
      }
      await page.waitForTimeout(1000);
      const result = { url: url.href, route, finalUrl: page.url(), title: (await page.title()).slice(0, 2000), consoleMessages, pageErrors, requestFailures, responseErrors };
      if (!steps.length) return { ok: true, ...result };
      const incidents = consoleMessages.length + pageErrors.length + requestFailures.length + responseErrors.length;
      return { ...result, ok: !error && incidents === 0, observations, error: error || (incidents ? `Browser reported ${incidents} issue(s).` : undefined) };
    } finally { await context.close(); }
  }
  frame(id: string, url: string): Promise<PreviewFrame> {
    return this.serial(id, async () => {
      const page = await this.page(id, url);
      const buffer = await page.screenshot({ type: 'jpeg', quality: 85, timeout: 10000 });
      return { image: `data:image/jpeg;base64,${buffer.toString('base64')}`, width: 1200, height: 800 };
    });
  }
  input(id: string, input: PreviewInput): Promise<void> {
    return this.serial(id, async () => {
      const page = this.pages.get(id)?.page;
      if (!page || page.isClosed()) throw new Error('Open this artifact preview first.');
      if (input.type === 'click') await page.mouse.click(input.x, input.y);
      if (input.type === 'wheel') await page.mouse.wheel(input.dx, input.dy);
      if (input.type === 'text') await page.keyboard.insertText(input.text);
      if (input.type === 'key') await page.keyboard.press(input.key);
    });
  }
  reload(id: string): Promise<void> { return this.serial(id, async () => { await this.pages.get(id)?.page.reload({ waitUntil: 'domcontentloaded' }); }); }
  close(id: string): Promise<void> { return this.serial(id, async () => { const page = this.pages.get(id); this.pages.delete(id); await page?.context.close(); }); }
  async dispose(): Promise<void> { await Promise.allSettled([...this.pages.keys()].map((id) => this.close(id))); await (await this.browser?.catch(() => undefined))?.close(); this.browser = undefined; }
}
