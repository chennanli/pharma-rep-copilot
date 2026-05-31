# Pharma Rep Copilot — common workflows.
#
# Two entry points cover most people:
#   make demo       — one-click: uv setup, docker up, synth seed, open browser.  ~3 min
#   make demo-real  — same but pulls real CMS data via the CMS Data API.        ~10-30 min
#
# Everything else is a building block of those two.

.PHONY: help demo demo-real setup uv-install venv deps up down logs \
        db-init seed-synth seed-real-cms run open clean reset doctor data-status \
        install-docker

UV    := $(shell command -v uv 2>/dev/null)
PY    := .venv/bin/python
PIP   := .venv/bin/pip
PORT  := 8080
URL   := http://localhost:$(PORT)

help:
	@echo ""
	@echo "Pharma Rep Copilot — make targets"
	@echo ""
	@echo "  make demo          One-click: setup + docker up + synth seed + open browser"
	@echo "  make demo-real     Same but with real CMS data (downloads via CMS API, ~10 min)"
	@echo ""
	@echo "Building blocks:"
	@echo "  make doctor          Check prerequisites — run this first if something fails"
	@echo "  make install-docker  One-shot: install OrbStack via brew, launch it"
	@echo "  make setup           Install uv if missing, create venv, install deps"
	@echo "  make up            docker compose up -d (Postgres + app)"
	@echo "  make down          docker compose down (keeps volumes)"
	@echo "  make logs          tail app logs"
	@echo "  make data-status   show row counts in warehouse + memory store"
	@echo "  make db-init       Apply schemas / tables / read-only role"
	@echo "  make seed-synth    Load ~500 HCPs of plausible synthetic data (no download)"
	@echo "  make seed-real     Download CA CMS data via API + ingest"
	@echo "  make run           Start uvicorn locally (no docker — for hot reload dev)"
	@echo "  make open          Open the UI in default browser"
	@echo "  make clean         Stop containers + wipe data (DESTRUCTIVE)"
	@echo ""

# ---------- one-click flows ----------

demo: doctor setup up wait-pg db-init seed-synth open
	@echo ""
	@echo "✓ Demo ready. UI is at $(URL)"

demo-real: doctor setup up wait-pg db-init seed-real open
	@echo ""
	@echo "✓ Demo (real CMS) ready. UI is at $(URL)"

# ---------- one-shot installers ----------

install-docker:
	@if ! command -v brew >/dev/null 2>&1; then \
	  echo "✗ Homebrew not found. Install brew first:"; \
	  echo '   /bin/bash -c "$$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'; \
	  exit 1; \
	fi
	@if command -v docker >/dev/null 2>&1; then \
	  echo "✓ Docker already installed: $$(docker --version)"; \
	  if ! docker info >/dev/null 2>&1; then \
	    echo "▶ Docker is installed but daemon not running. Trying to launch ..."; \
	    open -a OrbStack 2>/dev/null || open -a Docker 2>/dev/null || colima start 2>/dev/null \
	      || echo "  ⚠ Couldn't auto-launch — please open Docker manually."; \
	  fi; \
	  exit 0; \
	fi
	@echo "▶ Installing OrbStack via Homebrew (lightweight Docker for Mac, free for personal use)"
	@echo "  (You may be prompted for your Mac password — that's brew installing a system app.)"
	@brew install --cask orbstack
	@echo "▶ Launching OrbStack ..."
	@open -a OrbStack 2>/dev/null \
	  || open /Applications/OrbStack.app 2>/dev/null \
	  || (command -v orb >/dev/null 2>&1 && orb start) \
	  || (echo "  ⚠ Auto-launch didn't work."; \
	      echo "    Open OrbStack manually:  open /Applications/OrbStack.app")
	@echo ""
	@echo "✓ OrbStack is starting up. Wait ~20 seconds for the icon to appear in your menu bar."
	@echo "  Then re-run:  make demo"

# ---------- pre-flight check ----------

