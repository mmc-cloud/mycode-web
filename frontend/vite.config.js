import { defineConfig, loadEnv } from "vite"
import vue from "@vitejs/plugin-vue"
import { API_BASE_PATH, WEB_BASE_PATH } from "./src/base-path.js"

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "")
  return {
    base: `${WEB_BASE_PATH}/`,
    plugins: [vue()],
    server: {
      host: "127.0.0.1",
      port: 5173,
      proxy: {
        [API_BASE_PATH]: {
          target: env.MYCODE_FASTAPI_TARGET || "http://127.0.0.1:8000",
          changeOrigin: true,
          ws: true,
        },
      },
    },
  }
})
