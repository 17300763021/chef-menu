import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  base: '/chef-menu/',
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    fileParallelism: false,
  },
})
