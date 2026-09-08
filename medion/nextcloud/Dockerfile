FROM nextcloud:34

# Switch to root to install system packages
USER root

# Install OCRmyPDF, Tesseract, and your required language packs
RUN apt-get update && apt-get install -y --no-install-recommends \
    ocrmypdf \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-deu \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
