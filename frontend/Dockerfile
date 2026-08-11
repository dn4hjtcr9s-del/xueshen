# MemoryManagerGraph 前端镜像（本地与云端同构，§14.5）。
# 构建产物用 vite preview 提供静态服务（4173 端口，与 compose 映射一致）。
FROM node:24-slim AS base

WORKDIR /app

COPY package.json package-lock.json ./
# vite preview 依赖 devDependencies 中的 vite，不能 --omit=dev
RUN npm ci

COPY . .
RUN npm run build

EXPOSE 4173
CMD ["npm", "run", "preview", "--", "--host", "0.0.0.0", "--port", "4173"]
