.PHONY: install run dev build deploy clean venv

venv:
	test -d venv || python3 -m venv venv

install: venv
	./venv/bin/pip install -r requirements.txt

run: install
	ENV=dev ./venv/bin/python app.py

dev: run

build: install

deploy:
	apps-platform app deploy

clean:
	rm -rf venv
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
