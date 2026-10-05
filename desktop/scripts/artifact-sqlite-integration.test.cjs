const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const load = require('./load-ts.cjs');
const { ArtifactsService, ArtifactLibraryService, RuntimeSettingsService, removeArtifactDependencyVolumes, command } = load(() => ({
  ...require('../src/main/services/artifacts.ts'),
  ...require('../src/main/services/artifactLibrary.ts'),
  ...require('../src/main/services/runtimeSettings.ts'),
  ...require('../src/main/services/artifactDockerCleanup.ts'),
  ...require('../src/main/services/artifactProcess.ts'),
}));

function completed(child) {
  return new Promise((resolve, reject) => {
    let output = '';
    child.stdout.on('data', data => { output += data; });
    child.stderr.on('data', data => { output += data; });
    child.on('error', reject);
    child.on('close', code => code === 0 ? resolve(output) : reject(new Error(output)));
    child.stdin.end();
  });
}

test('SQLite previews repair skipped native installs, retain database data, and reuse healthy bindings', { timeout: 480000 }, async t => {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'skillz sqlite integration ')));
  const library = new ArtifactLibraryService(path.join(root, 'settings.json'), path.resolve(__dirname, '../artifact-template'), path.join(root, 'contexts'));
  const service = new ArtifactsService(library, new RuntimeSettingsService(path.join(root, 'runtime.json')), () => {});
  let seed;
  t.after(async () => {
    await service.dispose();
    await seed?.stop();
    const records = (await library.library()).artifacts;
    await removeArtifactDependencyVolumes(records.map(record => record.root), root);
    assert.equal(fs.realpathSync(path.dirname(root)), fs.realpathSync(os.tmpdir()));
    assert.ok(path.basename(root).startsWith('skillz sqlite integration '));
    await fs.promises.rm(root, { recursive: true, force: true, maxRetries: 5 });
  });
  await library.configure(path.join(root, 'library'));
  const record = await library.create({ title: 'SQLite repair', prompt: 'Store notes', sourceRoot: '', shareFacts: false, shareMemory: false, app: { version: 1, database: { enabled: true, dialect: 'sqlite' } } });
  // Reproduce a populated dependency volume whose lifecycle scripts never ran.
  fs.writeFileSync(path.join(record.root, '.npmrc'), 'ignore-scripts=true\n');
  seed = await service.sandbox(record.id);
  const prepared = await seed.prepare(text => process.stdout.write(text));
  const missing = await completed(seed.spawn(prepared.docker, prepared.args, ['sh', '-c', 'mkdir -p "$HOME" && npm install --ignore-scripts --no-audit --no-fund && node -e \'try { require("sqlite3"); process.exit(1); } catch (error) { if (!error.message.includes("Could not locate the bindings file")) throw error; console.log("REPRODUCED_MISSING_SQLITE_BINDING"); }\'']));
  assert.match(missing, /REPRODUCED_MISSING_SQLITE_BINDING/);
  const dependencies = await service.dependencySetup(record.id);
  assert.equal((await dependencies.status())[0].ready, false);
  console.log('Reproduced missing SQLite binding; starting real preview for repair.');
  let runtime;
  try { runtime = await service.start(record.id); }
  catch (error) { console.error(service.runtimes.get(record.id)?.state.logs); throw error; }
  assert.equal(runtime.status, 'running');
  assert.match(runtime.logs, /SQLite native binding repaired/);
  assert.equal((await dependencies.status())[0].ready, true);
  async function sql(name, statement, replacements = {}) {
    const response = await fetch(runtime.url + '/_skillz/commands/' + name, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'x-skillz-approve-mutation': 'true' },
      body: JSON.stringify({ sql: statement, replacements }),
    });
    const body = await response.json();
    assert.equal(response.status, 200, JSON.stringify(body));
    return body.rows;
  }
  await sql('db_execute', 'CREATE TABLE notes (text TEXT NOT NULL)');
  await sql('db_execute', 'INSERT INTO notes (text) VALUES (:text)', { text: 'Persisted ë' });
  assert.deepEqual(await sql('db_query', 'SELECT text FROM notes'), [{ text: 'Persisted ë' }]);
  await service.stop(record.id);
  runtime = await service.start(record.id);
  assert.doesNotMatch(runtime.logs, /Repairing the SQLite native binding/);
  assert.deepEqual(await sql('db_query', 'SELECT text FROM notes'), [{ text: 'Persisted ë' }]);
  // Simulate an existing volume losing its native build, then use Setup's repair path.
  const running = service.runtimes.get(record.id).sandbox;
  await command(prepared.docker, ['exec', running.name, 'node', '-e', 'require("fs").unlinkSync(require.resolve("sqlite3/build/Release/node_sqlite3.node"))'], record.root);
  await service.stop(record.id);
  assert.equal((await dependencies.status())[0].ready, false);
  let repairLog = '';
  await dependencies.install(text => { repairLog += text; });
  assert.match(repairLog, /SQLite native binding repaired/);
  assert.equal((await dependencies.status())[0].ready, true);
  runtime = await service.start(record.id);
  assert.deepEqual(await sql('db_query', 'SELECT text FROM notes'), [{ text: 'Persisted ë' }]);
  // A fresh second artifact gets its own empty database at the same relative path.
  const other = await library.create({ title: 'Separate SQLite', prompt: 'Store separate notes', sourceRoot: '', shareFacts: false, shareMemory: false, app: { version: 1, database: { enabled: true, dialect: 'sqlite' } } });
  const firstRuntime = runtime;
  runtime = await service.start(other.id);
  assert.deepEqual(await sql('db_query', "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'notes'"), []);
  await sql('db_execute', 'CREATE TABLE notes (text TEXT NOT NULL)');
  await sql('db_execute', "INSERT INTO notes (text) VALUES ('Other artifact')");
  runtime = firstRuntime;
  assert.deepEqual(await sql('db_query', 'SELECT text FROM notes'), [{ text: 'Persisted ë' }]);
  console.log('SQLite repair, real SQL queries, persistence, and warm restart verified.');
});
