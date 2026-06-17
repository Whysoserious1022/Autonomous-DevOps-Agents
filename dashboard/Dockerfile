# dashboard/Dockerfile
FROM node:20-alpine

WORKDIR /app

# Copy dependency configs
COPY package*.json ./

# Install packages
RUN npm install

# Copy source files
COPY . .

EXPOSE 5173

# Expose host so the container port-binds correctly to the outside
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
