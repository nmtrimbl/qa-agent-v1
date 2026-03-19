# AI QA Testing Platform (Beginner MVP)

This MVP turns:
1) a website URL + manual QA notes  
into
2) structured test steps (LLM JSON)  
into
3) deterministic browser execution (Playwright)  
into
4) failure analysis + a final QA report (LLM)

It is intentionally simple and synchronous for reliability.

## Features (Version 1 MVP)

1. Provide a website `URL`
2. Paste manual `test notes`
3. LLM planner generates structured test steps (JSON)
4. Browser executor runs steps deterministically in Playwright
5. Captures:
   - screenshots
   - console errors / page errors
   - page URL at failure
6. LLM bug analyzer summarizes likely failure reasons
7. LLM report generator produces the final QA report
8. Streamlit UI shows the report
9. FastAPI exposes `POST /run-test`

## Folder Layout

- `agents/`: planner, bug analyzer, report generator
- `browser/`: Playwright session + deterministic executor
- `models/`: Pydantic models for steps + reports
- `workflows/`: synchronous pipeline glue code
- `api/`: FastAPI server
- `ui/`: Streamlit UI
- `artifacts/` and `logs/`: saved test outputs

## Prerequisites

- Python 3.10+ (3.11 recommended)
- A working OpenAI API key

## Setup (Exact Commands)

From the repo root (the folder that contains `README.md`):

1. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

3. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Install Playwright browsers (Chromium):
   ```bash
   python -m playwright install chromium
   ```

5. Configure environment variables:
   ```bash
   cp .env.example .env
   ```
   Then edit `.env` and set:
   - `OPENAI_API_KEY="..."` (required)

   The file `.env.example` also includes optional values:
   - `OPENAI_MODEL`
   - `FASTAPI_URL`
   - `PLAYWRIGHT_HEADLESS`

## Run FastAPI

From the repo root:

```bash
uvicorn api.server:app --reload --port 8000
```

Health check:
```bash
curl http://localhost:8000/health
```

## Run Streamlit UI

From the repo root (in a separate terminal):

```bash
streamlit run ui/streamlit_app.py --server.port 8501
```

Open the shown URL in your browser (usually `http://localhost:8501`).

## How selectors work (important for beginners)

The LLM planner outputs `selector` values.

For `click` and `assert_text`, the executor supports:

1. CSS selectors (e.g. `button[type='submit']`, `input[name='q']`)
2. Text selector format: `text=Visible text` (exact match)

For `fill`, the executor supports CSS selectors only
(`fill` does not support `text=...` selectors in this MVP).

For `assert_text`, the executor is more forgiving than a simple viewport-only check:
- it first tries the requested locator
- then it searches text from the full page body / common footer containers
- if needed, it scrolls and retries so footer text can still be found
- text matching normalizes whitespace, smart quotes, and spacing around symbols like `©` / `®`

## Test Step JSON format (what the planner returns)

The planner returns a JSON object with this shape:

```json
{
  "steps": [
    { "action": "goto", "url": "https://example.com" },
    { "action": "click", "selector": "text=Login" },
    { "action": "fill", "selector": "input[name='email']", "text": "a@b.com" },
    { "action": "press", "key": "Enter" },
    { "action": "assert_text", "selector": "h1", "expected_text": "Welcome" },
    { "action": "screenshot", "screenshot_name": "final", "full_page": true }
  ]
}
```

Supported `action` values:
`goto`, `click`, `fill`, `press`, `assert_text`, `screenshot`.

Field meanings (only the required fields need to be included per action):
- `goto`: `url`
- `click`: `selector` (CSS selector, or `text=Visible text` for exact text match)
- `fill`: `selector` (CSS selector only), `text`
- `press`: `key` (e.g. `"Enter"`)
- `assert_text`: `selector` (CSS or `text=...`), `expected_text` (checks contains by default)
- `screenshot`: optional `screenshot_name`, optional `full_page` (the executor stores report screenshots as full-page images)

## Outputs (Where artifacts are saved)

For each run, the pipeline creates:

- `artifacts/<run_id>/planned_steps.json`
- `artifacts/<run_id>/execution_result.json`
- `artifacts/<run_id>/report.json`
- `artifacts/<run_id>/screenshots/<run_id>/...png`

Screenshots are shown in the Streamlit UI when available.

## Running tests (optional)

```bash
pytest -q
```

