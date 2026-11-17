const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const load = require('./load-ts.cjs');
const { ArtifactLibraryService, ArtifactPrebuiltUpdates, ArtifactsService, RuntimeSettingsService, git, prebuiltBaselineFile } = load(() => ({
  ...require('../src/main/services/artifactLibrary.ts'), ...require('../src/main/services/artifactPrebuiltUpdates.ts'),
  ...require('../src/main/services/artifactPrebuiltSource.ts'), ...require('../src/main/services/artifacts.ts'),
  ...require('../src/main/services/runtimeSettings.ts'), ...require('../src/main/services/artifactProcess.ts'),
}));
const read = (root, relative) => fs.readFile(path.join(root, relative), 'utf8');
const write = (root, relative, value) => fs.writeFile(path.join(root, relative), value);
async function fixture(t) {
  const root = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'skillz-prebuilt-updates-ë ')));
  t.after(() => fs.rm(root, { recursive: true, force: true, maxRetries: 5 }));
  const source = path.join(root, 'bundles/beta-observatory');
  await fs.mkdir(path.join(source, 'src'), { recursive: true }); await fs.mkdir(path.join(source, '.artifact'));
  await write(source, 'package.json', JSON.stringify({ name: 'fixture', dependencies: {}, scripts: {} }));
  await write(source, '.gitignore', 'node_modules/\ndist/\n.artifact-data/\n.artifact-local.json\n');
  await write(source, '.artifact/apis.json', '{"version":1,"apis":[]}');
  const initial = ['title = original', ...Array.from({ length: 12 }, (_, i) => 'line ' + i), 'status = old', ''].join('\n');
  await write(source, 'src/App.tsx', initial);
  const library = new ArtifactLibraryService(path.join(root, 'settings.json'), path.resolve(__dirname, '../artifact-template'), path.join(root, 'contexts'), path.join(root, 'bundles'));
  await library.configure(path.join(root, 'library'));
  const record = await library.installPrebuilt('beta-observatory', { directories: [], allowWorkspaceRead: false }, { provider: 'gemini', model: 'fixture', backendScript: 'main_v2.py' });
  return { root, source, record, library, updates: new ArtifactPrebuiltUpdates(library, root), initial };
}

test('only maintained source changes trigger updates, excluding builds, settings and installed edits', async t => {
  const { source, record, updates, initial } = await fixture(t);
  assert.deepEqual(await updates.status(record.id), { available: false, changedFiles: 0 });
  await write(record.root, 'src/App.tsx', initial.replace('title = original', 'title = custom'));
  for (const root of [source, record.root]) { await fs.mkdir(path.join(root, 'dist')); await write(root, 'dist/output.js', 'generated'); await write(root, '.env', 'private'); }
  await write(record.root, 'package.json', '{"scripts":{},"dependencies":{"pg":"local"},"name":"fixture"}');
  assert.equal((await updates.status(record.id)).available, false);
  await write(source, 'src/App.tsx', initial.replace('status = old', 'status = new'));
  assert.deepEqual(await updates.status(record.id), { available: true, changedFiles: 1 });
  assert.equal((await updates.review(record.id)).files[0].action, 'merge');
});

test('updates merge local edits, add/remove files, preserve configuration and staging, and back up original bytes', async t => {
  const { source, record, library, updates, initial, root } = await fixture(t);
  const local = initial.replace('title = original', 'title = custom'); await write(record.root, 'src/App.tsx', local);
  await write(source, 'src/App.tsx', initial.replace('status = old', 'status = new')); await write(source, 'src/new.ts', 'incoming');
  const baselineFile = prebuiltBaselineFile(root, record.root), baseline = JSON.parse(await fs.readFile(baselineFile, 'utf8'));
  baseline.files['src/removed.ts'] = { data: Buffer.from('retired').toString('base64'), executable: false }; await fs.writeFile(baselineFile, JSON.stringify(baseline)); await write(record.root, 'src/removed.ts', 'retired');
  await write(record.root, 'custom.txt', 'unrelated'); await git(record.root, 'add', '--', 'custom.txt'); const index = await git(record.root, 'diff', '--cached');
  await fs.mkdir(path.join(record.root, '.artifact-data')); await write(record.root, '.artifact-data/recording.json', 'retained');
  const meta = await read(record.root, 'artifact.json'), apis = await read(record.root, '.artifact/apis.json');
  const review = await updates.review(record.id), result = await updates.apply(record.id, { token: review.token, resolutions: {} });
  assert.equal(result.updatedFiles, 3); assert.match(await read(record.root, 'src/App.tsx'), /title = custom/); assert.match(await read(record.root, 'src/App.tsx'), /status = new/);
  assert.equal(await read(record.root, 'src/new.ts'), 'incoming'); await assert.rejects(read(record.root, 'src/removed.ts'), { code: 'ENOENT' });
  assert.equal(await read(record.root, 'artifact.json'), meta); assert.equal(await read(record.root, '.artifact/apis.json'), apis); assert.equal(await read(record.root, '.artifact-data/recording.json'), 'retained');
  assert.deepEqual((await library.find(record.id)).runtime, record.runtime); assert.equal(await git(record.root, 'diff', '--cached'), index);
  const backup = JSON.parse(await fs.readFile(result.backup, 'utf8')); assert.equal(Buffer.from(backup.files['src/App.tsx'].data, 'base64').toString(), local); assert.ok(backup.missing.includes('src/new.ts'));
  assert.equal((await updates.status(record.id)).available, false);
});

