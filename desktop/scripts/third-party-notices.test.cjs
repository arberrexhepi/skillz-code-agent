const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const asar = require('@electron/asar');
const notices = require('./third-party-notices.cjs');
const loadTypeScript = require('./load-ts.cjs');
const { thirdPartyNoticesPlugin } = loadTypeScript(() => require('./third-party-notices-plugin.ts'));

function write(file, contents) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, typeof contents === 'object' ? JSON.stringify(contents, null, 2) + '\n' : contents);
}
function fixture(t) {
  const base = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'skillz notice test ')));
  const root = path.join(base, 'desktop');
  t.after(() => {
    assert.equal(path.dirname(base), fs.realpathSync(os.tmpdir()));
    assert(path.basename(base).startsWith('skillz notice test '));
    fs.rmSync(base, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  });
  write(path.join(base, 'LICENSE'), 'Skillz Apache license fixture\n');
  write(path.join(base, 'NOTICE'), 'Copyright 2026 arbër inc\n');
  write(path.join(root, 'package.json'), { name: 'skillz-fixture', version: '1.0.0', dependencies: { alpha: '1.0.0' }, devDependencies: { bundled: '2.0.0', electron: '44.0.0', unused: '3.0.0' } });
  const packages = { '': { name: 'skillz-fixture', version: '1.0.0' } };
  for (const [name, version, dev] of [['alpha', '1.0.0', false], ['alpha/node_modules/nested', '1.0.0', false], ['bundled', '2.0.0', true], ['electron', '44.0.0', true], ['unused', '3.0.0', true]]) {
    const packageName = name.split('/').at(-1);
    packages['node_modules/' + name] = { version, license: 'MIT', ...(dev ? { dev: true } : {}) };
    write(path.join(root, 'node_modules', name, 'package.json'), { name: packageName, version, license: 'MIT', type: 'module', exports: './index.js' });
    write(path.join(root, 'node_modules', name, 'index.js'), 'export const answer = 42;\n');
    write(path.join(root, 'node_modules', name, 'LICENSE'), `Copyright ${packageName}\nMIT license fixture\n`);
  }
  packages['node_modules/macos-only'] = { version: '1.0.0', license: 'MIT', optional: true };
  write(path.join(root, 'package-lock.json'), { lockfileVersion: 3, packages });
  write(path.join(root, 'node_modules', 'alpha', 'NOTICE'), 'Keep this upstream attribution\n');
  write(path.join(root, 'node_modules', 'alpha', 'lib', 'embedded.js.LICENSE'), 'Copyright embedded dependency\nMIT embedded license fixture\n');
  write(path.join(root, 'node_modules', 'electron', 'dist', 'LICENSES.chromium.html'), 'This local runtime file must not be concatenated into the generated notice text.');
  for (const target of ['main', 'preload', 'renderer']) {
    const file = `out/${target}/index.js`;
    write(path.join(root, file), `console.log('${target}');\n`);
    write(path.join(root, 'out', 'licenses', `bundles-${target}.json`), { formatVersion: 1, target, packages: target === 'renderer' ? ['node_modules/bundled'] : [], files: [{ path: file, sha256: notices.sha(fs.readFileSync(path.join(root, file))) }] });
  }
  return { root, base };
}
function packageFixture(root, base, platform = 'win32') {
  notices.generate(root);
  const output = path.join(base, 'release');
  const resources = platform === 'darwin' ? path.join(output, 'skillz-fixture.app', 'Contents', 'Resources') : path.join(output, 'resources');
  const generated = path.join(root, 'out', 'licenses');
  for (const file of ['LICENSE', 'NOTICE']) write(path.join(resources, file), fs.readFileSync(path.join(base, file), 'utf8'));
  write(path.join(resources, 'THIRD_PARTY_NOTICES.txt'), fs.readFileSync(path.join(generated, 'THIRD_PARTY_NOTICES.txt'), 'utf8'));
  fs.cpSync(path.join(generated, 'third-party-licenses'), path.join(resources, 'third-party-licenses'), { recursive: true });
  write(path.join(resources, 'app', 'package.json'), JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8')));
  for (const target of ['main', 'preload', 'renderer']) write(path.join(resources, 'app', 'out', target, 'index.js'), fs.readFileSync(path.join(root, 'out', target, 'index.js'), 'utf8'));
  fs.cpSync(path.join(root, 'node_modules', 'alpha'), path.join(resources, 'app', 'node_modules', 'alpha'), { recursive: true });
  write(path.join(output, platform === 'darwin' ? 'LICENSE' : 'LICENSE.electron.txt'), fs.readFileSync(path.join(root, 'node_modules', 'electron', 'LICENSE'), 'utf8'));
  write(path.join(output, 'LICENSES.chromium.html'), '<html>Chromium distribution notices</html>\n');
  const context = { appOutDir: output, electronPlatformName: platform, packager: { projectDir: root, getResourcesDir: () => resources, getMacOsElectronFrameworkResourcesDir: () => path.join(output, 'absent framework') } };
  notices.afterPack(context);
  return { output, resources, context };
}

test('generation includes runtime, nested and bundled dev packages, upstream notices and sidecars', t => {
  const { root } = fixture(t);
  const first = notices.assemble(root);
  assert.deepEqual(first, notices.assemble(root));
  assert.deepEqual(first.manifest.packages.map(pkg => pkg.name).sort(), ['alpha', 'bundled', 'electron', 'nested']);
  assert(first.text.includes('Third-party packages retain their own licenses'));
  assert(first.text.includes('third-party-licenses/packages/alpha/NOTICE'));
  assert(!first.text.includes('Keep this upstream attribution'));
  assert(!first.text.includes('MIT embedded license fixture'));
  assert(first.files.some(document => document.contents.toString('utf8').includes('Keep this upstream attribution')));
  assert(!first.text.includes('This local runtime file'));
  assert(!first.text.includes('Copyright unused'));
  notices.generate(root);
  notices.check(root);
});

test('generation rejects missing license text, missing required dependencies and version drift', t => {
  const { root } = fixture(t);
  const file = path.join(root, 'node_modules', 'alpha', 'LICENSE');
  const original = fs.readFileSync(file);
  fs.unlinkSync(file);
  assert.throws(() => notices.assemble(root), /Missing upstream license/);
  fs.writeFileSync(file, original);
  const packageFile = path.join(root, 'node_modules', 'alpha', 'package.json');
  const metadata = JSON.parse(fs.readFileSync(packageFile, 'utf8'));
  write(packageFile, { ...metadata, version: '9.0.0' });
  assert.throws(() => notices.assemble(root), /differs from package-lock/);
  write(packageFile, metadata);
  fs.renameSync(path.join(root, 'node_modules', 'alpha'), path.join(root, 'removed-alpha'));
  assert.throws(() => notices.assemble(root), /Missing runtime dependency/);
});

test('generation selects the Apache option for DOMPurify without rewriting upstream text', t => {
  const { root } = fixture(t);
  const directory = path.join(root, 'node_modules', 'bundled');
  write(path.join(directory, 'package.json'), { name: 'dompurify', version: '2.0.0', license: '(MPL-2.0 OR Apache-2.0)' });
  const generated = notices.assemble(root);
  assert.equal(generated.manifest.packages.find(pkg => pkg.name === 'dompurify').selectedLicense, 'Apache-2.0');
  assert(generated.files.some(document => document.contents.toString('utf8') === 'Copyright bundled\nMIT license fixture\n'));
});

test('checking detects edited generated notices, upstream changes and stale bundle inventories', t => {
  const { root } = fixture(t);
  notices.generate(root);
  fs.appendFileSync(path.join(root, 'out', 'licenses', 'THIRD_PARTY_NOTICES.txt'), 'changed');
  assert.throws(() => notices.check(root), /stale/);
  notices.generate(root);
  fs.appendFileSync(path.join(root, 'node_modules', 'alpha', 'NOTICE'), 'new attribution');
  assert.throws(() => notices.check(root), /stale/);
  fs.appendFileSync(path.join(root, 'out', 'main', 'index.js'), 'changed();');
  assert.throws(() => notices.generate(root), /Build output changed/);
});

test('the real bundler records dev dependencies that survive the renderer build', async t => {
  const { root } = fixture(t);
  const source = path.join(root, 'source.js');
  write(source, "import { answer } from 'bundled'; console.log(answer);\n");
  write(path.join(root, 'node_modules', 'bundled', 'package.json'), { name: 'bundled', version: '2.0.0', license: 'MIT', type: 'module', exports: './lib/index.js' });
  write(path.join(root, 'node_modules', 'bundled', 'lib', 'package.json'), { name: 'internal-module-build', version: '0.0.1', type: 'module' });
  write(path.join(root, 'node_modules', 'bundled', 'lib', 'index.js'), 'export const answer = 42;\n');
  const { build } = await import('vite');
  await build({ configFile: false, root, logLevel: 'silent', plugins: [thirdPartyNoticesPlugin('renderer', root)], build: { outDir: path.join(root, 'out', 'renderer'), minify: false, rollupOptions: { input: source } } });
  const inventory = JSON.parse(fs.readFileSync(path.join(root, 'out', 'licenses', 'bundles-renderer.json'), 'utf8'));
  assert.deepEqual(inventory.packages, ['node_modules/bundled']);
  notices.generate(root);
  notices.check(root);
});

test('Windows package verification reads a real ASAR and detects removed or changed notices and code', async t => {
  const { root, base } = fixture(t);
  const { output, resources } = packageFixture(root, base);
  await asar.createPackage(path.join(resources, 'app'), path.join(resources, 'app.asar'));
  notices.verify(output, notices.check(root));
  const chrome = path.join(resources, 'third-party-licenses', 'electron', 'LICENSES.chromium.html');
  const original = fs.readFileSync(chrome);
  fs.appendFileSync(chrome, 'changed');
  assert.throws(() => notices.verify(output), /Electron notice changed/);
  fs.writeFileSync(chrome, original);
  const notice = path.join(resources, 'THIRD_PARTY_NOTICES.txt');
  const text = fs.readFileSync(notice);
  fs.unlinkSync(notice);
  assert.throws(() => notices.verify(output), /ENOENT/);
  fs.writeFileSync(notice, text);
  write(path.join(resources, 'app', 'out', 'main', 'index.js'), 'console.log("tampered");\n');
  await asar.createPackage(path.join(resources, 'app'), path.join(resources, 'app.asar'));
  assert.throws(() => notices.verify(output), /Packaged bundle changed/);
});

test('package verification rejects a shipped dependency absent from generated notices', t => {
  const { root, base } = fixture(t);
  const { output, resources } = packageFixture(root, base);
  write(path.join(resources, 'app', 'node_modules', 'unexpected', 'package.json'), { name: 'unexpected', version: '1.0.0' });
  assert.throws(() => notices.verify(output), /no generated notices: unexpected/);
});

test('macOS app resources keep Electron notices that originate outside the app bundle', t => {
  const { root, base } = fixture(t);
  const { output, resources } = packageFixture(root, base, 'darwin');
  assert.equal(notices.resourcesDirectory(output), resources);
  assert.equal(notices.resourcesDirectory(path.join(output, 'skillz-fixture.app')), resources);
  notices.verify(path.join(output, 'skillz-fixture.app'));
});

test('required upstream notices cannot disappear while the package license remains', t => {
  const { root } = fixture(t);
  const directory = path.join(root, 'node_modules', 'bundled');
  write(path.join(directory, 'package.json'), { name: 'playwright', version: '2.0.0', license: 'Apache-2.0' });
  write(path.join(directory, 'NOTICE'), 'Original Microsoft attribution');
  write(path.join(directory, 'ThirdPartyNotices.txt'), 'Bundled dependency licenses');
  notices.generate(root);
  fs.unlinkSync(path.join(directory, 'NOTICE'));
  assert.throws(() => notices.generate(root), /Missing required upstream document.*NOTICE/);
});

test('internal package metadata inherits its parent notices but nested dependencies must be listed', t => {
  const { root, base } = fixture(t);
  const { output, resources } = packageFixture(root, base);
  write(path.join(resources, 'app', 'node_modules', 'alpha', 'lib', 'package.json'), { name: 'internal-build', version: '0.0.1' });
  notices.verify(output);
  write(path.join(resources, 'app', 'node_modules', 'alpha', 'node_modules', 'unexpected', 'package.json'), { name: 'unexpected', version: '1.0.0' });
  assert.throws(() => notices.verify(output), /no generated notices: unexpected/);
});

test('a missing Electron license cannot be substituted with the project license', t => {
  const { root, base } = fixture(t);
  const { output, context } = packageFixture(root, base);
  fs.unlinkSync(path.join(output, 'LICENSE.electron.txt'));
  assert.throws(() => notices.afterPack(context), /Electron distribution is missing LICENSE/);
});

test('artifact packaging filters exclude cached code and Git metadata at every depth', t => {
  const { root, base } = fixture(t);
  const { FileMatcher } = require('app-builder-lib/out/fileMatcher');
  const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8'));
  for (const resource of pkg.build.extraResources.filter(item => ['artifact-template', 'prebuilt-artifacts'].includes(item.from))) {
    const from = path.join(root, resource.from);
    const matcher = new FileMatcher(from, path.join(base, 'destination'), value => value, resource.filter);
    const filter = matcher.createFilter();
    for (const relative of ['node_modules/lib/index.js', 'dist/index.js', '.git', 'nested/node_modules/lib/index.js', 'nested/dist/index.js', 'nested/.git']) {
      const file = path.join(from, relative);
      write(file, 'cached code or Git metadata');
      assert.equal(filter(file, fs.statSync(file)), false, resource.from + '/' + relative);
    }
    const source = path.join(from, 'nested', 'src', 'App.tsx');
    write(source, 'source code');
    assert.equal(filter(source, fs.statSync(source)), true);
  }
});

test('release verification catches artifact dependency caches outside the main app archive', t => {
  const { root, base } = fixture(t);
  const { output, resources } = packageFixture(root, base);
  write(path.join(resources, 'prebuilt-artifacts', 'example', 'node_modules', 'copied-library', 'index.js'), 'cached third-party code');
  assert.throws(() => notices.verify(output), /Artifact source includes generated files/);
});

test('original upstream texts remain separately packaged while the notice stays a concise index', t => {
  const { root, base } = fixture(t);
  const { output, resources } = packageFixture(root, base);
  const index = fs.readFileSync(path.join(resources, 'THIRD_PARTY_NOTICES.txt'), 'utf8');
  assert(!index.includes('Keep this upstream attribution'));
  assert(index.includes('third-party-licenses/packages/alpha/NOTICE'));
  const original = path.join(resources, 'third-party-licenses', 'packages', 'alpha', 'NOTICE');
  assert.equal(fs.readFileSync(original, 'utf8'), 'Keep this upstream attribution\n');
  fs.appendFileSync(original, 'changed');
  assert.throws(() => notices.verify(output), /Packaged upstream license changed/);
});
