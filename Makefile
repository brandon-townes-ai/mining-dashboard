.PHONY: install run dev build deploy clean venv

venv:
	test -d venv || python3 -m venv venv

install: venv
	./venv/bin/pip install -r requirements.txt

# Production-like local run: gunicorn, debug off — mirrors the Procfile/Cloud Run.
run: install
	./venv/bin/gunicorn app:app --bind 0.0.0.0:$${PORT:-5001}

# Development: Flask dev server with debug + hot reload (restarts on file save).
dev: install
	ENV=dev ./venv/bin/python app.py

build: install

deploy:
	apps-platform app deploy

clean:
	rm -rf venv
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
