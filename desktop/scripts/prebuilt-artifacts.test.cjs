const assert = require('node:assert/strict');
const { execFileSync, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { check, main, publish, snapshot, verifyPins, destination } = require('./prebuilt-artifacts.cjs');

// Git for Windows shell helpers also need their bundled Unix utilities.
if (process.platform === 'win32') {
  const execPath = execFileSync('git', ['--exec-path'], { encoding: 'utf8' }).trim();
  const utilities = path.resolve(execPath, '../../../usr/bin');
  if (fs.existsSync(utilities)) process.env.PATH = utilities + path.delimiter + process.env.PATH;
}
// All network-style operations in this suite use temporary local repositories.
process.env.GIT_ALLOW_PROTOCOL = 'file';
const ids = ['server-manager', 'repo-issue-manager'];
const childPath = (root, id) => path.join(root, 'desktop/prebuilt-artifacts', id);
function git(root, ...args) {
  return execFileSync('git', args, { cwd: root, encoding: 'utf8', windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] }).trim();
}
function commit(root, message) {
  git(root, '-c', 'user.name=Prebuilt Test', '-c', 'user.email=prebuilt@example.invalid', 'commit', '-qm', message);
}
function noApplication(remote) {
  assert.equal(spawnSync('git', ['show-ref', '--verify', '--quiet', 'refs/heads/app'], { cwd: remote, encoding: 'utf8' }).status, 1);
}
function noTemporaryRefs(root) { assert.equal(git(root, 'for-each-ref', '--format=%(refname)', 'refs/skillz-prebuilt-check'), ''); }
function clone(container, remote, name, recursive = true) {
  const root = path.join(container, name);
  git(container, '-c', 'protocol.file.allow=always', 'clone', '--quiet', '--branch', 'app', ...(recursive ? ['--recurse-submodules'] : []), remote, root);
  return root;
}
function fixture(t) {
  const container = fs.mkdtempSync(path.join(os.tmpdir(), 'skillz-prebuilt-test-'));
  t.after(() => {
    const resolved = fs.realpathSync(container);
    assert.equal(path.dirname(resolved), fs.realpathSync(os.tmpdir()));
    assert.ok(path.basename(resolved).startsWith('skillz-prebuilt-test-'));
    fs.rmSync(resolved, { recursive: true, force: true });
  });
  const root = path.join(container, 'author');
  const original = path.join(container, 'original.git');
  const target = path.join(container, 'independent.git');
  fs.mkdirSync(root);
  git(root, 'init', '--quiet', '--initial-branch=app');
  git(root, 'config', 'core.autocrlf', 'false');
  for (const remote of [original, target]) git(container, 'init', '--quiet', '--bare', '--initial-branch=app', remote);
  git(root, 'remote', 'add', 'origin', original);
  git(root, 'remote', 'add', 'personal', target);
  let manifest = '';
  for (const id of ids) {
    const child = childPath(root, id);
    fs.mkdirSync(child, { recursive: true });
    git(child, 'init', '--quiet', '--initial-branch=' + 'prebuilt/' + id);
    git(child, 'config', 'core.autocrlf', 'false');
    for (const file of ['artifact.json', 'package.json', 'server/index.ts', 'src/App.tsx']) {
      fs.mkdirSync(path.dirname(path.join(child, file)), { recursive: true });
      fs.writeFileSync(path.join(child, file), file.endsWith('.json') ? '{}\n' : id + ' version one\n');
    }
    git(child, 'add', '--', '.');
    commit(child, 'Initial ' + id);
    const pin = git(child, 'rev-parse', 'HEAD');
    const relative = 'desktop/prebuilt-artifacts/' + id;
    git(root, 'update-index', '--add', '--cacheinfo', '160000,' + pin + ',' + relative);
    manifest += '[submodule "' + relative + '"]\n\tpath = ' + relative + '\n\turl = ./\n\tbranch = prebuilt/' + id + '\n';
  }
  fs.writeFileSync(path.join(root, '.gitmodules'), manifest);
  fs.writeFileSync(path.join(root, 'application.txt'), 'application one\n');
  git(root, 'add', '--', '.gitmodules', 'application.txt');
  commit(root, 'Initial application');
  return { root, container, original, target };
}
function advance(root, id, message = 'version two') {
  const child = childPath(root, id);
  fs.writeFileSync(path.join(child, 'src/App.tsx'), id + ' ' + message + '\n');
  git(child, 'add', '--', 'src/App.tsx');
  commit(child, message);
  git(root, 'add', '--', 'desktop/prebuilt-artifacts/' + id);
  commit(root, 'Pin ' + message);
}
const quiet = () => {};

