import { promises as fs } from 'node:fs';
import type { ArtifactCapability } from '../../shared/artifacts';
import { artifactDependencyVolume, artifactRuntimeImage, dockerCommand } from './artifactSandbox';
import { command } from './artifactProcess';

export async function sqliteCapability(root: string): Promise<ArtifactCapability> {
  const item: ArtifactCapability = { id: 'sqlite', label: 'SQLite native driver', ready: false, installable: true, detail: 'Install or repair SQLite in this artifact’s Docker dependency volume. Database files are preserved.' };
  try {
    const docker = await dockerCommand();
    const volume = artifactDependencyVolume(await fs.realpath(root));
    const image = await artifactRuntimeImage();
    // Inspection must not pull images or create an empty dependency volume.
    await command(docker, ['image', 'inspect', image], root);
    await command(docker, ['volume', 'inspect', volume], root);
    await command(docker, ['run', '--rm', '--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=64', '--memory=256m', '--workdir=/repo', '--mount', 'type=volume,src=' + volume + ',dst=/repo/node_modules,readonly', image, 'node', '-e', 'require("sqlite3")'], root);
    return { ...item, ready: true, installable: false, detail: 'SQLite native binding loads successfully in the Linux runtime.' };
  } catch {
    return item;
  }
}
