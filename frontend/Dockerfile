FROM node:24-alpine AS build
WORKDIR /build
# package-lock.json 提交进仓库,npm ci 按锁文件精确安装,保证构建可复现
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
LABEL org.opencontainers.image.title="OpsSOP Frontend" \
      org.opencontainers.image.version="1.1.2" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/liduojia123/OpsSOP"
COPY --from=build /build/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
    CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
