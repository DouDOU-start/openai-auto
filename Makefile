.PHONY: dev web fmt lint test

UV ?= uv
HOST ?= 0.0.0.0
PORT ?= 8765

dev:
	$(UV) run uvicorn protocol_reg.dev_server:app --reload --host $(HOST) --port $(PORT)

web:
	$(UV) run protocol-reg-web --host $(HOST) --port $(PORT)
