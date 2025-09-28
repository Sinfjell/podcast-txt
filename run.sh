#!/bin/bash

# Podcast Transcriber Docker Runner
# This script makes it easy to run the podcast transcriber app

echo "🎙️  Podcast Transcriber Docker Setup"
echo "====================================="

# Check if .env file exists
if [ ! -f .env ]; then
    echo "⚠️  No .env file found!"
    echo "📝 Creating .env file from template..."
    cp .env.example .env
    echo "✅ Created .env file"
    echo "🔑 Please edit .env file and add your OpenAI API key"
    echo "   Then run this script again."
    exit 1
fi

# Check if OpenAI API key is set (handle both quoted and unquoted formats)
if ! grep -q "OPENAI_API_KEY.*sk-" .env; then
    echo "⚠️  OpenAI API key not found in .env file"
    echo "🔑 Please edit .env file and add your OpenAI API key"
    echo "   Format: OPENAI_API_KEY=sk-your-key-here"
    echo "   Or: OPENAI_API_KEY=\"sk-your-key-here\""
    exit 1
fi

echo "✅ Environment configured"
echo "🐳 Starting Docker container..."

# Create temp directory if it doesn't exist
mkdir -p temp

# Run with docker-compose
docker-compose up --build

echo "🎉 App should be running at http://localhost:5002"
