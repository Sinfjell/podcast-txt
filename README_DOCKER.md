# Podcast Transcriber - Docker Setup

This document explains how to run the Podcast Transcriber app using Docker.

## Quick Start

### Option 1: Using the Run Script (Easiest)
```bash
./run.sh
```

### Option 2: Manual Docker Compose
```bash
# 1. Copy environment template
cp .env.example .env

# 2. Edit .env file and add your OpenAI API key
# OPENAI_API_KEY=sk-your-key-here

# 3. Run the app
docker-compose up --build
```

### Option 3: Direct Docker Run
```bash
# Build the image
docker build -t podcast-transcriber .

# Run with environment variable
docker run -d \
  -p 5002:5002 \
  -e OPENAI_API_KEY=your_key_here \
  -v $(pwd)/temp:/app/temp \
  --name podcast-transcriber \
  podcast-transcriber
```

## Environment Variables

Create a `.env` file with the following variables:

```env
# Required
OPENAI_API_KEY=sk-your-openai-api-key-here

# Optional
SECRET_KEY=your-secret-key-change-in-production
FLASK_ENV=development
```

## Accessing the App

Once running, open your browser and go to:
- **http://localhost:5002**

## Features

- ✅ **One-click setup** with Docker
- ✅ **Audio processing** with FFmpeg support
- ✅ **Health checks** to ensure app is running
- ✅ **Persistent storage** for temporary files
- ✅ **Production ready** with proper configuration
- ✅ **Easy deployment** to any Docker-compatible platform

## Docker Commands

```bash
# Start the app
docker-compose up

# Start in background
docker-compose up -d

# Stop the app
docker-compose down

# View logs
docker-compose logs -f

# Rebuild after code changes
docker-compose up --build

# Remove everything (including volumes)
docker-compose down -v
```

## Troubleshooting

### Port Already in Use
If port 5002 is already in use, edit `docker-compose.yml` and change the port mapping:
```yaml
ports:
  - "5003:5002"  # Use port 5003 instead
```

### Permission Issues
Make sure the `run.sh` script is executable:
```bash
chmod +x run.sh
```

### Environment Variables Not Working
Make sure your `.env` file is in the same directory as `docker-compose.yml` and has the correct format:
```env
OPENAI_API_KEY=sk-your-actual-key-here
```

## Development

For development, you can mount your code directory:
```bash
docker-compose -f docker-compose.yml -f docker-compose.dev.yml up
```

Create `docker-compose.dev.yml`:
```yaml
version: '3.8'
services:
  podcast-transcriber:
    volumes:
      - .:/app
    environment:
      - FLASK_ENV=development
```

## Production Deployment

The same Docker setup can be deployed to:
- Railway
- Render
- DigitalOcean App Platform
- AWS ECS
- Google Cloud Run
- Any Docker-compatible platform

Just make sure to set the environment variables in your hosting platform.
