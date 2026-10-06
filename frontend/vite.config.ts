import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({
  plugins: [react()],
  build: { assetsDir: 'static' },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', ws: true },
      '/assets/teams': { target: 'http://127.0.0.1:8000' },
    },
  },
});
