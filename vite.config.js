import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  root: 'frontend',
  base: '/static/dist/',
  plugins: [vue()],
  build: { outDir: '../static/dist', emptyOutDir: true, target: 'es2020' },
  server: { proxy: { '/api': 'http://127.0.0.1:9000', '/ws': { target: 'ws://127.0.0.1:9000', ws: true } } },
});
