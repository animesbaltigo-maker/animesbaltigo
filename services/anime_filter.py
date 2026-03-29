KEYWORDS = [
    "anime", "manga", "mangá", "episodio", "episódio", "temporada",
    "personagem", "personagens", "filme", "ova", "opening", "ending",
    "dublado", "legendado", "shounen", "shonen", "seinen", "isekai",
    "romance", "anilist", "naruto", "one piece", "bleach", "dragon ball",
    "attack on titan", "jujutsu", "demon slayer", "boku no hero",
    "fullmetal", "death note", "fate", "vinland", "tokyo ghoul"
]

def is_anime_related(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(word in t for word in KEYWORDS)
