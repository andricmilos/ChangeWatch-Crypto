# Every model is imported here so Base.metadata.create_all knows about all tables.
from app.models.candle import Candle

__all__ = ["Candle"]