test('an independent destination and a second machine receive both prebuilt histories and later changes', t => {
  const { root, container, original, target } = fixture(t);
  publish(root, 'origin', quiet);
  const developer = clone(container, original, 'developer');
  git(developer, 'remote', 'add', 'personal', target);
  advance(developer, 'server-manager');
  publish(developer, 'personal', quiet);
  assert.equal(git(developer, 'rev-parse', '--abbrev-ref', '@{upstream}'), 'personal/app');
  // Previously initialized child remotes must follow the new parent upstream.
  main(['init'], developer);
  for (const id of ids) assert.equal(git(childPath(developer, id), 'remote', 'get-url', 'origin').replaceAll('\\', '/').replace(/\/$/, ''), target.replaceAll('\\', '/'));
  const second = clone(container, target, 'second-machine');
  check(second);
  for (const item of snapshot(developer).modules) {
    assert.equal(git(childPath(second, item.id), 'rev-parse', 'HEAD'), item.pin);
    assert.equal(git(childPath(second, item.id), 'remote', 'get-url', 'origin').replaceAll('\\', '/').replace(/\/$/, ''), target.replaceAll('\\', '/'));
  }
  advance(developer, 'repo-issue-manager', 'version three');
  publish(developer, 'personal', quiet);
  git(second, 'pull', '--quiet', '--no-recurse-submodules');
  main(['init'], second);
  assert.match(fs.readFileSync(path.join(childPath(second, 'repo-issue-manager'), 'src/App.tsx'), 'utf8'), /version three/);
  assert.match(fs.readFileSync(path.join(childPath(second, 'server-manager'), 'src/App.tsx'), 'utf8'), /version two/);
  verifyPins(second, target, snapshot(second).modules);
  noTemporaryRefs(developer);
  noTemporaryRefs(second);
});

test('verification rejects missing destination histories even when original remotes and local objects contain them', t => {
  const { root, original, target } = fixture(t);
  publish(root, 'origin', quiet);
  assert.throws(() => verifyPins(root, target, snapshot(root).modules), /Destination is missing/);
  const oldPin = git(childPath(root, 'server-manager'), 'rev-parse', 'HEAD');
  advance(root, 'server-manager');
  git(root, 'push', '--no-recurse-submodules', target, oldPin + ':refs/heads/prebuilt/server-manager');
  git(original, 'push', '--no-recurse-submodules', target, 'refs/heads/prebuilt/repo-issue-manager:refs/heads/prebuilt/repo-issue-manager');
  assert.throws(() => verifyPins(root, target, snapshot(root).modules), /does not contain the pinned/);
  noApplication(target);
  noTemporaryRefs(root);
});

test('dirty prebuilts, staged pointers, and uncommitted relative URLs stop publication', t => {
  const { root, target } = fixture(t);
  const child = childPath(root, 'server-manager');
  git(root, 'config', 'submodule.desktop/prebuilt-artifacts/server-manager.ignore', 'all');
  fs.writeFileSync(path.join(child, 'uncommitted.txt'), 'local only');
  assert.throws(() => publish(root, 'personal', quiet), /Commit changes inside/);
  git(child, 'add', '--', 'uncommitted.txt');
  commit(child, 'Child changes');
  assert.throws(() => publish(root, 'personal', quiet), /updated server-manager pointer/);
  git(root, 'add', '--', 'desktop/prebuilt-artifacts/server-manager');
  assert.throws(() => publish(root, 'personal', quiet), /updated submodule pointers/);
  commit(root, 'Updated pin');
  git(root, 'config', '--file', '.gitmodules', 'submodule.desktop/prebuilt-artifacts/server-manager.url', 'https://example.invalid/original.git');
  assert.throws(() => publish(root, 'personal', quiet), /Working .gitmodules/);
  noApplication(target);
});

