# ============================================
# Stage 1: Base image with dependencies
# Этот слой кэшируется и пересобирается только при изменении requirements.txt
# ============================================
FROM python:3.11-slim as base

# Устанавливаем системные зависимости (ffmpeg для извлечения/воспроизведения аудио)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg unzip curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем Deno (JS-runtime, нужен yt-dlp для решения JS-challenge YouTube)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh
ENV PATH="/usr/local/bin:${PATH}"

WORKDIR /app

# Обновляем pip
RUN pip install --no-cache-dir --upgrade pip

# Копируем только requirements.txt (для кэширования слоя)
COPY requirements.txt .

# Устанавливаем Python зависимости
RUN pip install --no-cache-dir -r requirements.txt

# ============================================
# Stage 2: Final image with bot code
# ============================================
FROM base

WORKDIR /app

# Копируем весь код бота
COPY . .

# Настраиваем директорию данных (для persistent SQLite базы blacklist.db)
ENV DATA_DIR=/app/data

# Запускаем бота
CMD ["python", "main.py"]

