"""
Face verification: compares live-capture embedding (from face-api.js on frontend)
against enrolled embedding stored in DB. No heavy ML on the backend — just
vector math, so it stays free-tier friendly.
"""
import numpy as np

FACE_MATCH_THRESHOLD = 0.6  # cosine similarity; tune with real data (0.55-0.7 typical for face-api.js)


def embedding_to_str(embedding: list) -> str:
    return ",".join(str(x) for x in embedding)


def str_to_embedding(s: str) -> np.ndarray:
    return np.array([float(x) for x in s.split(",")])


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    a, b = np.array(a), np.array(b)
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def verify_face(live_embedding: list, enrolled_embedding_str: str) -> dict:
    enrolled = str_to_embedding(enrolled_embedding_str)
    live = np.array(live_embedding)

    if enrolled.shape[0] != live.shape[0]:
        return {"score": 0.0, "verified": "fail", "reason": "embedding_dimension_mismatch"}

    score = cosine_similarity(live, enrolled)
    verified = "pass" if score >= FACE_MATCH_THRESHOLD else "fail"
    return {"score": score, "verified": verified, "reason": None}
