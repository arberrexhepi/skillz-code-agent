const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const assert = require('node:assert/strict');

const projectDir = path.resolve(__dirname, '..');
const targets = ['main', 'preload', 'renderer'];
const noticeName = 'THIRD_PARTY_NOTICES.txt';
const sha = content => createHash('sha256').update(content).digest('hex');
const json = file => JSON.parse(fs.readFileSync(file, 'utf8'));
const sorted = values => [...values].sort();
function inside(root, location) {
  if (typeof location !== 'string' || path.isAbsolute(location) || location.includes('\\')) throw new Error(`Invalid notice path: ${location}`);
  const resolved = path.resolve(root, location);
  const relative = path.relative(root, resolved);
  if (relative.startsWith('..') || path.isAbsolute(relative)) throw new Error(`Notice path escapes its directory: ${location}`);
  return resolved;
}
function documents(directory, excludedRootDirectories = []) {
  const result = [];
  function walk(current, prefix = '') {
    for (const entry of fs.readdirSync(current, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      if (entry.name === 'node_modules' || entry.name === '.git' || (!prefix && excludedRootDirectories.includes(entry.name))) continue;
      const location = prefix + entry.name;
      if (entry.isDirectory()) walk(path.join(current, entry.name), location + '/');
      else if (entry.isFile() && (/^(?:licen[sc]es?(?:[._-].*)?|copying(?:[._-].*)?|copyright(?:[._-].*)?|notice(?:[._-].*)?|authors(?:[._-].*)?|third[-_]?party[-_]?notices?(?:[._-].*)?)$/i.test(entry.name) || /\.LICENSE(?:\.txt)?$/i.test(entry.name))) {
        const contents = fs.readFileSync(path.join(current, entry.name));
        if (contents.includes(0) || !contents.toString('utf8').trim()) throw new Error(`Empty or binary license document: ${location}`);
        result.push({ path: location, sha256: sha(contents), contents });
      }
    }
  }
  walk(directory);
  if (!result.some(item => !item.path.includes('/') && /licen[sc]e|copying|copyright/i.test(item.path))) throw new Error(`Missing upstream license text in ${directory}`);
  return result;
}
function assemble(root = projectDir) {
  const pkg = json(path.join(root, 'package.json'));
  const lock = json(path.join(root, 'package-lock.json'));
  const sources = new Map();
  const bundleFiles = new Map();
  const add = (location, use) => {
    if (!location.startsWith('node_modules/')) throw new Error(`Unexpected dependency location: ${location}`);
    if (!sources.has(location)) sources.set(location, new Set());
    sources.get(location).add(use);
  };
  for (const [location, info] of Object.entries(lock.packages)) {
    if (!location || info.dev) continue;
    if (!fs.existsSync(inside(root, location))) {
      if (info.optional) continue; // For example macOS-only fsevents on Windows.
      throw new Error(`Missing runtime dependency ${location}. Run npm ci in desktop.`);
    }
    add(location, 'runtime dependency');
  }
  add('node_modules/electron', 'Electron runtime');
  for (const target of targets) {
    const inventory = json(path.join(root, 'out', 'licenses', `bundles-${target}.json`));
    assert.equal(inventory.formatVersion, 1, `Unsupported ${target} bundle inventory`);
    assert.equal(inventory.target, target);
    if (!inventory.files.length) throw new Error(`Empty ${target} build. Run npm run build.`);
    for (const location of inventory.packages) add(location, `${target} bundle`);
    for (const file of inventory.files) {
      if (!file.path.startsWith(`out/${target}/`)) throw new Error(`Unexpected ${target} output: ${file.path}`);
      if (sha(fs.readFileSync(inside(root, file.path))) !== file.sha256) throw new Error(`Build output changed: ${file.path}. Run npm run build.`);
      bundleFiles.set(file.path, file.sha256);
    }
  }
  const packages = [];
  const files = [];
  const sections = [
    'Skillz Code Agent - third-party software notices',
    'Generated from locked runtime dependencies, packages included in built JavaScript, and Electron.',
    'Third-party packages retain their own licenses and copyright notices.',
    'This index does not replace or expand their license terms.',
    'Original license and attribution files accompany this app at the paths below.',
    'Electron/Chromium component licenses ship at third-party-licenses/electron/LICENSES.chromium.html.',
    '',
  ];
  for (const location of sorted(sources.keys())) {
    const directory = inside(root, location);
    const metadata = json(path.join(directory, 'package.json'));
    const locked = lock.packages[location];
    if (!locked || locked.version !== metadata.version) throw new Error(`Installed dependency differs from package-lock.json: ${location}. Run npm ci.`);
    const declared = typeof metadata.license === 'string' ? metadata.license : metadata.license?.type;
    if (!metadata.name || !declared || /unlicensed|see license/i.test(declared)) throw new Error(`Review missing license metadata for ${location}`);
    const selected = metadata.name === 'dompurify' && declared === '(MPL-2.0 OR Apache-2.0)' ? 'Apache-2.0' : declared;
    const notices = documents(directory, metadata.name === 'electron' ? ['dist'] : []);
    const required = metadata.name === 'monaco-editor' ? ['ThirdPartyNotices.txt'] : ['playwright', 'playwright-core'].includes(metadata.name) ? ['NOTICE', 'ThirdPartyNotices.txt'] : [];
    for (const name of required) if (!notices.some(document => document.path === name)) throw new Error(`Missing required upstream document for ${metadata.name}: ${name}`);
    const uses = sorted(sources.get(location));
    packages.push({ path: location, name: metadata.name, version: metadata.version, declaredLicense: declared, selectedLicense: selected, uses, documents: notices.map(({ path, sha256 }) => ({ path, sha256, file: `packages/${location.slice('node_modules/'.length)}/${path}` })) });
    sections.push(`${metadata.name}@${metadata.version} (${selected})`);
    for (const document of notices) {
      const file = `packages/${location.slice('node_modules/'.length)}/${document.path}`;
      sections.push(`  third-party-licenses/${file}`);
      files.push({ file, contents: document.contents });
    }
    sections.push('');
  }
  const text = sections.join('\n') + '\n';
  const manifest = {
    formatVersion: 1,
    app: { name: pkg.name, version: pkg.version },
    packages,
    bundleFiles: [...bundleFiles].sort(([a], [b]) => a.localeCompare(b)).map(([path, sha256]) => ({ path, sha256 })),
    projectDocuments: ['LICENSE', 'NOTICE'].map(name => ({ path: name, sha256: sha(fs.readFileSync(path.join(root, '..', name))) })),
    noticeSha256: sha(text),
  };
  return { text, manifest, files };
}
function generate(root = projectDir) {
  const generated = assemble(root);
  const destination = path.join(root, 'out', 'licenses');
  fs.mkdirSync(destination, { recursive: true });
  const licenseDirectory = path.join(destination, 'third-party-licenses');
  if (fs.existsSync(licenseDirectory)) {
    // Only replace this generator's output, after checking the resolved target.
    const relative = path.relative(fs.realpathSync(root), fs.realpathSync(licenseDirectory));
    if (relative !== path.join('out', 'licenses', 'third-party-licenses') || fs.lstatSync(licenseDirectory).isSymbolicLink()) throw new Error('Generated license directory is outside the intended build output.');
    fs.rmSync(licenseDirectory, { recursive: true, force: true });
  }
  fs.mkdirSync(licenseDirectory, { recursive: true });
  for (const document of generated.files) {
    const file = inside(licenseDirectory, document.file);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, document.contents);
  }
  fs.writeFileSync(path.join(licenseDirectory, 'manifest.json'), JSON.stringify(generated.manifest, null, 2) + '\n');
  fs.writeFileSync(path.join(destination, noticeName), generated.text);
  fs.writeFileSync(path.join(destination, 'manifest.json'), JSON.stringify(generated.manifest, null, 2) + '\n');
  console.log(`Generated ${noticeName}: ${generated.manifest.packages.length} packages, ${generated.manifest.packages.reduce((count, pkg) => count + pkg.documents.length, 0)} upstream documents.`);
  return generated.manifest;
}
function check(root = projectDir) {
  const expected = assemble(root);
  assert.equal(fs.readFileSync(path.join(root, 'out', 'licenses', noticeName), 'utf8'), expected.text, 'Third-party notices are stale. Run npm run notices:generate.');
  assert.deepEqual(json(path.join(root, 'out', 'licenses', 'manifest.json')), expected.manifest, 'Notice manifest is stale. Run npm run notices:generate.');
  const directory = path.join(root, 'out', 'licenses', 'third-party-licenses');
  assert.deepEqual(json(path.join(directory, 'manifest.json')), expected.manifest, 'License directory manifest is stale.');
  for (const document of expected.files) assert.equal(sha(fs.readFileSync(inside(directory, document.file))), sha(document.contents), `Generated upstream license changed: ${document.file}`);
  return expected.manifest;
}
function resourcesDirectory(directory) {
  const absolute = path.resolve(directory);
  for (const candidate of [absolute, path.join(absolute, 'resources'), path.join(absolute, 'Contents', 'Resources')]) {
    if (fs.existsSync(path.join(candidate, 'third-party-licenses', 'manifest.json'))) return candidate;
  }
  const apps = fs.readdirSync(absolute, { withFileTypes: true }).filter(entry => entry.isDirectory() && entry.name.endsWith('.app'));
  if (apps.length === 1) return resourcesDirectory(path.join(absolute, apps[0].name));
  throw new Error(`Packaged third-party notices were not found in ${directory}`);
}
function appReader(resources) {
  const archive = path.join(resources, 'app.asar');
  if (fs.existsSync(archive)) {
    const asar = require('@electron/asar');
    asar.uncache(archive);
    return { read: file => asar.extractFile(archive, path.normalize(file)), files: () => asar.listPackage(archive).map(file => file.replaceAll('\\', '/').replace(/^\//, '')) };
  }
  const app = path.join(resources, 'app');
  const files = [];
  function walk(directory, prefix = '') {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      if (entry.isDirectory()) walk(path.join(directory, entry.name), prefix + entry.name + '/');
      else if (entry.isFile()) files.push(prefix + entry.name);
    }
  }
  walk(app);
  return { read: file => fs.readFileSync(inside(app, file)), files: () => files };
}
function verify(directory, expected) {
  const resources = resourcesDirectory(directory);
  const manifest = json(path.join(resources, 'third-party-licenses', 'manifest.json'));
  const { electronNotices, ...baseline } = manifest;
  assert.equal(manifest.formatVersion, 1);
  if (expected) assert.deepEqual(baseline, expected, 'Packaged notice manifest differs from the build.');
  assert.equal(sha(fs.readFileSync(path.join(resources, noticeName))), manifest.noticeSha256, 'Packaged third-party notice text is missing or changed.');
  for (const item of manifest.projectDocuments) assert.equal(sha(fs.readFileSync(inside(resources, item.path))), item.sha256, `Packaged ${item.path} is changed.`);
  for (const pkg of manifest.packages) for (const document of pkg.documents) assert.equal(sha(fs.readFileSync(inside(path.join(resources, 'third-party-licenses'), document.file))), document.sha256, `Packaged upstream license changed: ${pkg.name}/${document.path}`);
  assert(electronNotices?.length === 2, 'Electron runtime notices were not preserved.');
  for (const filename of ['electron/LICENSE', 'electron/LICENSES.chromium.html']) {
    const item = electronNotices.find(document => document.path === filename);
    assert(item, `Missing Electron document ${filename}`);
    assert.equal(sha(fs.readFileSync(inside(path.join(resources, 'third-party-licenses'), item.path))), item.sha256, `Electron notice changed: ${item.path}`);
    if (filename === 'electron/LICENSE') assert.equal(item.sha256, manifest.packages.find(pkg => pkg.name === 'electron').documents.find(document => document.path === 'LICENSE').sha256, 'Packaged Electron license differs from the installed version.');
  }
  // Artifact dependencies are installed separately. Cached compiled code must
  // not be copied into a desktop release without its own dependency inventory.
  function checkArtifactSource(directory) {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      assert(!['node_modules', 'dist', '.git'].includes(entry.name), `Artifact source includes generated files or Git metadata: ${path.join(directory, entry.name)}`);
      if (entry.isDirectory()) checkArtifactSource(path.join(directory, entry.name));
    }
  }
  for (const name of ['artifact-template', 'prebuilt-artifacts']) {
    const directory = path.join(resources, name);
    if (fs.existsSync(directory)) checkArtifactSource(directory);
  }
  const app = appReader(resources);
  assert.deepEqual(JSON.parse(app.read('package.json').toString('utf8')).name, manifest.app.name);
  assert.equal(JSON.parse(app.read('package.json').toString('utf8')).version, manifest.app.version);
  for (const item of manifest.bundleFiles) assert.equal(sha(app.read(item.path)), item.sha256, `Packaged bundle changed: ${item.path}`);
  const covered = new Map(manifest.packages.map(pkg => [pkg.path, pkg]));
  let dependencies = 0;
  for (const file of app.files()) {
    if (!file.startsWith('node_modules/') || !file.endsWith('/package.json')) continue;
    const marker = file.lastIndexOf('node_modules/') + 'node_modules/'.length;
    const segments = file.slice(marker).split('/');
    const packagePath = file.slice(0, marker) + segments.slice(0, segments[0].startsWith('@') ? 2 : 1).join('/');
    // Internal package.json files (for example lib/cjs/package.json) inherit
    // the enclosing published package's licenses; nested node_modules do not.
    if (file !== packagePath + '/package.json') continue;
    const pkg = JSON.parse(app.read(file).toString('utf8'));
    const expected = covered.get(packagePath);
    assert(expected && expected.name === pkg.name && expected.version === pkg.version, `Packaged dependency has no generated notices: ${pkg.name}@${pkg.version}`);
    dependencies++;
  }
  console.log(`Verified packaged notices, Electron/Chromium credits, ${manifest.bundleFiles.length} build outputs, and ${dependencies} packaged dependencies in ${resources}.`);
  return manifest;
}
function beforePack(context) {
  generate(context.packager.projectDir);
}
function afterPack(context) {
  const root = context.packager.projectDir;
  const expected = check(root);
  const resources = context.packager.getResourcesDir(context.appOutDir);
  const destination = path.join(resources, 'third-party-licenses', 'electron');
  fs.mkdirSync(destination, { recursive: true });
  const candidates = [context.appOutDir, resources];
  const electronLicense = expected.packages.find(pkg => pkg.name === 'electron').documents.find(document => document.path === 'LICENSE');
  if (context.electronPlatformName === 'darwin') candidates.push(context.packager.getMacOsElectronFrameworkResourcesDir(context.appOutDir));
  const electronNotices = [];
  for (const [filename, alternatives] of [['LICENSE', ['LICENSE.electron.txt', 'LICENSE']], ['LICENSES.chromium.html', ['LICENSES.chromium.html']]]) {
    const source = candidates.flatMap(base => alternatives.map(name => path.join(base, name))).find(file => fs.existsSync(file) && (filename !== 'LICENSE' || sha(fs.readFileSync(file)) === electronLicense.sha256));
    if (!source) throw new Error(`Electron distribution is missing ${filename}; packaging stopped.`);
    const content = fs.readFileSync(source);
    if (!content.length) throw new Error(`Electron distribution has an empty ${filename}`);
    fs.writeFileSync(path.join(destination, filename), content);
    electronNotices.push({ path: `electron/${filename}`, sha256: sha(content) });
  }
  fs.writeFileSync(path.join(resources, 'third-party-licenses', 'manifest.json'), JSON.stringify({ ...expected, electronNotices }, null, 2) + '\n');
  verify(resources, expected);
}
function main(args) {
  const [command, directory, ...extra] = args;
  if (extra.length) throw new Error('Usage: third-party-notices.cjs generate | check | verify <packaged-app-directory>');
  if (command === 'generate' && !directory) generate();
  else if (command === 'check' && !directory) { check(); console.log('Generated third-party notices are current.'); }
  else if (command === 'verify' && directory) verify(directory);
  else throw new Error('Usage: third-party-notices.cjs generate | check | verify <packaged-app-directory>');
}
if (require.main === module) {
  try { main(process.argv.slice(2)); }
  catch (error) { console.error(`Third-party notices: ${error.message}`); process.exitCode = 1; }
}
module.exports = { afterPack, assemble, beforePack, check, documents, generate, resourcesDirectory, sha, verify };
