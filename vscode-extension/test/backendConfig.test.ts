import test from 'node:test';
import assert from 'node:assert/strict';
import * as path from 'path';

import { describeBackendScript, extractJsonLikeStringSetting, preferredBackendScriptValue, resolveBackendScript } from '../src/backendConfig';

test('resolveBackendScript defaults to main.py in the repo root', () => {
  assert.equal(resolveBackendScript('/repo', ''), path.join('/repo', 'main.py'));
});

test('resolveBackendScript accepts repo-relative beta entrypoints', () => {
  assert.equal(resolveBackendScript('/repo', 'main_v2.py'), path.join('/repo', 'main_v2.py'));
});

test('resolveBackendScript preserves absolute configured paths', () => {
  assert.equal(resolveBackendScript('/repo', '/tmp/custom_backend.py'), '/tmp/custom_backend.py');
});

test('describeBackendScript treats main_v2.py as beta runtime', () => {
  assert.deepEqual(describeBackendScript('/repo', 'main_v2.py'), {
    resolvedPath: path.join('/repo', 'main_v2.py'),
    scriptName: 'main_v2.py',
    runtimeKey: 'beta',
    runtimeLabel: 'Beta Runtime',
  });
});

test('preferredBackendScriptValue prefers global setting over workspace-scoped values', () => {
  assert.equal(preferredBackendScriptValue({
    value: 'main.py',
    defaultValue: 'main.py',
    globalValue: 'main_v2.py',
    workspaceValue: 'live_test_loop.py',
    workspaceFolderValue: 'main.py',
  }), 'main_v2.py');
});

test('preferredBackendScriptValue falls back to workspace and then default', () => {
  assert.equal(preferredBackendScriptValue({
    value: 'live_test_loop.py',
    defaultValue: 'main.py',
    globalValue: '',
    workspaceValue: 'live_test_loop.py',
    workspaceFolderValue: '',
  }), 'live_test_loop.py');
  assert.equal(preferredBackendScriptValue({
    value: 'main.py',
    defaultValue: 'main.py',
    globalValue: '',
    workspaceValue: '',
    workspaceFolderValue: '',
  }), 'main.py');
});

test('extractJsonLikeStringSetting reads backendScript from settings json text', () => {
  const text = `{
    // legacy workspace setting
    "pythonAgent.backendScript": "/tmp/main_v2.py",
    "editor.minimap.enabled": false,
  }`;
  assert.equal(extractJsonLikeStringSetting(text, 'pythonAgent.backendScript'), '/tmp/main_v2.py');
});

test('extractJsonLikeStringSetting returns undefined when key is absent', () => {
  assert.equal(extractJsonLikeStringSetting('{"editor.tabSize": 2}', 'pythonAgent.backendScript'), undefined);
});