import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// The dev server proxies API traffic to the local compose stack so the
// browser stays same-origin during walkthroughs (the API deliberately has
// no CORS middleware; cross-origin fetches fail closed).
export default defineConfig({
  plugins: [vue()],
  build: { sourcemap: false },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