doctor:
	@echo "▶ Checking prerequisites ..."
	@MISSING=""; \
	command -v docker >/dev/null 2>&1 || MISSING="$$MISSING docker"; \
	command -v python3 >/dev/null 2>&1 || MISSING="$$MISSING python3"; \
	command -v git    >/dev/null 2>&1 || MISSING="$$MISSING git"; \
	if [ -n "$$MISSING" ]; then \
	  echo ""; \
	  echo "✗ Missing prerequisite(s):$$MISSING"; \
	  echo ""; \
	  case "$$MISSING" in \
	    *docker*) \
	      echo "  Easiest fix (one command):"; \
	      echo "    make install-docker                # installs OrbStack via brew, launches it"; \
	      echo ""; \
	      echo "  Or pick your own:"; \
	      echo "    brew install --cask orbstack       # recommended on Mac — light + fast, free"; \
	      echo "    brew install --cask docker         # Docker Desktop — heavier but official"; \
	      echo "    brew install colima docker         # CLI-only — needs 'colima start'"; \
	      echo "" ;; \
	  esac; \
	  case "$$MISSING" in \
	    *python3*) echo "  Install Python 3.12+ from https://www.python.org/downloads/ or 'brew install python@3.12'"; echo "" ;; \
	  esac; \
	  case "$$MISSING" in \
	    *git*) echo "  Install git: 'brew install git' or https://git-scm.com/download"; echo "" ;; \
	  esac; \
	  exit 1; \
	fi
	@if ! docker info >/dev/null 2>&1; then \
	  echo ""; \
	  echo "✗ Docker is installed but not running."; \
	  echo "  Launch it: open -a OrbStack  (or 'open -a Docker', or 'colima start')"; \
	  echo "  Then re-run 'make demo'."; \
	  exit 1; \
	fi
	@echo "✓ docker:   $$(docker --version)"
	@echo "✓ python3:  $$(python3 --version)"
	@echo "✓ git:      $$(git --version | head -1)"
	@if [ ! -f .env ]; then \
	  echo "⚠ .env not found yet — 'make setup' will create it for you."; \
	elif grep -q '^ANTHROPIC_API_KEY=sk-ant-xxxxx' .env && ! grep -q '^LLM_PROVIDER=ollama' .env; then \
	  echo "⚠ .env still has placeholder ANTHROPIC_API_KEY. Edit it before running queries (or set LLM_PROVIDER=ollama)."; \
	fi

# ---------- building blocks ----------

uv-install:
	@if [ -z "$(UV)" ]; then \
	  echo "▶ Installing uv (Astral) ..."; \
	  curl -LsSf https://astral.sh/uv/install.sh | sh; \
	  echo "  Restart your shell or 'source ~/.bashrc' to pick up uv."; \
	else \
	  echo "✓ uv already installed: $(UV)"; \
	fi

venv: uv-install
	@if [ ! -d .venv ]; then \
	  echo "▶ Creating venv with uv ..."; \
	  uv venv --python 3.12; \
	else \
	  echo "✓ .venv already exists"; \
	fi

deps: venv
	@echo "▶ Installing Python deps with uv ..."
	@uv pip install -r requirements.txt

setup: deps
	@if [ ! -f .env ]; then \
	  cp .env.example .env; \
	  echo "✓ Created .env from .env.example — edit it to set ANTHROPIC_API_KEY"; \
	  echo "  (or set LLM_PROVIDER=ollama in .env to use a local LLM and skip the key)"; \
	else \
	  echo "✓ .env already exists"; \
	fi

up:
	@docker compose up -d
	@echo "✓ docker compose up -d issued"

down:
	@docker compose down

wait-pg:
	@echo "▶ Waiting for Postgres ..."
	@for i in $$(seq 1 30); do \
	  if docker exec hcp-pg pg_isready -U hcp_admin -d hcp_insights >/dev/null 2>&1; then \
	    echo "✓ Postgres ready"; exit 0; \
	  fi; \
	  sleep 1; \
	done; \
	echo "✗ Postgres did not become ready in 30s"; exit 1

db-init:
	@bash scripts/setup_postgres.sh

seed-synth: db-init
	@$(PY) scripts/synth_seed.py
	@$(PY) scripts/seed_drug_alias.py

seed-real: db-init
	@$(PY) scripts/download_cms_via_api.py --state CA
	@$(PY) scripts/derive_npi_registry.py
	@$(PY) scripts/seed_drug_alias.py

logs:
	@docker compose logs -f app

data-status:
	@echo "▶ Warehouse row counts:"
	@docker exec hcp-pg psql -U hcp_admin -d hcp_insights -c "\
SELECT 'payments.general_payments'        AS table_name, COUNT(*) AS rows FROM payments.general_payments \
UNION ALL SELECT 'partd.prescriber_drug_yearly', COUNT(*) FROM partd.prescriber_drug_yearly \
UNION ALL SELECT 'npi.npi_registry',             COUNT(*) FROM npi.npi_registry \
UNION ALL SELECT 'reference.drug_alias',         COUNT(*) FROM reference.drug_alias \
ORDER BY 1;" 2>/dev/null || echo "  (Postgres not running — try 'make up' first)"
	@echo ""
	@echo "▶ Memory store status:"
	@[ -f data/memory.db ] && sqlite3 data/memory.db "SELECT 'sql_examples (' || status || ')' AS what, COUNT(*) AS n FROM sql_examples GROUP BY status UNION ALL SELECT 'hcp_facts (total)', COUNT(*) FROM hcp_facts UNION ALL SELECT 'hcp_facts (distinct NPIs)', COUNT(DISTINCT npi) FROM hcp_facts;" 2>/dev/null || echo "  (memory.db not created yet — run a query through the app first)"

run:
	@$(PY) -m uvicorn app.main:app --host 0.0.0.0 --port $(PORT) --reload

open:
	@command -v open >/dev/null && open $(URL) || echo "Open $(URL) in your browser."

clean:
	@docker compose down -v
	@rm -rf data/memory.db data/chroma
	@echo "✓ Stopped, removed volumes + local memory store."

reset: clean setup
	@echo "✓ Reset done."