test('a plain clone fails the build check and initialization restores the committed pins', t => {
  const { root, container, original } = fixture(t);
  publish(root, 'origin', quiet);
  const plain = clone(container, original, 'plain', false);
  assert.throws(() => check(plain), /submodules:init/);
  const prior = process.env.GIT_ALLOW_PROTOCOL;
  process.env.GIT_ALLOW_PROTOCOL = 'file';
  try { main(['init'], plain); }
  finally { if (prior === undefined) delete process.env.GIT_ALLOW_PROTOCOL; else process.env.GIT_ALLOW_PROTOCOL = prior; }
  check(plain);
  for (const item of snapshot(plain).modules) assert.equal(git(childPath(plain, item.id), 'rev-parse', 'HEAD'), item.pin);
});

test('a newer destination branch is preserved when it already contains the pinned version', t => {
  const { root, target } = fixture(t);
  publish(root, 'personal', quiet);
  const oldHead = git(root, 'rev-parse', 'HEAD');
  advance(root, 'server-manager');
  publish(root, 'personal', quiet);
  const newTip = git(target, 'rev-parse', 'refs/heads/prebuilt/server-manager');
  git(root, 'switch', '--quiet', '--create', 'release-old', oldHead);
  git(childPath(root, 'server-manager'), 'checkout', '--quiet', snapshot(root).modules.find(item => item.id === 'server-manager').pin);
  publish(root, 'personal', quiet);
  assert.equal(git(target, 'rev-parse', 'refs/heads/prebuilt/server-manager'), newTip);
  verifyPins(root, target, snapshot(root).modules);
  noTemporaryRefs(root);
});

test('diverged prebuilt histories are rejected before publishing the application', t => {
  const { root, target } = fixture(t);
  const unrelated = path.join(path.dirname(root), 'unrelated');
  fs.mkdirSync(unrelated);
  git(unrelated, 'init', '--quiet');
  fs.writeFileSync(path.join(unrelated, 'other.txt'), 'other');
  git(unrelated, 'add', '--', '.');
  commit(unrelated, 'Unrelated history');
  git(unrelated, 'push', target, 'HEAD:refs/heads/prebuilt/server-manager');
  assert.throws(() => publish(root, 'personal', quiet), /has diverged/);
  noApplication(target);
  noTemporaryRefs(root);
});

test('a server rejection of prebuilt publication prevents the application push', t => {
  const { root, target } = fixture(t);
  const hook = path.join(target, 'hooks/pre-receive');
  fs.writeFileSync(hook, '#!/bin/sh\nwhile read old new ref; do\ncase "$ref" in refs/heads/prebuilt/*) echo "prebuilt denied" >&2; exit 1;; esac\ndone\n');
  fs.chmodSync(hook, 0o755);
  assert.throws(() => publish(root, 'personal', quiet), /prebuilt denied|hook declined/);
  noApplication(target);
  assert.equal(git(target, 'for-each-ref', '--format=%(refname)', 'refs/heads/prebuilt'), '');
  noTemporaryRefs(root);
});

test('multiple push destinations and detached application HEAD are rejected without remote changes', t => {
  const { root, target, original } = fixture(t);
  git(root, 'config', '--add', 'remote.personal.pushurl', target);
  git(root, 'config', '--add', 'remote.personal.pushurl', original);
  assert.throws(() => destination(root, 'personal'), /exactly one push URL/);
  assert.throws(() => destination(root, '--all'), /configured Git remote/);
  git(root, 'checkout', '--quiet', '--detach');
  assert.throws(() => publish(root, 'origin', quiet), /Switch the application to a branch/);
  noApplication(target);
  noApplication(original);
});


test('application publication uses the captured commit and destination even if a branch or remote moves', t => {
  const { root, target, original } = fixture(t);
  const captured = snapshot(root);
  publish(root, 'personal', message => {
    if (message.startsWith('Prebuilt pins verified')) {
      advance(root, 'server-manager', 'concurrent edit');
      git(root, 'remote', 'set-url', 'personal', original);
    }
  });
  assert.equal(git(target, 'rev-parse', 'refs/heads/app'), captured.head);
  verifyPins(root, target, captured.modules);
  noApplication(original);
  assert.notEqual(git(root, 'rev-parse', 'HEAD'), captured.head);
  noTemporaryRefs(root);
});
