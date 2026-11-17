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
const { GitService, ArtifactGitService, WorkspaceService, GitChangeRow, gitStatusLabel, canDiscard, parseNumstat, populateLineChanges } = loadTypeScript(() => ({
  ...require('../src/main/services/git.ts'),
  ...require('../src/main/services/artifactGit.ts'),
  ...require('../src/main/services/gitLineChanges.ts'),
  ...require('../src/main/services/workspace.ts'),
  ...require('../src/renderer/src/components/GitChangeRow.tsx'),
  ...require('../src/shared/gitStatus.ts'),
}));

async function fixture(t) {
  const container = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'workbench-git-test-')));
  const root = path.join(container, 'repo');
  fs.mkdirSync(root);
  const git = (...args) => execFileSync('git', args, { cwd: root, encoding: 'utf8' });
  git('init', '-q');
  // Keep byte comparisons independent of the user's Windows checkout settings.
  git('config', 'core.autocrlf', 'false');
  fs.writeFileSync(path.join(root, 'notes.md'), 'committed\n');
  git('add', '--', 'notes.md');
  git('-c', 'user.name=Workbench Test', '-c', 'user.email=workbench@example.invalid', 'commit', '-qm', 'Initial');
  const workspace = new WorkspaceService(() => {});
  workspace.startWatcher = () => {}; // Git tests do not need OS file-watch resources.
  await workspace.open(root);
  t.after(() => {
    workspace.dispose();
    assert.equal(fs.realpathSync(path.dirname(container)), fs.realpathSync(os.tmpdir()));
    assert.ok(path.basename(container).startsWith('workbench-git-test-'));
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
  fs.writeFileSync(path.join(outside, 'note.md'), 'outside change');
  fs.symlinkSync(outside, directory, process.platform === 'win32' ? 'junction' : 'dir');
  await assert.rejects(service.discard('docs/note.md', async () => assert.fail('Unsafe path reached confirmation'), async () => assert.fail('No trash')), /outside the workspace/);
  assert.equal(fs.readFileSync(path.join(outside, 'note.md'), 'utf8'), 'outside change');
});

test('broad, outside, and staged-only targets are rejected before confirmation', async (t) => {
  const { root, container, service, git } = await fixture(t);
  fs.writeFileSync(path.join(root, 'notes.md'), 'staged');
  git('add', '--', 'notes.md');
  const outside = path.join(container, 'outside.md');
  fs.writeFileSync(outside, 'outside');
  for (const target of ['.', '../outside.md', root, 'notes.md']) {
    await assert.rejects(service.discard(target, async () => assert.fail('Unsafe target reached confirmation'), async () => assert.fail('No trash')));
  }
  assert.equal(fs.readFileSync(outside, 'utf8'), 'outside');
});

