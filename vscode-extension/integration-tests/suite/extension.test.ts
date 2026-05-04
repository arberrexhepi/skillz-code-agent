import * as assert from 'node:assert/strict';
import * as vscode from 'vscode';

describe('Python Agent Extension', () => {
  it('activates and registers the open command', async () => {
    const extension = vscode.extensions.getExtension('local.python-agent-extension');
    assert.ok(extension, 'Extension should be available in the extension host');

    await extension.activate();
    assert.equal(extension.isActive, true);

    const commands = await vscode.commands.getCommands(true);
    assert.ok(commands.includes('skillzAgent.openAgent'));
  });

  it('executes the open command without throwing', async () => {
    await vscode.commands.executeCommand('skillzAgent.openAgent');
    assert.ok(true);
  });
});