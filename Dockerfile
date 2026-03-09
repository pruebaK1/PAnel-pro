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
    && pip install flask \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
EXPOSE 8080
CMD ["/app/start.sh"]
