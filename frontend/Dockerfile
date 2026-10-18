# MemoryManagerGraph 前端镜像（本地与云端同构，§14.5）。
# 首屏性能审计 P3：弃用 vite preview（官方定位仅为预览，非生产服务器），
# 构建产物改由 nginx 托管，静态服务策略与生产宿主机 nginx 对齐。
FROM node:24-slim AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
RUN npm run build

FROM nginx:1.27-alpine

COPY nginx.default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
