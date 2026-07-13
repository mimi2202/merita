# Dev-mode image. The arena is a live tape — you WILL be tweaking it against real settlement
# data right up until the demo, and a production build step in the loop just means you find
# out about the broken flex layout ninety seconds before you present.
# Swap to `npm run build` + nginx when the UI stops moving.

FROM node:22-slim
WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci || npm install

COPY . .

EXPOSE 5173
# --host is mandatory: Vite binds 127.0.0.1 by default, which inside a container means
# "reachable only from inside the container". Classic hour-long head-scratch.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
