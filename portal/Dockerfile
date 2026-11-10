# --- build the static bundle ---
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
# The proxy reaches npm as build args; BUILD_TLS_VERIFY=false skips its
# certificate checks, for a proxy that re-signs them.
ARG BUILD_TLS_VERIFY=true
RUN if [ "$BUILD_TLS_VERIFY" = "false" ]; then npm config set strict-ssl false; fi; \
    npm ci
COPY . .
RUN npm run build

# --- serve it, proxying /api to the backend ---
FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
