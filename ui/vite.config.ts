import path from 'node:path'
import { fileURLToPath } from 'node:url'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const currentDirectory = path.dirname(fileURLToPath(import.meta.url))

export default defineConfig({
  base: './',
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(currentDirectory, 'src') },
  },
  build: {
    outDir: path.resolve(currentDirectory, '../factory/factory_ui/static'),
    emptyOutDir: true,
  },
})