test('conflicts require explicit choices; keeping local source acknowledges the bundle', async t => {
  const { source, record, updates, initial } = await fixture(t);
  await write(record.root, 'src/App.tsx', initial.replace('title = original', 'title = local')); await write(source, 'src/App.tsx', initial.replace('title = original', 'title = incoming'));
  const review = await updates.review(record.id); assert.equal(review.files[0].action, 'conflict');
  await assert.rejects(updates.apply(record.id, { token: review.token, resolutions: {} }), /Choose which version/);
  await assert.rejects(updates.apply(record.id, { token: review.token, resolutions: { '../../outside': 'incoming' } }), /listed conflict/);
  await updates.apply(record.id, { token: review.token, resolutions: { 'src/App.tsx': 'installed' } }); assert.match(await read(record.root, 'src/App.tsx'), /title = local/); assert.equal((await updates.status(record.id)).available, false);
  await write(source, 'src/App.tsx', initial.replace('title = original', 'title = incoming again')); const next = await updates.review(record.id); assert.equal(next.files[0].action, 'conflict');
  await updates.apply(record.id, { token: next.token, resolutions: { 'src/App.tsx': 'incoming' } }); assert.equal(await read(record.root, 'src/App.tsx'), await read(source, 'src/App.tsx'));
});

test('stale reviews cannot overwrite newer changes to either copy', async t => {
  const { source, record, updates, initial } = await fixture(t); await write(source, 'src/App.tsx', initial + 'incoming'); const review = await updates.review(record.id);
  await write(record.root, 'src/App.tsx', initial + 'local edit'); await assert.rejects(updates.apply(record.id, { token: review.token, resolutions: {} }), /changed after this review/); assert.equal(await read(record.root, 'src/App.tsx'), initial + 'local edit');
  const next = await updates.review(record.id); await write(source, 'src/App.tsx', initial + 'new incoming'); await assert.rejects(updates.apply(record.id, { token: next.token, resolutions: {} }), /changed after this review/);
});

test('legacy installs retain provenance after rename and description changes; custom namesakes have no updates', async t => {
  const { source, record, library, initial, root } = await fixture(t); await fs.rm(prebuiltBaselineFile(root, record.root));
  const localFile = path.join(root, 'artifact-records.json'), records = JSON.parse(await fs.readFile(localFile, 'utf8')); delete records[record.root].prebuiltId; await fs.writeFile(localFile, JSON.stringify(records));
  const meta = JSON.parse(await read(record.root, 'artifact.json')); delete meta.prebuiltId; meta.title = 'Renamed'; await write(record.root, 'artifact.json', JSON.stringify(meta));
  const reloaded = new ArtifactLibraryService(path.join(root, 'settings.json'), path.resolve(__dirname, '../artifact-template'), path.join(root, 'contexts'), path.join(root, 'bundles'));
  const registry = await reloaded.prebuilts(); t.mock.method(reloaded, 'prebuilts', async () => registry.map(item => ({ ...item, description: 'New description' }))); assert.equal((await reloaded.find(record.id)).prebuiltId, 'beta-observatory');
  const updates = new ArtifactPrebuiltUpdates(reloaded, root); assert.equal((await updates.status(record.id)).available, false); await write(source, 'src/App.tsx', initial + 'incoming'); assert.equal((await updates.status(record.id)).available, true);
  const custom = await library.create({ title: 'Beta Observatory', prompt: 'Custom namesake', sourceRoot: '', shareFacts: false, shareMemory: false }); assert.deepEqual(await updates.status(custom.id), { available: false, changedFiles: 0 });
});

