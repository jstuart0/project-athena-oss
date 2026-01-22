"""Recipes RAG Service - Spoonacular API Integration

Provides recipe search, meal planning, and nutritional information.

API Endpoints:
- GET /health - Health check
- GET /recipes/search - Search recipes
- GET /recipes/{recipe_id} - Get recipe details
- GET /recipes/random - Get random recipes
- GET /recipes/ingredients - Search by ingredients
"""

import os
import sys

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Query, Path
from fastapi.responses import JSONResponse

from shared.cache import cached
from shared.service_registry import register_service, unregister_service
from shared.logging_config import setup_logging
from shared.admin_config import get_admin_client
from shared.metrics import setup_metrics_endpoint

# Configure logging
setup_logging(service_name="recipes-rag")
logger = structlog.get_logger()

SERVICE_NAME = "recipes"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8020"))
# Spoonacular API Configuration
SPOONACULAR_API_KEY = os.getenv("SPOONACULAR_API_KEY", "")
SPOONACULAR_BASE_URL = "https://api.spoonacular.com"

# Global clients
http_client: Optional[httpx.AsyncClient] = None
admin_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, admin_client, SPOONACULAR_API_KEY
    logger.info("recipes_service.startup", msg="Initializing Recipes RAG service")

    # Initialize admin client
    admin_client = get_admin_client()

    # Try to fetch API key from Admin API (overrides env var)
    try:
        api_config = await admin_client.get_external_api_key("spoonacular")
        if api_config and api_config.get("api_key"):
            SPOONACULAR_API_KEY = api_config["api_key"]
            logger.info("api_key_from_admin", service="spoonacular")
        else:
            logger.info("api_key_from_env", service="spoonacular")
    except Exception as e:
        logger.warning("admin_api_unavailable", error=str(e), service="spoonacular")
        logger.info("api_key_from_env_fallback", service="spoonacular")

    if not SPOONACULAR_API_KEY:
        logger.warning("recipes_service.config.missing_key", msg="SPOONACULAR_API_KEY not set")

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0),
        params={"apiKey": SPOONACULAR_API_KEY} if SPOONACULAR_API_KEY else {}
    )

    logger.info("recipes_service.startup.complete", msg="Recipes RAG service ready")
    yield

    logger.info("recipes_service.shutdown", msg="Shutting down Recipes RAG service")
    if http_client:
        await http_client.aclose()
    if admin_client:
        await admin_client.close()

app = FastAPI(
    title="Recipes RAG Service",
    description="Recipe search and meal planning via Spoonacular API",
    version="1.0.0",
    lifespan=lifespan
)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)

@cached(ttl=3600)
async def search_recipes(
    query: str,
    cuisine: Optional[str] = None,
    diet: Optional[str] = None,
    intolerances: Optional[str] = None,
    max_results: int = 10
) -> Dict[str, Any]:
    if not SPOONACULAR_API_KEY:
        raise ValueError("Spoonacular API key not configured")

    params = {"query": query, "number": min(max_results, 100)}
    if cuisine:
        params["cuisine"] = cuisine
    if diet:
        params["diet"] = diet
    if intolerances:
        params["intolerances"] = intolerances

    logger.info("recipes_service.search", query=query, cuisine=cuisine, diet=diet)

    response = await http_client.get(f"{SPOONACULAR_BASE_URL}/recipes/complexSearch", params=params)
    response.raise_for_status()
    data = response.json()

    recipes = []
    for recipe in data.get("results", []):
        recipes.append({
            "id": recipe.get("id"),
            "title": recipe.get("title"),
            "image": recipe.get("image"),
            "ready_in_minutes": recipe.get("readyInMinutes"),
            "servings": recipe.get("servings")
        })

    return {"recipes": recipes, "total_results": data.get("totalResults", 0)}

@cached(ttl=86400)
async def get_recipe_details(recipe_id: int) -> Dict[str, Any]:
    if not SPOONACULAR_API_KEY:
        raise ValueError("Spoonacular API key not configured")

    logger.info("recipes_service.get_recipe", recipe_id=recipe_id)

    response = await http_client.get(
        f"{SPOONACULAR_BASE_URL}/recipes/{recipe_id}/information",
        params={"includeNutrition": "true"}
    )
    response.raise_for_status()
    recipe = response.json()

    return {
        "id": recipe.get("id"),
        "title": recipe.get("title"),
        "image": recipe.get("image"),
        "ready_in_minutes": recipe.get("readyInMinutes"),
        "servings": recipe.get("servings"),
        "source_url": recipe.get("sourceUrl"),
        "summary": recipe.get("summary"),
        "instructions": recipe.get("instructions"),
        "ingredients": [
            {
                "name": ing.get("name"),
                "amount": ing.get("amount"),
                "unit": ing.get("unit")
            }
            for ing in recipe.get("extendedIngredients", [])
        ],
        "nutrition": recipe.get("nutrition", {})
    }

