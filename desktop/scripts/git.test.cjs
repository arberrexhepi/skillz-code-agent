const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const loadTypeScript = require('./load-ts.cjs');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

// Load the desktop's actual TS modules without starting Electron or a renderer.
const { GitService, WorkspaceService, GitChangeRow, gitStatusLabel, canDiscard } = loadTypeScript(() => ({
  ...require('../src/main/services/git.ts'),
  ...require('../src/main/services/workspace.ts'),
  ...require('../src/renderer/src/components/GitChangeRow.tsx'),
  ...require('../src/shared/gitStatus.ts'),
}));

async function fixture(t) {
  const container = fs.mkdtempSync(path.join(os.tmpdir(), 'workbench-git-test-'));
  const root = path.join(container, 'repo');
  fs.mkdirSync(root);
  const git = (...args) => execFileSync('git', args, { cwd: root, encoding: 'utf8' });
  git('init', '-q');
  fs.writeFileSync(path.join(root, 'notes.md'), 'committed\n');
  git('add', '--', 'notes.md');
  git('-c', 'user.name=Workbench Test', '-c', 'user.email=workbench@example.invalid', 'commit', '-qm', 'Initial');
  const workspace = new WorkspaceService(() => {});
  workspace.startWatcher = () => {}; // Git tests do not need OS file-watch resources.
  await workspace.open(root);
  t.after(() => {
    workspace.dispose();
    fs.rmSync(container, { recursive: true, force: true });
  });
  return { root, container, git, workspace, service: new GitService(workspace) };
}

test('Markdown statuses distinguish tracked modifications from untracked files', async (t) => {
  const { root, service } = await fixture(t);
  fs.writeFileSync(path.join(root, 'notes.md'), 'modified\n');
  fs.mkdirSync(path.join(root, 'docs'));
  fs.writeFileSync(path.join(root, 'docs/new.md'), 'new\n');
  const { files } = await service.status();
  const tracked = files.find((file) => file.path === 'notes.md');
  const untracked = files.find((file) => file.path === 'docs/new.md');
  assert.equal(gitStatusLabel(tracked).code, 'M');
  assert.equal(gitStatusLabel(untracked).code, 'U');
  assert.match(gitStatusLabel(untracked).title, /Untracked/);
  assert.equal((await service.fileDiff('docs/new.md')).original, '');
});

test('discard restores only unstaged edits and its diff uses the index baseline', async (t) => {
  const { root, service, git } = await fixture(t);
  const file = path.join(root, 'notes.md');
  fs.writeFileSync(file, 'staged\n');
  git('add', '--', 'notes.md');
  fs.writeFileSync(file, 'unstaged\n');
  assert.deepEqual(await service.fileDiff('notes.md'), { path: 'notes.md', original: 'staged\n', modified: 'unstaged\n', language: 'markdown' });
  assert.equal((await service.fileDiff('notes.md', true)).original, 'committed\n');
  const result = await service.discard('notes.md', async () => true, async () => assert.fail('Tracked file must not go to Trash'));
  assert.equal(result.discarded, true);
  assert.equal(fs.readFileSync(file, 'utf8'), 'staged\n');
  assert.equal(git('show', ':notes.md'), 'staged\n');
  assert.equal(result.status.files[0].indexStatus, 'M');
  assert.equal(result.status.files[0].workTreeStatus, ' ');
});

test('cancelling discard never changes the file', async (t) => {
  const { root, service } = await fixture(t);
  fs.writeFileSync(path.join(root, 'notes.md'), 'keep me\n');
  const result = await service.discard('notes.md', async () => false, async () => assert.fail('No trash on cancel'));
  assert.equal(result.discarded, false);
  assert.equal(fs.readFileSync(path.join(root, 'notes.md'), 'utf8'), 'keep me\n');
});

test('untracked discard delegates exactly one file to recoverable Trash', async (t) => {
  const { root, container, service } = await fixture(t);
  const name = 'new [draft].md';
  fs.writeFileSync(path.join(root, name), 'recover me');
  const result = await service.discard(name, async (file) => {
    assert.equal(file.path, name);
    assert.equal(file.indexStatus + file.workTreeStatus, '??');
    return true;
  }, async (target) => {
    assert.equal(target, path.join(root, name));
    fs.renameSync(target, path.join(container, 'trashed.md'));
  });
  assert.equal(result.discarded, true);
  assert.equal(fs.readFileSync(path.join(container, 'trashed.md'), 'utf8'), 'recover me');
  assert.equal(result.status.files.length, 0);
});

test('a file changing during confirmation aborts discard', async (t) => {
  const { root, service } = await fixture(t);
  const file = path.join(root, 'notes.md');
  fs.writeFileSync(file, 'first edit');
  await assert.rejects(service.discard('notes.md', async () => {
    fs.writeFileSync(file, 'new edit arriving during confirmation');
    return true;
  }, async () => assert.fail('No trash')), /changed while confirmation/);
  assert.equal(fs.readFileSync(file, 'utf8'), 'new edit arriving during confirmation');
});

test('workspace switching during confirmation cannot discard in either workspace', async (t) => {
  const { root, container, workspace, service } = await fixture(t);
  fs.writeFileSync(path.join(root, 'notes.md'), 'keep me');
  const other = path.join(container, 'other');
  fs.mkdirSync(other);
  await assert.rejects(service.discard('notes.md', async () => {
    await workspace.open(other);
    return true;
  }, async () => assert.fail('No trash')), /Workspace changed/);
  assert.equal(fs.readFileSync(path.join(root, 'notes.md'), 'utf8'), 'keep me');
});

