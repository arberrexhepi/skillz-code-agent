const fs = require('node:fs');

try {
  // Electron 44 repairs a missing runtime lazily from this module entrypoint.
  // electron-vite reads path.txt directly, so resolve Electron first.
  const executable = require('electron');
  if (!fs.existsSync(executable)) {
    throw new Error(`Electron executable was not found at ${executable}`);
  }
  console.log(`Electron runtime ready: ${executable}`);
} catch (error) {
  console.error(`Electron runtime setup failed: ${error instanceof Error ? error.message : String(error)}`);
  process.exitCode = 1;
}