test('manually installed source is acknowledged without a false update alert', async t => {
  const { source, record, updates, initial } = await fixture(t); await write(source, 'src/App.tsx', initial + 'copied'); await write(record.root, 'src/App.tsx', initial + 'copied'); assert.equal((await updates.status(record.id)).available, false);
  await write(record.root, 'src/App.tsx', initial + 'copied\ncustomization'); assert.equal((await updates.status(record.id)).available, false);
});

test('package updates preserve capability-owned database drivers even when choosing incoming', async t => {
  const { source, record, updates } = await fixture(t); await write(record.root, 'package.json', JSON.stringify({ name: 'local', dependencies: { pg: 'local', sequelize: 'local' }, scripts: {} })); await write(source, 'package.json', JSON.stringify({ name: 'incoming', dependencies: { express: 'new' }, scripts: {} }));
  const review = await updates.review(record.id); await updates.apply(record.id, { token: review.token, resolutions: review.files[0].action === 'conflict' ? { 'package.json': 'incoming' } : {} }); assert.deepEqual(JSON.parse(await read(record.root, 'package.json')).dependencies, { express: 'new', pg: 'local', sequelize: 'local' });
});

test('linked installed paths cannot write outside the artifact', async t => {
  const { root, source, record, updates, initial } = await fixture(t); const outside = path.join(root, 'outside'); await fs.mkdir(outside); await write(outside, 'App.tsx', 'outside'); await fs.rm(path.join(record.root, 'src'), { recursive: true });
  try { await fs.symlink(outside, path.join(record.root, 'src'), process.platform === 'win32' ? 'junction' : 'dir'); } catch (error) { if (process.platform === 'win32' && error.code === 'EPERM') { t.skip('Windows symlink privilege unavailable'); return; } throw error; }
  await write(source, 'src/App.tsx', initial + 'incoming'); assert.match((await updates.status(record.id)).error, /not a regular file/); await assert.rejects(updates.review(record.id), /not a regular file/); assert.equal(await read(outside, 'App.tsx'), 'outside');
});

test('applying stops only the owning preview and agent and blocks new starts during the update', async t => {
  const { root, library, record, source, initial } = await fixture(t); const events = [], stops = [];
  const service = new ArtifactsService(library, new RuntimeSettingsService(path.join(root, 'runtime.json')), event => events.push(event)); t.after(() => service.dispose()); t.mock.method(service, 'stop', async id => { stops.push(id); });
  service.agents.set(record.id, { agent: { stop: async () => stops.push('editing:' + record.id) }, workspace: { dispose() {} } }); service.agents.set('sibling', { agent: { stop: async () => stops.push('editing:sibling') }, workspace: { dispose() {} } });
  await write(source, 'src/App.tsx', initial + 'incoming'); const review = await service.prebuiltUpdates.review(record.id), apply = service.prebuiltUpdates.apply.bind(service.prebuiltUpdates);
  t.mock.method(service.prebuiltUpdates, 'apply', async (id, request) => { await assert.rejects(service.start(id), /being updated/); await assert.rejects(service.startAgent(id, {}), /being updated/); await assert.rejects(service.saveAccess(id, {}), /update to finish/); await assert.rejects(service.agent(id), /being updated/); return apply(id, request); });
  await service.applyPrebuiltUpdate(record.id, { token: review.token, resolutions: {} }); assert.deepEqual(stops, [record.id, 'editing:' + record.id]); assert.ok(events.some(event => event.type === 'prebuilt-updated' && event.id === record.id));
});


test('line ending differences in Windows checkouts do not create a false update', async t => {
  const { source, record, updates, initial } = await fixture(t);
  await write(source, 'src/App.tsx', initial.replaceAll('\n', '\r\n'));
  await write(record.root, 'src/App.tsx', initial.replaceAll('\n', '\r\n'));
  assert.deepEqual(await updates.status(record.id), { available: false, changedFiles: 0 });
  await write(source, 'src/App.tsx', initial.replace('status = old', 'status = new').replaceAll('\n', '\r\n'));
  const review = await updates.review(record.id), result = await updates.apply(record.id, { token: review.token, resolutions: {} });
  const backup = JSON.parse(await fs.readFile(result.backup, 'utf8'));
  assert.equal(Buffer.from(backup.files['src/App.tsx'].data, 'base64').toString(), initial.replaceAll('\n', '\r\n'));
});

test('a write failure rolls back completed writes and retains the backup and old baseline', async t => {
  const { source, record, updates, initial } = await fixture(t);
  await write(source, 'src/App.tsx', initial + 'incoming'); await write(source, 'src/Z.ts', 'new file');
  const review = await updates.review(record.id), rename = fs.rename.bind(fs);
  t.mock.method(fs, 'rename', async (from, to) => { if (to === path.join(record.root, 'src/Z.ts')) throw new Error('Fixture write failure'); return rename(from, to); });
  await assert.rejects(updates.apply(record.id, { token: review.token, resolutions: {} }), /Fixture write failure/);
  assert.equal(await read(record.root, 'src/App.tsx'), initial);
  await assert.rejects(read(record.root, 'src/Z.ts'), { code: 'ENOENT' });
  assert.equal((await updates.status(record.id)).available, true);
});