@cached(ttl=300)
async def get_random_recipes(number: int = 1, tags: Optional[str] = None) -> Dict[str, Any]:
    if not SPOONACULAR_API_KEY:
        raise ValueError("Spoonacular API key not configured")

    params = {"number": min(number, 10)}
    if tags:
        params["tags"] = tags

    logger.info("recipes_service.random", number=number, tags=tags)

    response = await http_client.get(f"{SPOONACULAR_BASE_URL}/recipes/random", params=params)
    response.raise_for_status()
    data = response.json()

    recipes = []
    for recipe in data.get("recipes", []):
        recipes.append({
            "id": recipe.get("id"),
            "title": recipe.get("title"),
            "image": recipe.get("image"),
            "ready_in_minutes": recipe.get("readyInMinutes"),
            "servings": recipe.get("servings"),
            "summary": recipe.get("summary")
        })

    return {"recipes": recipes}

@cached(ttl=3600)
async def search_by_ingredients(
    ingredients: str,
    max_results: int = 10
) -> Dict[str, Any]:
    if not SPOONACULAR_API_KEY:
        raise ValueError("Spoonacular API key not configured")

    logger.info("recipes_service.search_by_ingredients", ingredients=ingredients)

    response = await http_client.get(
        f"{SPOONACULAR_BASE_URL}/recipes/findByIngredients",
        params={"ingredients": ingredients, "number": min(max_results, 100)}
    )
    response.raise_for_status()
    data = response.json()

    recipes = []
    for recipe in data:
        recipes.append({
            "id": recipe.get("id"),
            "title": recipe.get("title"),
            "image": recipe.get("image"),
            "used_ingredient_count": recipe.get("usedIngredientCount"),
            "missed_ingredient_count": recipe.get("missedIngredientCount"),
            "used_ingredients": [ing.get("name") for ing in recipe.get("usedIngredients", [])],
            "missed_ingredients": [ing.get("name") for ing in recipe.get("missedIngredients", [])]
        })

    return {"recipes": recipes}

@app.get("/health")
async def health_check():
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "service": "recipes-rag",
            "api_key_configured": SPOONACULAR_API_KEY is not None
        }
    )

@app.get("/recipes/search")
async def search(
    query: str = Query(..., description="Search query"),
    cuisine: Optional[str] = Query(None, description="Cuisine type"),
    diet: Optional[str] = Query(None, description="Diet type (vegetarian, vegan, etc.)"),
    intolerances: Optional[str] = Query(None, description="Intolerances (gluten, dairy, etc.)"),
    max_results: int = Query(10, ge=1, le=100)
):
    try:
        result = await search_recipes(query, cuisine, diet, intolerances, max_results)
        logger.info("recipes_service.search.success", recipes_count=len(result["recipes"]))
        return result
    except ValueError as e:
        logger.warning("recipes_service.search.invalid_request", error=str(e))
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        logger.error("recipes_service.search.api_error", error=str(e))
        raise HTTPException(status_code=502, detail=f"Spoonacular API error: {e}")
    except Exception as e:
        logger.error("recipes_service.search.error", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/recipes/{recipe_id}")
async def get_recipe(recipe_id: int = Path(..., description="Recipe ID")):
    try:
        result = await get_recipe_details(recipe_id)
        logger.info("recipes_service.get_recipe.success", recipe_id=recipe_id)
        return result
    except ValueError as e:
        logger.warning("recipes_service.get_recipe.invalid_request", error=str(e))
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        logger.error("recipes_service.get_recipe.api_error", error=str(e))
        raise HTTPException(status_code=502, detail=f"Spoonacular API error: {e}")
    except Exception as e:
        logger.error("recipes_service.get_recipe.error", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/recipes/random")
async def random(
    number: int = Query(1, ge=1, le=10, description="Number of recipes"),
    tags: Optional[str] = Query(None, description="Tags (vegetarian, dessert, etc.)")
):
    try:
        result = await get_random_recipes(number, tags)
        logger.info("recipes_service.random.success", recipes_count=len(result["recipes"]))
        return result
    except ValueError as e:
        logger.warning("recipes_service.random.invalid_request", error=str(e))
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        logger.error("recipes_service.random.api_error", error=str(e))
        raise HTTPException(status_code=502, detail=f"Spoonacular API error: {e}")
    except Exception as e:
        logger.error("recipes_service.random.error", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/recipes/ingredients")
async def by_ingredients(
    ingredients: str = Query(..., description="Comma-separated ingredients"),
    max_results: int = Query(10, ge=1, le=100)
):
    try:
        result = await search_by_ingredients(ingredients, max_results)
        logger.info("recipes_service.ingredients.success", recipes_count=len(result["recipes"]))
        return result
    except ValueError as e:
        logger.warning("recipes_service.ingredients.invalid_request", error=str(e))
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        logger.error("recipes_service.ingredients.api_error", error=str(e))
        raise HTTPException(status_code=502, detail=f"Spoonacular API error: {e}")
    except Exception as e:
        logger.error("recipes_service.ingredients.error", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8020"))
    uvicorn.run("main:app", host="0.0.0.0", port=SERVICE_PORT, reload=True, log_config=None)
