# HAH backend

## Setup

```bash
cd backend
conda env create --file environment.yml
conda activate hah-backend
cp .env.example .env
```

## Run

```bash
uvicorn hah.main:app --app-dir src --reload
```

## Test

```bash
pytest
```

`GET /health` checks the API process. `GET /ready` checks the PostgreSQL connection.
