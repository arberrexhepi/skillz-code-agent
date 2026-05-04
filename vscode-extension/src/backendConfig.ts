import * as path from 'path';

export interface BackendScriptSettingValues {
  value?: string;
  defaultValue?: string;
  globalValue?: string;
  workspaceValue?: string;
  workspaceFolderValue?: string;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function firstNonEmptyValue(...values: Array<string | undefined>): string | undefined {
  for (const value of values) {
    const trimmed = String(value || '').trim();
    if (trimmed) {
      return trimmed;
    }
  }
  return undefined;
}

export function preferredBackendScriptValue(values: BackendScriptSettingValues): string | undefined {
  return firstNonEmptyValue(
    values.globalValue,
    values.workspaceValue,
    values.workspaceFolderValue,
    values.value,
    values.defaultValue,
  );
}

export function extractJsonLikeStringSetting(text: string, key: string): string | undefined {
  const pattern = new RegExp(`"${escapeRegExp(key)}"\\s*:\\s*("(?:\\\\.|[^"\\\\])*")`);
  const match = pattern.exec(text);
  if (!match) {
    return undefined;
  }
  try {
    return JSON.parse(match[1]);
  } catch {
    return undefined;
  }
}

export function resolveBackendScript(repoRoot: string, configuredScript: string | undefined): string {
  const trimmed = String(configuredScript || '').trim();
  if (!trimmed) {
    return path.join(repoRoot, 'main.py');
  }
  if (path.isAbsolute(trimmed)) {
    return trimmed;
  }
  return path.join(repoRoot, trimmed);
}

export interface BackendScriptInfo {
  resolvedPath: string;
  scriptName: string;
  runtimeKey: 'stable' | 'beta' | 'custom';
  runtimeLabel: string;
}

export function describeBackendScript(repoRoot: string, configuredScript: string | undefined): BackendScriptInfo {
  const resolvedPath = resolveBackendScript(repoRoot, configuredScript);
  const scriptName = path.basename(resolvedPath);

  if (scriptName === 'main.py') {
    return {
      resolvedPath,
      scriptName,
      runtimeKey: 'stable',
      runtimeLabel: 'Stable Runtime',
    };
  }

  if (scriptName === 'live_test_loop.py' || scriptName === 'main_v2.py') {
    return {
      resolvedPath,
      scriptName,
      runtimeKey: 'beta',
      runtimeLabel: 'Beta Runtime',
    };
  }

  return {
    resolvedPath,
    scriptName,
    runtimeKey: 'custom',
    runtimeLabel: 'Custom Runtime',
  };
}