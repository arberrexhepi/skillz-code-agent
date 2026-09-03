import os from 'node:os';
import path from 'node:path';

/** Finder does not source shell startup files. Extend child PATH without running a shell. */
export function hostEnvironment(base: NodeJS.ProcessEnv = process.env, platform: NodeJS.Platform = process.platform, home = os.homedir()): NodeJS.ProcessEnv {
  if (platform !== 'darwin') return base;
  const locations = [
    '/opt/homebrew/bin', '/usr/local/bin',
    path.posix.join(home, '.docker/bin'),
    '/Applications/Docker.app/Contents/Resources/bin',
    path.posix.join(home, 'Applications/Docker.app/Contents/Resources/bin'),
  ];
  const current = (base.PATH || '/usr/bin:/bin:/usr/sbin:/sbin').split(':').filter(Boolean);
  return { ...base, PATH: [...new Set([...current, ...locations])].join(':') };
}
