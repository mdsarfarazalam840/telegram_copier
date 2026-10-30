import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/browser',
  use: { baseURL: 'http://127.0.0.1:4185', headless: true },
  webServer: {
    command: 'npx vite preview --host 127.0.0.1 --port 4185 --strictPort',
    url: 'http://127.0.0.1:4185',
    reuseExistingServer: false,
  },
});
