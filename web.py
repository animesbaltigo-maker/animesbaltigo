from fastapi import FastAPI
from fastapi.responses import FileResponse

app = FastAPI()

@app.get("/player")
async def player():
    return FileResponse("web/player.html")