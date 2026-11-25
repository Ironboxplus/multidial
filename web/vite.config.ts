import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

// Built to a static bundle (fixed names multidial.js / multidial.css, no hash)
// so the LuCI template can reference it directly under /luci-static/multidial/.
export default defineConfig({
  plugins: [react()],
  base: '/luci-static/multidial/',
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  build: {
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        entryFileNames: 'multidial.js',
        assetFileNames: 'multidial.[ext]',
        chunkFileNames: 'multidial-[name].js',
      },
    },
  },
})
