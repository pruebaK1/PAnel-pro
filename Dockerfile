FROM python:3.11-slim
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    ffmpeg \
    firefox-esr \
    xvfb \
    pulseaudio \
    xdotool \
    wmctrl \
    x11-utils \
    x11vnc \
    websockify \
    openbox \
    xclip \
    curl \
    wget \
    procps \
    fonts-dejavu \
    && pip install flask playwright \
    && playwright install chromium \
    && playwright install-deps chromium \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
RUN chmod +x /app/start.sh
EXPOSE 8080
CMD ["/app/start.sh"]
