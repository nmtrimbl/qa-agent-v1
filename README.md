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
6. Feedback-assisted memory helps the planner and executor adapt over time
7. LLM bug analyzer summarizes likely failure reasons
8. LLM report generator produces the final QA report
9. Streamlit UI shows the report and accepts tester feedback
10. FastAPI exposes `POST /run-test`

## Final Project Tree

```text
ai-qa-platform/
├── __init__.py
├── api/
│   ├── __init__.py
│   └── server.py
├── agents/
│   ├── __init__.py
│   ├── bug_analyzer.py
│   ├── report_generator.py
│   └── test_planner.py
├── artifacts/
├── browser/
│   ├── __init__.py
│   ├── browser_session.py
│   └── executor.py
├── config/
│   ├── __init__.py
│   └── settings.py
├── logs/
├── models/
│   ├── __init__.py
│   ├── site_memory.py
│   ├── test_report.py
│   └── test_step.py
├── tests/
│   ├── __init__.py
│   ├── test_executor_helpers.py
│   ├── test_models.py
│   └── test_site_memory.py
├── ui/
│   ├── __init__.py
│   └── streamlit_app.py
├── utils/
│   ├── __init__.py
│   ├── file_helpers.py
│   ├── json_helpers.py
│   ├── report_helpers.py
│   ├── runtime.py
│   └── site_memory.py
├── workflows/
│   ├── __init__.py
│   └── qa_pipeline.py
├── .env.example
├── README.md
├── pytest.ini
└── requirements.txt
```

## Folder Layout

- `agents/`: LLM planner, bug analyzer, and report generator
- `browser/`: Playwright session and deterministic executor
- `models/`: Pydantic models for steps and reports
- `workflows/`: synchronous pipeline glue code
- `api/`: FastAPI server
- `ui/`: Streamlit UI
- `utils/`: shared beginner-friendly helpers for files, JSON parsing, reporting, runtime setup, and site memory
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

## Semantic Planning

The planner no longer treats every instruction as a literal text match.

Instead, it tries to understand user intent and can produce semantic hints such as:
- `intent`: the high-level action, such as `login`
- `candidate_labels`: alternate visible labels like `Login`, `Sign In`, `Account`
- `candidate_selectors`: alternate selectors if the UI varies
- `menu_hints`: labels that may need to be opened first
- `memory_hint_ids`: soft hints learned from previous feedback or successful runs

Example:
- tester note: `click login`
- planner may generate a step that tries `Login`, `Sign In`, and `Account`, and may also suggest opening an account menu first

## Fallback Execution

The executor is still deterministic, but it is more flexible than a single exact-text lookup.

For click steps, it now tries a fixed order of fallbacks:
1. the main `selector`
2. any `candidate_selectors`
3. role-based button/link matches
4. aria-label, title, and alt text
5. partial text matches from `candidate_labels`
6. `menu_hints`, then retrying the target

This keeps behavior explainable and bounded:
- no free-form LLM control in the browser
- no autonomous loops
- no unbounded retries

## Site Memory And Feedback-Assisted Learning

The system stores two kinds of memory under `artifacts/site_memory/`:

- domain memory: specific to one site, such as `artifacts/site_memory/staging4_doheny_com.json`
- global memory: shared soft hints in `artifacts/site_memory/_global.json`

These memory files store hints, not hard rules. Each hint tracks:
- `intent`
- `kind`
- candidate labels/selectors
- source of the hint
- a soft `strength`
- success and failure counts

Important behavior:
- tester feedback is saved as a hint
- the planner sees the hint on future runs
- the executor may try it as a fallback
- successful runs strengthen the hint
- failed runs weaken it
- old hints can become less important over time

This means the system is feedback-assisted, not strongly trained. If a site changes later, old hints are still only suggestions.

## QA Tester Feedback Workflow

After a run completes in Streamlit:
- review the report
- enter what went wrong
- enter the correct behavior
- optionally provide a high-level intent such as `login`
- click `Submit Feedback`

Example:
- issue: `The system only looked for Login text.`
- correction: `Open the account icon, then click Sign In.`

That feedback is saved for the domain and also as a weaker global hint so future runs can reuse the idea on similar sites.

## How selectors work (important for beginners)

The LLM planner outputs `selector` values.

For `click` and `assert_text`, the executor supports:

1. CSS selectors (e.g. `button[type='submit']`, `input[name='q']`)
2. Text selector format: `text=Visible text`
3. Semantic fallback labels via `candidate_labels`
4. Alternate selectors via `candidate_selectors`
5. Optional menu-opening hints via `menu_hints`

For `fill`, the executor supports CSS selectors only
(`fill` does not support `text=...` selectors in this MVP).

For `assert_text`, the executor is more forgiving than a simple viewport-only check:
- it first tries the requested locator
- then it searches text from Playwright text lookups, the full page body, and common footer containers
- if needed, it scrolls and retries so footer text can still be found
- text matching normalizes whitespace, smart quotes, and spacing around symbols like `©` / `®`

## Test Step JSON format (what the planner returns)

The planner returns a JSON object with this shape:

```json
{
  "steps": [
    { "action": "goto", "url": "https://example.com" },
    {
      "action": "click",
      "intent": "login",
      "candidate_labels": ["Login", "Sign In", "Account"],
      "menu_hints": ["Account"]
    },
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
- `click`: `selector`, or semantic fields like `candidate_labels` / `candidate_selectors`
- `fill`: `selector` (CSS selector only), `text`
- `press`: `key` (e.g. `"Enter"`)
- `assert_text`: `selector` (CSS or `text=...`), `expected_text` (checks contains by default)
- `screenshot`: optional `screenshot_name`, optional `full_page` (the executor stores report screenshots as full-page images)
- optional semantic fields:
  - `intent`
  - `candidate_labels`
  - `candidate_selectors`
  - `menu_hints`
  - `fallback_actions`
  - `memory_hint_ids`
  - `optional`

## Outputs (Where artifacts are saved)

For each run, the pipeline creates:

- `artifacts/<run_id>/planned_steps.json`
- `artifacts/<run_id>/execution_result.json`
- `artifacts/<run_id>/report.json`
- `artifacts/<run_id>/screenshots/<run_id>/...png`
- `artifacts/site_memory/<domain>.json`
- `artifacts/site_memory/_global.json`

Screenshots are shown in the Streamlit UI when available.

## Running tests (optional)

```bash
pytest -q
```

