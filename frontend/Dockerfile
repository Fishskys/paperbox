FROM docker.io/library/node:24-alpine AS builder
WORKDIR /webui
COPY package.json package-lock.json ./
RUN npm ci
COPY . ./
RUN npm run build

FROM docker.io/library/nginx:stable-alpine
ENV PAPERBOX_API_UPSTREAM=http://api:8077
ENV PAPERBOX_PROXY_TIMEOUT=240s
COPY deploy/default.conf.template /etc/nginx/templates/default.conf.template
COPY deploy/15-paperbox-resolver.envsh /docker-entrypoint.d/15-paperbox-resolver.envsh
RUN chmod +x /docker-entrypoint.d/15-paperbox-resolver.envsh
COPY --from=builder /webui/dist /usr/share/nginx/html
EXPOSE 80