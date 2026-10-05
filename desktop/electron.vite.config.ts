import { resolve } from 'node:path';
import { defineConfig, externalizeDepsPlugin } from 'electron-vite';
import react from '@vitejs/plugin-react';
import { thirdPartyNoticesPlugin } from './scripts/third-party-notices-plugin';

export default defineConfig({
  main: {
    plugins: [externalizeDepsPlugin(), thirdPartyNoticesPlugin('main', __dirname)],
  },
  preload: {
    plugins: [externalizeDepsPlugin(), thirdPartyNoticesPlugin('preload', __dirname)],
  },
  renderer: {
    server: {
      port: 55173,
    },
    resolve: {
      alias: {
        '@renderer': resolve('src/renderer/src'),
        '@shared': resolve('src/shared'),
      },
    },
    plugins: [react(), thirdPartyNoticesPlugin('renderer', __dirname)],
  },
});
