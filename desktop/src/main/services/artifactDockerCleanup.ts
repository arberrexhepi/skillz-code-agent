import { promises as fs } from 'node:fs';
import { command } from './artifactProcess';
import { artifactDependencyVolume, artifactRuntimeImage, dockerCommand, harnessRoot } from './artifactSandbox';
import type { ArtifactDockerCleanupPlan, ArtifactDockerCleanupResult } from '../../shared/artifacts';

const imagePattern = /^skillz-artifact:[a-f0-9]{20}$/;
const volumePattern = /^skillz-artifact-deps-[a-f0-9]{20}$/;

interface CleanupTools {
  dockerCommand: typeof dockerCommand;
  command: typeof command;
  artifactRuntimeImage: typeof artifactRuntimeImage;
}

const defaultTools: CleanupTools = { dockerCommand, command, artifactRuntimeImage };
function lines(output: string): string[] { return output.split(/\r?\n/).map(value => value.trim()).filter(Boolean); }
function sorted(values: Iterable<string>): string[] { return [...new Set(values)].sort((a, b) => a.localeCompare(b)); }

export async function planArtifactDockerCleanup(installedRoots: string[], source = harnessRoot(), tools: CleanupTools = defaultTools): Promise<ArtifactDockerCleanupPlan> {
  const docker = await tools.dockerCommand();
  const [currentImage, imageOutput, containerImageOutput, volumeOutput, attachedVolumeOutput, canonicalRoots] = await Promise.all([
    tools.artifactRuntimeImage(source),
    tools.command(docker, ['image', 'ls', '--filter', 'reference=skillz-artifact:*', '--format', '{{.Repository}}:{{.Tag}}'], source),
    tools.command(docker, ['ps', '--all', '--filter', 'label=agency.aiam.skillz.artifact=true', '--format', '{{.Image}}'], source),
    tools.command(docker, ['volume', 'ls', '--format', '{{.Name}}'], source),
    tools.command(docker, ['volume', 'ls', '--filter', 'dangling=false', '--format', '{{.Name}}'], source),
    Promise.all(installedRoots.map(root => fs.realpath(root))),
  ]);
  const images = lines(imageOutput).filter(value => imagePattern.test(value));
  const volumes = lines(volumeOutput).filter(value => volumePattern.test(value));
  const preservedImages = new Set([currentImage, ...lines(containerImageOutput).filter(value => imagePattern.test(value))]);
  const preservedVolumes = new Set([
    ...canonicalRoots.map(artifactDependencyVolume),
    ...lines(attachedVolumeOutput).filter(value => volumePattern.test(value)),
  ]);
  return {
    currentImage,
    obsoleteImages: sorted(images.filter(value => !preservedImages.has(value))),
    orphanedVolumes: sorted(volumes.filter(value => !preservedVolumes.has(value))),
    preservedImages: sorted(images.filter(value => preservedImages.has(value))),
    preservedVolumes: sorted(volumes.filter(value => preservedVolumes.has(value))),
  };
}

export async function cleanArtifactDockerResources(installedRoots: string[], source = harnessRoot(), tools: CleanupTools = defaultTools): Promise<ArtifactDockerCleanupResult> {
  const plan = await planArtifactDockerCleanup(installedRoots, source, tools);
  const docker = await tools.dockerCommand();
  const removedVolumes: string[] = [], removedImages: string[] = [], failures: string[] = [];
  for (const volume of plan.orphanedVolumes) {
    try { await tools.command(docker, ['volume', 'rm', volume], source); removedVolumes.push(volume); }
    catch (error) { failures.push(`${volume}: ${String(error)}`); }
  }
  for (const image of plan.obsoleteImages) {
    try { await tools.command(docker, ['image', 'rm', image], source); removedImages.push(image); }
    catch (error) { failures.push(`${image}: ${String(error)}`); }
  }
  return { ...plan, removedImages, removedVolumes, failures };
}

export async function removeArtifactDependencyVolumes(roots: string[], cwd: string, tools: Pick<CleanupTools, 'dockerCommand' | 'command'> = defaultTools): Promise<void> {
  const docker = await tools.dockerCommand();
  for (const root of roots) {
    const canonical = await fs.realpath(root);
    await tools.command(docker, ['volume', 'rm', artifactDependencyVolume(canonical)], cwd).catch(error => {
      if (!/no such volume/i.test(String(error))) throw error;
    });
  }
}