test('external file symlinks are rejected before discard confirmation', async (t) => {
  const { root, container, service } = await fixture(t);
  const outside = path.join(container, 'outside.md');
  fs.writeFileSync(outside, 'outside');
  try {
    fs.symlinkSync(outside, path.join(root, 'link.md'), 'file');
  } catch (error) {
    if (process.platform === 'win32' && error.code === 'EPERM') {
      t.skip('Windows requires Developer Mode or symlink privileges for file symlinks');
      return;
    }
    throw error;
  }
  await assert.rejects(service.discard('link.md', async () => assert.fail('Symlink reached confirmation'), async () => assert.fail('No trash')));
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

test('workspace and artifact rows receive net line counts, including staged plus unstaged edits', async t => {
  const { root, service, git } = await fixture(t);
  const artifact = new ArtifactGitService({ find: async () => ({ root }) });
  const counts = async expected => {
    for (const status of await Promise.all([service.status(), artifact.status('fixture')])) {
      assert.deepEqual(status.files.find(file => file.path === 'notes.md').lineChanges, expected);
    }
  };
  fs.writeFileSync(path.join(root, 'notes.md'), 'one\ntwo\n');
  await counts({ additions: 2, deletions: 1 });
  git('add', '--', 'notes.md');
  fs.writeFileSync(path.join(root, 'notes.md'), 'one\ntwo\nthree\n');
  await counts({ additions: 3, deletions: 1 });
  // Summing staged and unstaged stats would overcount a change reverted to HEAD.
  fs.writeFileSync(path.join(root, 'notes.md'), 'committed\n');
  await counts({ additions: 0, deletions: 0 });
});

test('new, empty, binary and deleted files get truthful deltas', async t => {
  const { root, service } = await fixture(t);
  fs.writeFileSync(path.join(root, 'new file.md'), 'first\r\nsecond');
  fs.writeFileSync(path.join(root, 'empty.txt'), '');
  fs.writeFileSync(path.join(root, 'image.bin'), Buffer.from([1, 0, 2, 10]));
  fs.unlinkSync(path.join(root, 'notes.md'));
  const files = (await service.status()).files;
  const changes = name => files.find(file => file.path === name).lineChanges;
  assert.deepEqual(changes('new file.md'), { additions: 2, deletions: 0 });
  assert.deepEqual(changes('empty.txt'), { additions: 0, deletions: 0 });
  assert.deepEqual(changes('image.bin'), { binary: true });
  assert.deepEqual(changes('notes.md'), { additions: 0, deletions: 1 });
});

test('rename counts use the destination and combine a full rewrite with the original deletion', async t => {
  const { root, service, git } = await fixture(t);
  git('mv', 'notes.md', 'renamed notes.md');
  let file = (await service.status()).files.find(file => file.path === 'renamed notes.md');
  assert.deepEqual(file.lineChanges, { additions: 0, deletions: 0 });
  fs.writeFileSync(path.join(root, 'renamed notes.md'), 'new first\nnew second\n');
  file = (await service.status()).files.find(file => file.path === 'renamed notes.md');
  assert.deepEqual(file.lineChanges, { additions: 2, deletions: 1 });
});

test('tracked binary changes are labelled without inventing line counts', async t => {
  const { root, service, git } = await fixture(t);
  fs.writeFileSync(path.join(root, 'image.bin'), Buffer.from([0, 1, 2]));
  git('add', '--', 'image.bin');
  git('-c', 'user.name=Workbench Test', '-c', 'user.email=workbench@example.invalid', 'commit', '-qm', 'Binary fixture');
  fs.writeFileSync(path.join(root, 'image.bin'), Buffer.from([0, 3, 4, 5]));
  assert.deepEqual((await service.status()).files.find(file => file.path === 'image.bin').lineChanges, { binary: true });
});

test('separate added and deleted rows retain counts when status rename detection is disabled', async t => {
  const { service, git } = await fixture(t);
  git('config', 'status.renames', 'false');
  git('mv', 'notes.md', 'renamed notes.md');
  const files = (await service.status()).files;
  assert.deepEqual(files.find(file => file.path === 'notes.md').lineChanges, { additions: 0, deletions: 1 });
  assert.deepEqual(files.find(file => file.path === 'renamed notes.md').lineChanges, { additions: 1, deletions: 0 });
});

test('initial repository counts include the actual working content of newly staged files', async t => {
  const { root, service, git } = await fixture(t);
  fs.rmSync(path.join(root, '.git'), { recursive: true, force: true });
  git('init', '-q');
  git('config', 'core.autocrlf', 'false');
  git('add', '--', 'notes.md');
  fs.writeFileSync(path.join(root, 'notes.md'), 'first\nsecond\nthird');
  assert.deepEqual((await service.status()).files.find(file => file.path === 'notes.md').lineChanges, { additions: 3, deletions: 0 });
});

test('untracked symlinks count their link blob and never the external target', async t => {
  const { root, container, service } = await fixture(t);
  fs.writeFileSync(path.join(container, 'external.txt'), 'external\nprivate\ncontent\n');
  try { fs.symlinkSync(path.join(container, 'external.txt'), path.join(root, 'link.txt'), 'file'); }
  catch (error) { if (process.platform === 'win32' && error.code === 'EPERM') { t.skip('Symlinks require Windows Developer Mode'); return; } throw error; }
  assert.deepEqual((await service.status()).files.find(file => file.path === 'link.txt').lineChanges, { additions: 1, deletions: 0 });
});

test('numstat preserves tabs, newlines, binary markers and NUL-separated renames', () => {
  const parsed = parseNumstat(['2\t1\tfile\twith\nwhitespace.ts', '-\t-\timage.bin', '0\t0\t', 'old\tname', 'new\nname', ''].join('\0'));
  assert.deepEqual(parsed.get('file\twith\nwhitespace.ts'), { additions: 2, deletions: 1 });
  assert.deepEqual(parsed.get('image.bin'), { binary: true });
  assert.deepEqual(parsed.get('new\nname'), { additions: 0, deletions: 0 });
  assert.equal(parsed.has('old\tname'), false);
});

test('unresolved conflicts and unavailable stats omit the numeric badge', async t => {
  const { root } = await fixture(t);
  const conflict = { path: 'notes.md', indexStatus: 'U', workTreeStatus: 'U' };
  await populateLineChanges(root, [conflict], '3\t2\tnotes.md\0', false);
  assert.equal(conflict.lineChanges, undefined);
  const unavailable = { path: 'notes.md', indexStatus: ' ', workTreeStatus: 'M' };
  await populateLineChanges(root, [unavailable], undefined, false);
  assert.equal(unavailable.lineChanges, undefined);
});

test('shared file row renders accessible addition/deletion badges and a distinct binary label', () => {
  const props = { file: { path: 'notes.md', indexStatus: ' ', workTreeStatus: 'M', lineChanges: { additions: 12, deletions: 3 } }, busy: false,
    onDiff: () => {}, onStage: () => {}, onUnstage: () => {}, onDiscard: () => {} };
  const html = renderToStaticMarkup(React.createElement(GitChangeRow, props));
  assert.match(html, /12 lines added, 3 lines deleted/);
  assert.match(html, />\+12<.*>−3</);
  const binary = renderToStaticMarkup(React.createElement(GitChangeRow, { ...props, file: { ...props.file, lineChanges: { binary: true } } }));
  assert.match(binary, />Binary</);
  assert.doesNotMatch(binary, /lines added/);
});
