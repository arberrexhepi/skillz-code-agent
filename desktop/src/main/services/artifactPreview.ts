import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';
import type { PreviewFrame, PreviewInput } from '../../shared/artifacts';
import type { ArtifactBrowserCheckInput, ArtifactBrowserCheckStep } from '../../shared/artifactAppMcp';
import { runLogged, nodeRuntime } from './artifactProcess';
import path from 'node:path';

type BrowserCheckOptions = Pick<ArtifactBrowserCheckInput, 'viewport' | 'reducedMotion' | 'colorScheme' | 'timeoutMs'>;

function defaultStepLabel(step: ArtifactBrowserCheckStep): string {
  if (step.label) return step.label;
  if ('selector' in step && step.selector) return `${step.type} ${step.selector}`.slice(0, 140);
  if ('route' in step) return `${step.type} ${step.route}`.slice(0, 140);
  if ('key' in step) return `${step.type} ${step.key}`.slice(0, 140);
  if ('ms' in step) return `wait ${step.ms}ms`;
  return step.type;
}

function isVerifierStepError(step: ArtifactBrowserCheckStep | undefined, message: string): boolean {
  if (/strict mode violation/i.test(message)) return true;
  if (step?.type === 'evaluate' && /(?:Evaluate step timed out|Evaluate step returned \d+ bytes|\b(?:SyntaxError|ReferenceError)\b)/i.test(message)) {
    return true;
  }
  return false;
}

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
  private async context(origin: string, options: BrowserCheckOptions = {}): Promise<BrowserContext> {
    if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(origin)) throw new Error('Preview requires a local artifact runtime.');
    if (!this.browser) this.browser = chromium.launch({ headless: true }).catch((error) => { this.browser = undefined; throw new Error(`Inspection browser unavailable. Install Playwright inspection browser in artifact Setup, then retry. ${String(error)}`); });
    const browser = await this.browser;
    const context = await browser.newContext({
      viewport: options.viewport ?? { width: 1200, height: 800 },
      deviceScaleFactor: 1,
      serviceWorkers: 'block',
      acceptDownloads: false,
      ...(options.reducedMotion ? { reducedMotion: options.reducedMotion } : {}),
      ...(options.colorScheme ? { colorScheme: options.colorScheme } : {}),
    });
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
  async checkRoute(previewUrl: string, route: string, steps: ArtifactBrowserCheckStep[] = [], options: BrowserCheckOptions = {}): Promise<Record<string, unknown>> {
    const origin = new URL(previewUrl).origin;
    const url = new URL(route, origin);
    if (url.origin !== origin) throw new Error('Route checks must stay on this artifact preview.');
    let context: BrowserContext;
    try { context = await this.context(origin, options); }
    catch (error) { return { ok: false, unavailable: true, error: String(error) }; }
    const consoleMessages: object[] = [], pageErrors: object[] = [], requestFailures: object[] = [], responseErrors: object[] = [];
    const observations: { type: ArtifactBrowserCheckStep['type']; label: string; selector?: string }[] = [];
    const measurements: { label: string; value: unknown }[] = [];
    let measurementBytes = 0;
    const verificationResults: Record<string, unknown> = {};
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
      let verifierError = false;
      let stepLabel = 'Initial route';
      let currentStep: ArtifactBrowserCheckStep | undefined;
      try {
        await page.goto(url.href, { waitUntil: 'domcontentloaded', timeout: 15000 });
        const assertOrigin = () => {
          if (new URL(page.url()).origin !== origin) throw new Error('Browser checks must stay on this artifact preview.');
        };
        if (steps.length) assertOrigin();
        const totalTimeout = options.timeoutMs ?? 30000;
        const deadline = Date.now() + totalTimeout;
        const defaultEvalTimeout = Math.max(15000, totalTimeout - 2000);
        for (const step of steps) {
          currentStep = step;
          stepLabel = defaultStepLabel(step);
          const remaining = deadline - Date.now();
          if (remaining <= 0) throw new Error('Browser interaction timed out.');
          const stepTimeout = (fallback: number) => Math.min(remaining, 'timeoutMs' in step && step.timeoutMs ? step.timeoutMs : fallback);
          if (step.type === 'goto') {
            const next = new URL(step.route, origin);
            if (next.origin !== origin) throw new Error('Browser checks must stay on this artifact preview.');
            await page.goto(next.href, { waitUntil: 'domcontentloaded', timeout: stepTimeout(10000) });
          } else if (step.type === 'click') {
            await page.locator(step.selector).click({ timeout: stepTimeout(5000) });
          } else if (step.type === 'fill') {
            const locator = page.locator(step.selector);
            await locator.waitFor({ state: 'visible', timeout: stepTimeout(5000) });
            await locator.fill(step.value, { timeout: stepTimeout(5000) });
          } else if (step.type === 'select') {
            const locator = page.locator(step.selector);
            await locator.waitFor({ state: 'visible', timeout: stepTimeout(5000) });
            await locator.selectOption(step.value, { timeout: stepTimeout(5000) });
          } else if (step.type === 'press') {
            if (step.selector) {
              await page.locator(step.selector).press(step.key, { timeout: stepTimeout(5000) });
            } else {
              await page.keyboard.press(step.key);
            }
          } else if (step.type === 'hover') {
            await page.locator(step.selector).hover({ timeout: stepTimeout(5000) });
          } else if (step.type === 'drag') {
            const locator = page.locator(step.selector);
            await locator.waitFor({ state: 'visible', timeout: stepTimeout(5000) });
            const box = await locator.boundingBox({ timeout: stepTimeout(5000) });
            if (!box || box.width <= 0 || box.height <= 0) throw new Error(`Element has no visible bounding box: ${step.selector}`);
            const startX = box.x + box.width / 2;
            const startY = box.y + box.height / 2;
            await page.mouse.move(startX, startY);
            await page.mouse.down();
            await page.mouse.move(startX + step.dx, startY + step.dy, { steps: step.steps ?? 8 });
            await page.mouse.up();
          } else if (step.type === 'wheel') {
            const locator = page.locator(step.selector);
            await locator.waitFor({ state: 'visible', timeout: stepTimeout(5000) });
            const box = await locator.boundingBox({ timeout: stepTimeout(5000) });
            if (!box || box.width <= 0 || box.height <= 0) throw new Error(`Element has no visible bounding box: ${step.selector}`);
            await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
            await page.mouse.wheel(step.deltaX ?? 0, step.deltaY);
          } else if (step.type === 'wait') {
            await page.waitForTimeout(Math.min(remaining, step.ms));
          } else if (step.type === 'assert-visible') {
            await page.locator(step.selector).waitFor({ state: 'visible', timeout: stepTimeout(10000) });
          } else if (step.type === 'assert-hidden') {
            await page.locator(step.selector).waitFor({ state: 'hidden', timeout: stepTimeout(10000) });
          } else if (step.type === 'assert-text') {
            const timeout = stepTimeout(10000);
            const locator = page.locator(step.selector);
            await locator.first().waitFor({ state: 'visible', timeout });
            const handle = await locator.first().elementHandle({ timeout });
            if (!handle) throw new Error(`Element not found for text assertion: ${step.selector}`);
            try {
              await page.waitForFunction(
                ({ element, expected, exact }) => {
                  const text = (element.textContent || '').trim();
                  return exact ? text === expected : text.includes(expected);
                },
                { element: handle, expected: step.text, exact: Boolean(step.exact) },
                { timeout: Math.max(100, Math.min(timeout, deadline - Date.now())) },
              );
            } catch {
              const actual = ((await handle.textContent()) || '').trim().slice(0, 200);
              throw new Error(`Expected ${step.exact ? 'exact ' : ''}text "${step.text}", observed "${actual}".`);
            } finally {
              await handle.dispose();
            }
          } else if (step.type === 'assert-focused') {
            const timeout = stepTimeout(10000);
            const locator = page.locator(step.selector);
            await locator.first().waitFor({ state: 'visible', timeout });
            const handle = await locator.first().elementHandle({ timeout });
            if (!handle) throw new Error(`Element not found for focus assertion: ${step.selector}`);
            try {
              await page.waitForFunction(
                (element) => document.activeElement === element,
                handle,
                { timeout: Math.max(100, Math.min(timeout, deadline - Date.now())) },
              );
            } catch {
              const active = await page.evaluate(() => {
                const el = document.activeElement;
                if (!el) return 'none';
                const id = el.id ? `#${el.id}` : '';
                const text = (el.textContent || '').trim().slice(0, 60);
                return `${el.tagName.toLowerCase()}${id}${text ? ` ("${text}")` : ''}`;
              });
              throw new Error(`Expected focus on ${step.selector}, but activeElement was ${active}.`);
            } finally {
              await handle.dispose();
            }
          } else if (step.type === 'evaluate') {
            const evalTimeout = stepTimeout(defaultEvalTimeout);
            let timer: ReturnType<typeof setTimeout> | undefined;
            const evaluated = await Promise.race([
              page.evaluate(async (source: string) => {
                let candidate: unknown;
                try {
                  candidate = (0, eval)(`(${source})`);
                } catch (syntaxError) {
                  if (syntaxError instanceof SyntaxError) {
                    const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor as new (body: string) => () => Promise<unknown>;
                    return await new AsyncFunction(source)();
                  }
                  throw syntaxError;
                }
                return typeof candidate === 'function' ? await (candidate as () => unknown)() : await candidate;
              }, step.script),
              new Promise<never>((_, reject) => {
                timer = setTimeout(() => reject(new Error(`Evaluate step timed out after ${evalTimeout}ms.`)), evalTimeout);
              }),
            ]).finally(() => { if (timer) clearTimeout(timer); });
            const value = evaluated === undefined ? null : evaluated;
            const serialized = JSON.stringify(value) ?? 'null';
            if (serialized.length > 16384) {
              throw new Error(`Evaluate step returned ${serialized.length} bytes (limit 16384). Return a compact measurement or verification_results summary.`);
            }
            if (measurementBytes + serialized.length <= 32768) {
              measurements.push({ label: stepLabel, value });
              measurementBytes += serialized.length;
            }
            if (value && typeof value === 'object' && !Array.isArray(value)) {
              const verdicts = (value as Record<string, unknown>).verification_results;
              if (verdicts && typeof verdicts === 'object' && !Array.isArray(verdicts)) {
                Object.assign(verificationResults, verdicts);
              }
            }
          }
          assertOrigin();
          observations.push({
            type: step.type,
            label: stepLabel,
            ...('selector' in step && step.selector ? { selector: step.selector } : {}),
          });
        }
      } catch (failure) {
        if (!steps.length) throw failure;
        const message = String(failure).slice(0, 600);
        verifierError = isVerifierStepError(currentStep, message);
        error = `${stepLabel}: ${message}`;
      }
      await page.waitForTimeout(steps.length ? 300 : 1000);
      const result: Record<string, unknown> = {
        url: url.href,
        route,
        finalUrl: page.url(),
        title: (await page.title()).slice(0, 2000),
        consoleMessages,
        pageErrors,
        requestFailures,
        responseErrors,
      };
      if (!steps.length) return { ok: true, ...result };
      const incidents = consoleMessages.length + pageErrors.length + requestFailures.length + responseErrors.length;
      if (measurements.length) result.measurements = measurements;
      if (Object.keys(verificationResults).length) result.verification_results = verificationResults;
      if (verifierError && incidents === 0) result.verifierError = true;
      return {
        ...result,
        ok: !error && incidents === 0,
        observations,
        error: error || (incidents ? `Browser reported ${incidents} issue(s).` : undefined),
      };
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
