FROM python:3.11-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    ffmpeg firefox-esr xvfb pulseaudio \
    xdotool x11-utils x11vnc websockify \
    sqlite3 curl wget ca-certificates \
    libdbus-glib-1-2 libgtk-3-0 libx11-xcb1 \
    python3-pip \
    && pip install flask yt-dlp \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
EXPOSE 8080
EXPOSE 5900
EXPOSE 6080
CMD ["python", "panel.py"]
