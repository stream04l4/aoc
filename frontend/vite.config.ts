import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/health': 'http://127.0.0.1:9200',
      '/v2': 'http://127.0.0.1:9200',
      '/control': 'http://127.0.0.1:9200',
      '/approvals': 'http://127.0.0.1:9200',
      '/metrics': 'http://127.0.0.1:9200',
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
  },
});
