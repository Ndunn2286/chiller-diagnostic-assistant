# Chiller Diagnostic App Starter v4

Adds:
- more refrigeration fingerprints
- guided troubleshooting mode
- OEM alarm library endpoint
- unit-aware tech-note parsing
- feedback capture / self-learning seed

## Backend
```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --reload
```

## Frontend
```bash
cd frontend
npm install
npm run dev
```