FROM node:latest as build-stage
COPY . /www
WORKDIR /www/app
RUN npm install
RUN npm run build

FROM nginx:stable-alpine3.18-slim
COPY nginx.conf /etc/nginx/conf.d/default.conf
RUN rm -rf /usr/share/nginx/html/*
COPY public /usr/share/nginx/html
COPY --from=build-stage /www/app/dist /usr/share/nginx/html/dist
VOLUME /etc/nginx/certs
EXPOSE 8080
EXPOSE 8443
ENTRYPOINT ["nginx", "-g", "daemon off;"]