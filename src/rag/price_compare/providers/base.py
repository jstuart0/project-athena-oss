"""Base class for price comparison providers."""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from dataclasses import dataclass


@dataclass
class PriceResult:
    """Standard price result format."""
    product_name: str
    price: float
    currency: str
    retailer: str
    url: str
    in_stock: bool = True
    shipping: Optional[float] = None
    condition: str = "new"
    image_url: Optional[str] = None
    last_updated: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "product_name": self.product_name,
            "price": self.price,
            "currency": self.currency,
            "retailer": self.retailer,
            "url": self.url,
            "in_stock": self.in_stock,
            "shipping": self.shipping,
            "total_price": self.price + (self.shipping or 0),
            "condition": self.condition,
            "image_url": self.image_url,
            "last_updated": self.last_updated
        }


class PriceProvider(ABC):
    """Abstract base class for price providers."""

    name: str = "base"
    requires_api_key: bool = False

    @abstractmethod
    async def search(self, query: str, **kwargs) -> List[PriceResult]:
        """Search for product by keyword."""
        pass

    async def lookup_upc(self, upc: str) -> List[PriceResult]:
        """Look up product by UPC/barcode (optional)."""
        return await self.search(upc)

    async def close(self):
        """Cleanup resources."""
        pass
