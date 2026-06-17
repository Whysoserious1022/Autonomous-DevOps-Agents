# dashboard/Dockerfile
# Use slim (Debian-based) instead of Alpine to avoid musl/rolldown binary incompatibility
FROM node:20-slim

WORKDIR /app

# Copy dependency configs
COPY package.json ./

# Install packages fresh (ignore stale lock file to pick up package.json changes)
RUN npm install --prefer-offline

# Copy source files
COPY . .

EXPOSE 5173

# Run the Vite dev server (--host 0.0.0.0 is in vite.config.js)
CMD ["npm", "run", "dev"]