test('staged content changing during confirmation aborts even when disk content is unchanged', async (t) => {
  const { root, service, git } = await fixture(t);
  const file = path.join(root, 'notes.md');
  fs.writeFileSync(file, 'staged');
  git('add', '--', 'notes.md');
  fs.writeFileSync(file, 'unstaged');
  await assert.rejects(service.discard('notes.md', async () => {
    git('update-index', '--cacheinfo', '100644', git('rev-parse', 'HEAD:notes.md').trim(), 'notes.md');
    return true;
  }, async () => assert.fail('No trash')), /changed while confirmation/);
  assert.equal(fs.readFileSync(file, 'utf8'), 'unstaged');
});

test('discard treats wildcard-looking filenames literally', async (t) => {
  const { root, service, git } = await fixture(t);
  for (const name of ['[x].md', 'x.md']) fs.writeFileSync(path.join(root, name), 'baseline');
  git('add', '-A');
  git('-c', 'user.name=Workbench Test', '-c', 'user.email=workbench@example.invalid', 'commit', '-qm', 'Files');
  for (const name of ['[x].md', 'x.md']) fs.writeFileSync(path.join(root, name), 'changed');
  await service.discard('[x].md', async () => true, async () => assert.fail('No trash'));
  assert.equal(fs.readFileSync(path.join(root, '[x].md'), 'utf8'), 'baseline');
  assert.equal(fs.readFileSync(path.join(root, 'x.md'), 'utf8'), 'changed');
});

test('discard restores a deleted tracked file', async (t) => {
  const { root, service } = await fixture(t);
  fs.unlinkSync(path.join(root, 'notes.md'));
  await service.discard('notes.md', async () => true, async () => assert.fail('No trash'));
  assert.equal(fs.readFileSync(path.join(root, 'notes.md'), 'utf8'), 'committed\n');
});

test('discard rejects tracked files behind an external parent symlink', async (t) => {
  const { root, container, service, git } = await fixture(t);
  const directory = path.join(root, 'docs');
  fs.mkdirSync(directory);
  fs.writeFileSync(path.join(directory, 'note.md'), 'committed');
  git('add', '--', 'docs/note.md');
  git('-c', 'user.name=Workbench Test', '-c', 'user.email=workbench@example.invalid', 'commit', '-qm', 'Nested file');
  const outside = path.join(container, 'outside-docs');
  fs.renameSync(directory, outside);
  fs.symlinkSync(outside, directory);
  await assert.rejects(service.discard('docs/note.md', async () => assert.fail('Unsafe path reached confirmation'), async () => assert.fail('No trash')), /outside the workspace/);
  assert.equal(fs.readFileSync(path.join(outside, 'note.md'), 'utf8'), 'committed');
});

test('broad, outside, symlink, and staged-only targets are rejected before confirmation', async (t) => {
  const { root, container, service, git } = await fixture(t);
  fs.writeFileSync(path.join(root, 'notes.md'), 'staged');
  git('add', '--', 'notes.md');
  const outside = path.join(container, 'outside.md');
  fs.writeFileSync(outside, 'outside');
  fs.symlinkSync(outside, path.join(root, 'link.md'));
  for (const target of ['.', '../outside.md', root, 'notes.md', 'link.md']) {
    await assert.rejects(service.discard(target, async () => assert.fail('Unsafe target reached confirmation'), async () => assert.fail('No trash')));
  }
  assert.equal(fs.readFileSync(outside, 'utf8'), 'outside');
});

test('row renders one meaningful status and an accessible undo control', () => {
  const calls = [];
  const props = {
    file: { path: 'docs/notes.md', indexStatus: ' ', workTreeStatus: 'M' }, busy: false,
    onDiff: () => {}, onStage: () => {}, onUnstage: () => {}, onDiscard: (file) => calls.push(file),
  };
  const html = renderToStaticMarkup(React.createElement(GitChangeRow, props));
  assert.match(html, /data-status="M"/);
  assert.match(html, /aria-label="Discard unstaged changes: docs\/notes.md"/);
  assert.doesNotMatch(html, /\?\?/);
  const row = GitChangeRow(props);
  const discard = React.Children.toArray(row.props.children).find((element) => element.props?.className === 'icon-button git-discard');
  discard.props.onClick();
  assert.deepEqual(calls, ['docs/notes.md']);
  const busy = renderToStaticMarkup(React.createElement(GitChangeRow, { ...props, busy: true }));
  assert.match(busy, /git-discard" disabled=""/);
});

test('untracked, staged, deleted, renamed, and conflict badges are explicit', () => {
  for (const [indexStatus, workTreeStatus, code, discard] of [
    ['?', '?', 'U', true], ['M', ' ', 'M', false], ['M', 'M', 'M', true],
    [' ', 'D', 'D', true], ['R', ' ', 'R', false], ['U', 'U', '!', false], ['A', 'A', '!', false],
  ]) {
    const file = { path: 'notes.md', indexStatus, workTreeStatus };
    assert.equal(gitStatusLabel(file).code, code);
    assert.equal(canDiscard(file), discard);
  }
});