test('incoming removal of locally edited source is a conflict and binary conflicts require a choice', async t => {
  const { source, record, updates, initial } = await fixture(t);
  await write(record.root, 'src/App.tsx', initial + 'local customization'); await fs.rm(path.join(source, 'src/App.tsx'));
  await write(source, 'image.bin', Buffer.from([0, 1, 2])); await write(record.root, 'image.bin', Buffer.from([0, 4, 5]));
  const review = await updates.review(record.id); assert.equal(review.files.length, 2); assert.ok(review.files.every(file => file.action === 'conflict')); assert.equal(review.files.find(file => file.path === 'image.bin').binary, true);
  await updates.apply(record.id, { token: review.token, resolutions: { 'src/App.tsx': 'incoming', 'image.bin': 'installed' } });
  await assert.rejects(read(record.root, 'src/App.tsx'), { code: 'ENOENT' }); assert.deepEqual(await fs.readFile(path.join(record.root, 'image.bin')), Buffer.from([0, 4, 5])); assert.equal((await updates.status(record.id)).available, false);
});


test('a partial manual update reviews only remaining changes', async t => {
  const { source, record, updates, initial } = await fixture(t);
  await write(source, 'src/App.tsx', initial + 'copied'); await write(record.root, 'src/App.tsx', initial + 'copied');
  await write(source, 'src/new.ts', 'remaining update');
  assert.deepEqual(await updates.status(record.id), { available: true, changedFiles: 1 });
  assert.deepEqual((await updates.review(record.id)).files.map(file => file.path), ['src/new.ts']);
});


test('an incomplete bundle cannot turn a missing checkout into deletion of the installed source', async t => {
  const { source, record, updates, initial } = await fixture(t);
  await fs.rm(path.join(source, 'package.json'));
  assert.match((await updates.status(record.id)).error, /incomplete/);
  await assert.rejects(updates.review(record.id), /incomplete/);
  assert.equal(await read(record.root, 'src/App.tsx'), initial);
});

test('a capability migration in progress blocks a prebuilt source update', async t => {
  const { root, library, record } = await fixture(t);
  const service = new ArtifactsService(library, new RuntimeSettingsService(path.join(root, 'runtime.json')), () => {});
  t.after(() => service.dispose());
  let release;
  t.mock.method(library, 'saveApp', () => new Promise(resolve => { release = resolve; }));
  const saving = service.saveApp(record.id, { version: 1 });
  await assert.rejects(service.applyPrebuiltUpdate(record.id, { token: 'a'.repeat(64), resolutions: {} }), /current artifact operation/);
  release({ migrated: false }); await saving;
});


test('Agent Observatory recognizes the old install title without renaming custom artifacts', async t => {
  const { root, record, library } = await fixture(t);
  const meta = JSON.parse(await read(record.root, 'artifact.json'));
  meta.title = 'Beta Observatory'; delete meta.prebuiltId;
  await write(record.root, 'artifact.json', JSON.stringify(meta));
  await git(record.root, 'add', '--', 'artifact.json');
  await git(record.root, 'commit', '--amend', '--no-edit');
  const localFile = path.join(root, 'artifact-records.json');
  const records = JSON.parse(await fs.readFile(localFile, 'utf8'));
  delete records[record.root].prebuiltId;
  await fs.writeFile(localFile, JSON.stringify(records));
  const reloaded = new ArtifactLibraryService(path.join(root, 'settings.json'), path.resolve(__dirname, '../artifact-template'), path.join(root, 'contexts'), path.join(root, 'bundles'));
  const legacy = await reloaded.find(record.id);
  assert.equal(legacy.title, 'Agent Observatory');
  assert.equal(legacy.prebuiltId, 'beta-observatory');
  assert.deepEqual(legacy.runtime, record.runtime);
  meta.title = 'My Observatory'; await write(record.root, 'artifact.json', JSON.stringify(meta));
  assert.equal((await reloaded.find(record.id)).title, 'My Observatory');
  const custom = await library.create({ title: 'Beta Observatory', prompt: 'Custom namesake', sourceRoot: '', shareFacts: false, shareMemory: false });
  assert.equal((await reloaded.find(custom.id)).title, 'Beta Observatory');
  assert.equal((await reloaded.find(custom.id)).prebuiltId, undefined);
});
