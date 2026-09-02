const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const load = require('./load-ts.cjs');
const { GitService, GitRepositorySetup } = load(() => ({
  ...require('../src/main/services/git.ts'),
  ...require('../src/renderer/src/components/GitPanel.tsx'),
}));

function fixture(t) {
  const container = fs.mkdtempSync(path.join(os.tmpdir(), 'skillz git init '));
  const root = path.join(container, 'My Project');
  fs.mkdirSync(root);
  // A developer's home/temp directory can itself be inside a Git repository.
  // Keep these fixture discovery checks inside the disposable container.
  const previousCeiling = process.env.GIT_CEILING_DIRECTORIES;
  process.env.GIT_CEILING_DIRECTORIES = container;
  t.after(() => {
    if (previousCeiling === undefined) delete process.env.GIT_CEILING_DIRECTORIES;
    else process.env.GIT_CEILING_DIRECTORIES = previousCeiling;
  });
  let currentRoot = root;
  const workspace = { requireRoot: () => currentRoot, resolve: (value) => path.resolve(currentRoot, value) };
  const service = new GitService(workspace);
  const git = (...args) => execFileSync('git', ['-c', 'core.autocrlf=false', ...args], { cwd: root, encoding: 'utf8' });
  t.after(() => {
    assert.equal(fs.realpathSync(path.dirname(container)), fs.realpathSync(os.tmpdir()));
    assert.ok(path.basename(container).startsWith('skillz git init '));
    fs.rmSync(container, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  });
  return { root, container, service, git, switchRoot: (value) => { currentRoot = value; } };
}

test('ordinary folder reports an uninitialized state and remains untouched until initialization', async (t) => {
  const { root, service, git } = fixture(t);
  fs.writeFileSync(path.join(root, 'notes.md'), 'keep me');
  assert.deepEqual(await service.status(), { isRepository: false, branch: '', ahead: 0, behind: 0, files: [] });
  assert.equal(fs.existsSync(path.join(root, '.git')), false);
  const status = await service.initialize(root);
  assert.equal(status.isRepository, true);
  assert.ok(status.branch);
  assert.equal(status.files.find((file) => file.path === 'notes.md').indexStatus, '?');
  assert.equal(fs.readFileSync(path.join(root, 'notes.md'), 'utf8'), 'keep me');
  assert.deepEqual(await service.history(), []);
  assert.equal(git('ls-files'), '');
  assert.equal(git('remote'), '');
  await service.stage(['notes.md']);
  await service.unstage(['notes.md']);
  assert.equal(git('ls-files'), '');
  await service.stage(['notes.md']);
  git('config', 'user.name', 'Workbench Test');
  git('config', 'user.email', 'workbench@example.invalid');
  assert.equal((await service.commit('First commit')).files.length, 0);
  assert.equal((await service.history())[0].subject, 'First commit');
});

test('existing repositories and folders within a parent repository are not reinitialized', async (t) => {
  const { root, service, git, switchRoot } = fixture(t);
  git('init', '-q');
  const config = fs.readFileSync(path.join(root, '.git', 'config'), 'utf8');
  assert.equal((await service.initialize(root)).isRepository, true);
  assert.equal(fs.readFileSync(path.join(root, '.git', 'config'), 'utf8'), config);
  const child = path.join(root, 'subfolder');
  fs.mkdirSync(child);
  switchRoot(child);
  assert.equal((await service.initialize(child)).isRepository, true);
  assert.equal(fs.existsSync(path.join(child, '.git')), false);
});

test('a linked worktree .git file is recognized as an existing repository', async (t) => {
  const { root, container, service, git, switchRoot } = fixture(t);
  git('init', '-q');
  git('-c', 'user.name=Workbench Test', '-c', 'user.email=workbench@example.invalid', 'commit', '--allow-empty', '-qm', 'Initial');
  const worktree = path.join(container, 'linked-worktree');
  git('worktree', 'add', '--detach', worktree);
  switchRoot(worktree);
  const before = fs.readFileSync(path.join(worktree, '.git'), 'utf8');
  assert.equal((await service.initialize(worktree)).isRepository, true);
  assert.equal(fs.readFileSync(path.join(worktree, '.git'), 'utf8'), before);
});

test('initialization refuses a stale workspace request', async (t) => {
  const { root, container, service, switchRoot } = fixture(t);
  const other = path.join(container, 'Other');
  fs.mkdirSync(other);
  switchRoot(other);
  await assert.rejects(service.initialize(root), /Workspace changed/);
  assert.equal(fs.existsSync(path.join(root, '.git')), false);
  assert.equal(fs.existsSync(path.join(other, '.git')), false);
});

test('workspace switch during repository check cannot initialize another folder', async (t) => {
  const { root, service, switchRoot } = fixture(t);
  const original = service.statusAt.bind(service);
  t.mock.method(service, 'statusAt', async (value) => {
    const status = await original(value);
    switchRoot(path.join(root, 'other'));
    return status;
  });
  await assert.rejects(service.initialize(root), /Workspace changed/);
  assert.equal(fs.existsSync(path.join(root, '.git')), false);
});

test('damaged repository metadata remains an error instead of an initialization offer', async (t) => {
  const { root, service } = fixture(t);
  fs.mkdirSync(path.join(root, '.git'));
  fs.writeFileSync(path.join(root, '.git', 'keep'), 'existing metadata');
  await assert.rejects(service.status(), /not a git repository/);
  await assert.rejects(service.initialize(root), /not a git repository/);
  assert.deepEqual(fs.readdirSync(path.join(root, '.git')), ['keep']);
});

test('unavailable Git and permission errors are not treated as uninitialized folders', async (t) => {
  const { root, service } = fixture(t);
  for (const message of ['spawn git ENOENT', 'fatal: detected dubious ownership in repository', 'fatal: Permission denied']) {
    t.mock.method(service, 'run', async () => { throw new Error(message); });
    await assert.rejects(service.status(), (error) => error.message === message);
    await assert.rejects(service.initialize(root), (error) => error.message === message);
  }
  assert.equal(fs.existsSync(path.join(root, '.git')), false);
});

test('inherited Git directory overrides cannot redirect initialization outside the selected folder', async (t) => {
  const { root, container, service } = fixture(t);
  const old = process.env.GIT_DIR;
  const outside = path.join(container, 'redirected.git');
  process.env.GIT_DIR = outside;
  t.after(() => { if (old === undefined) delete process.env.GIT_DIR; else process.env.GIT_DIR = old; });
  assert.equal((await service.initialize(root)).isRepository, true);
  assert.equal(fs.existsSync(path.join(root, '.git')), true);
  assert.equal(fs.existsSync(outside), false);
});

test('setup explains the folder and first commit, with an accessible pending state and retry', () => {
  const props = { workspaceRoot: 'C:\\My Project', busy: false, error: '', onInitialize: async () => {}, onRefresh: async () => {} };
  const html = renderToStaticMarkup(React.createElement(GitRepositorySetup, props));
  assert.match(html, /Initialize repository/);
  assert.match(html, /C:\\My Project/);
  assert.match(html, /first commit/);
  assert.doesNotMatch(html, /fatal:|Commit message/);
  const pending = renderToStaticMarkup(React.createElement(GitRepositorySetup, { ...props, busy: true }));
  assert.match(pending, /aria-busy="true"/);
  assert.match(pending, /disabled=""[^>]*>Initializing/);
  const failed = renderToStaticMarkup(React.createElement(GitRepositorySetup, { ...props, error: 'Permission denied' }));
  assert.match(failed, /role="alert"/);
  assert.match(failed, /Permission denied/);
  assert.match(failed, /Initialize repository/);
});
