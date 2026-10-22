# Every model is imported here so Base.metadata.create_all knows about all tables.
from app.models.alert import PriceAlert
from app.models.candle import Candle
from app.models.note import Note
from app.models.watchlist import WatchlistItem

__all__ = ["Candle", "Note", "PriceAlert", "WatchlistItem"]
