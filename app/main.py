from fastapi import FastAPI
from app.api.decide import router as decide_router

app = FastAPI(title="BotService - Deterministic Persona", version="0.1")

# include routers
app.include_router(decide_router, prefix="")
