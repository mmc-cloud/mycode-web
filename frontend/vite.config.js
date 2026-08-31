import { defineConfig, loadEnv } from "vite"
import vue from "@vitejs/plugin-vue"

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "")
  return {
    base: "/mycode/",
    plugins: [vue()],
    server: {
      host: "127.0.0.1",
      port: 5173,
      proxy: {
        "/mycode/api": {
          target: env.MYCODE_FASTAPI_TARGET || "http://127.0.0.1:8000",
          changeOrigin: true,
        },
      },
    },
  }
})
