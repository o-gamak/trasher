# Переменные
VENV := .venv
PYTHON := $(VENV)/bin/python3
PIP := $(VENV)/bin/pip
MAIN_SCRIPT := src/vibecode_test/trasher.py

# Цвета для терминала
YELLOW := \033[0;33m
GREEN  := \033[0;32m
NC     := \033[0m

.PHONY: help install run load-data clean

help:
	@echo "$(YELLOW)Доступные команды:$(NC)"
	@echo "  make install    - Создать окружение и установить зависимости"
	@echo "  make run        - Запуск анализа (Offline Mode, из кэша)"
	@echo "  make load-data  - Запуск сканера (Online Mode, запрос к API)"
	@echo "  make clean      - Удалить окружение и временные файлы"

install:
	@echo "$(YELLOW)Создание виртуального окружения...$(NC)"
	python3 -m venv $(VENV)
	@echo "$(YELLOW)Обновление pip и установка зависимостей...$(NC)"
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@if [ ! -f .env ]; then \
		touch .env; \
		echo "$(GREEN)Файл .env создан. Добавьте туда свои токены.$(NC)"; \
	fi

run:
	@echo "$(GREEN)Запуск анализатора (Offline Mode)...$(NC)"
	$(PYTHON) $(MAIN_SCRIPT) app.load_data=false

load-data:
	@echo "$(YELLOW)Запуск сканера с обновлением данных...$(NC)"
	$(PYTHON) $(MAIN_SCRIPT) app.load_data=true

clean:
	@echo "$(YELLOW)Удаление виртуального окружения и кэша данных...$(NC)"
	rm -rf $(VENV)
	rm -rf `find . -name __pycache__`
	rm -f data/download.json data/download_data.csv data/ready_data.csv
	@echo "$(GREEN)Очистка завершена$(NC)"