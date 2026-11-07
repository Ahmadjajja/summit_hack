# TripCheck — Gemini verifier + human approval gate

Setup:
```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill MONGODB_URI and GEMINI_API_KEY
```

Run order:
```bash
python seed.py
python verifier.py
python gate.py
```
