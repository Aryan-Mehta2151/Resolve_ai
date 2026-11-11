FROM node:22-alpine

WORKDIR /workspace/apps/web

COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci

COPY apps/web ./

CMD ["npm", "run", "dev", "--", "--hostname", "0.0.0.0"]
