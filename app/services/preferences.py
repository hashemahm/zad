from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Preferences


def get_preferences(db: Session) -> Preferences:
    prefs = db.get(Preferences, 1)
    if prefs is None:
        settings = get_settings()
        prefs = Preferences(
            id=1,
            max_video_minutes=settings.default_max_video_minutes,
            max_reading_minutes=settings.default_max_reading_minutes,
            include_videos=True,
            include_reading=True,
            include_unknown_length=False,
        )
        db.add(prefs)
        db.commit()
    return prefs
