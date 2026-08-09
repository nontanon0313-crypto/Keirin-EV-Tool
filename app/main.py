from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .database import init_db
from .routers import analyze, races, ev, purchases, simulation, bank, bankroll

app = FastAPI(title="競輪 期待値検証ツール API")

# GitHub PagesのPWAから叩けるようCORSを許可(本番では自分のGitHub Pagesドメインに絞ることを推奨)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(bank.router)
app.include_router(bankroll.router)
app.include_router(analyze.router)
app.include_router(races.router)
app.include_router(ev.router)
app.include_router(purchases.router)
app.include_router(simulation.router)


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/")
def root():
    return {"status": "ok", "service": "keirin-ev-tool"}


@app.get("/health")
def health():
    return {"status": "healthy"}
