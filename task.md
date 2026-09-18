# Task: Implement UI Clarity and 2-Layer AI Verification Engine

- [x] Task 1: Enhance NSGA-II UI Clarity (`nsga2_optimizer.py`, `app.py`, `index.html`) <!-- id: 0 -->
    - [x] Return readable corridor names, fault count, and time windows in `nsga2_optimizer.py` / `app.py` output <!-- id: 1 -->
    - [x] Update frontend rendering in `index.html` to display clear corridor labels, maintenance durations, and fault details instead of raw codes like "MB-AUTO-1" <!-- id: 2 -->
- [x] Task 2: Implement 2-Layer AI Verification Math Engine (`block_scheduler.py`) <!-- id: 3 -->
    - [x] Compute deterministic feasibility math: Time Deficit, Affected Trains Delay, and Train Slack Coverage before calling LLM <!-- id: 4 -->
    - [x] Inject exact calculation payload into LLM prompt so AI acts strictly as a verifier/summarizer <!-- id: 5 -->
- [x] Task 3: End-to-end verification and testing <!-- id: 6 -->
    - [x] Test API responses and UI display <!-- id: 7 -->
    - [x] Verify AI verification responses rely on deterministic pre-checks <!-- id: 8 -->
