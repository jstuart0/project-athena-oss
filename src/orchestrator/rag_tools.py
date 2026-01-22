"""
RAG Tool Definitions for LLM Tool Calling

Defines OpenAI function calling schemas for all 11 RAG services.
Admin can enable/disable individual tools via database configuration.

Each tool maps to a RAG service endpoint and includes:
- OpenAI function schema (name, description, parameters)
- Service URL for execution
- Guest mode permission
- Timeout configuration
"""
import os
from typing import List, Dict, Any, Optional
import structlog

logger = structlog.get_logger()

# RAG service base URLs (can be overridden by database config or environment variables)
# Port assignments for each service - defaults to localhost for development
WEATHER_URL = os.getenv("WEATHER_RAG_URL", "http://localhost:8010")
AIRPORTS_URL = os.getenv("AIRPORTS_RAG_URL", "http://localhost:8011")
STOCKS_URL = os.getenv("STOCKS_RAG_URL", "http://localhost:8012")
FLIGHTS_URL = os.getenv("FLIGHTS_RAG_URL", "http://localhost:8013")
EVENTS_URL = os.getenv("EVENTS_RAG_URL", "http://localhost:8014")
STREAMING_URL = os.getenv("STREAMING_RAG_URL", "http://localhost:8015")
NEWS_URL = os.getenv("NEWS_RAG_URL", "http://localhost:8016")
SPORTS_URL = os.getenv("SPORTS_RAG_URL", "http://localhost:8017")
WEBSEARCH_URL = os.getenv("WEBSEARCH_RAG_URL", "http://localhost:8018")
DINING_URL = os.getenv("DINING_RAG_URL", "http://localhost:8019")
RECIPES_URL = os.getenv("RECIPES_RAG_URL", "http://localhost:8020")
COMMUNITY_EVENTS_URL = os.getenv("COMMUNITY_EVENTS_RAG_URL", "http://localhost:8026")
SERPAPI_EVENTS_URL = os.getenv("SERPAPI_EVENTS_RAG_URL", "http://localhost:8032")
DIRECTIONS_URL = os.getenv("DIRECTIONS_RAG_URL", "http://localhost:8030")
SEATGEEK_EVENTS_URL = os.getenv("SEATGEEK_EVENTS_RAG_URL", "http://localhost:8024")
TRANSPORTATION_URL = os.getenv("TRANSPORTATION_RAG_URL", "http://localhost:8025")
AMTRAK_URL = os.getenv("AMTRAK_RAG_URL", "http://localhost:8027")
SITE_SCRAPER_URL = os.getenv("SITE_SCRAPER_RAG_URL", "http://localhost:8031")
PRICE_COMPARE_URL = os.getenv("PRICE_COMPARE_RAG_URL", "http://localhost:8033")
TESLA_URL = os.getenv("TESLA_RAG_URL", "http://localhost:8028")
MEDIA_URL = os.getenv("MEDIA_RAG_URL", "http://localhost:8029")
BRIGHTDATA_URL = os.getenv("BRIGHTDATA_RAG_URL", "http://localhost:8040")


# Tool definitions in OpenAI function calling format
TOOL_DEFINITIONS = [
    {
        "tool_name": "get_weather",
        "display_name": "Weather Forecast",
        "description": "Get weather forecast and conditions for a location",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get current weather conditions and forecast for a location. Returns temperature, conditions, humidity, wind, and 7-day forecast.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "City name or ZIP code (e.g., 'San Francisco' or '94102')"
                        },
                        "days": {
                            "type": "integer",
                            "description": "Number of forecast days (1-7)",
                            "default": 3
                        }
                    },
                    "required": ["location"]
                }
            }
        },
        "service_url": WEATHER_URL,
        "guest_mode_allowed": True,
        "timeout_seconds": 15
    },
    {
        "tool_name": "get_sports_scores",
        "display_name": "Sports Scores & Schedules",
        "description": "Get sports scores, schedules, and team information for a SPECIFIC team",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "get_sports_scores",
                "description": "Get live scores, schedules, and stats for a SPECIFIC TEAM. Use this when asking about a particular team's games or schedule. For 'best team' or 'top teams' or 'standings' questions, use get_sports_standings instead. IMPORTANT: Pass team names/abbreviations EXACTLY as the user says them.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "team": {
                            "type": "string",
                            "description": "Team name or abbreviation EXACTLY as user said it. Examples: 'mc' (Man City), 'mu' (Man United), 'ful' (Fulham), 'ravens', 'Michigan', 'Ohio State'. Do NOT expand abbreviations - pass them as-is."
                        },
                        "league": {
                            "type": "string",
                            "description": "League or competition. Use 'college-football' for NCAA football, 'college-basketball' for NCAA basketball, 'premier-league' for English soccer, 'mls' for US soccer, 'international' for national teams/World Cup.",
                            "enum": ["nfl", "college-football", "nba", "college-basketball", "wnba", "mlb", "nhl", "mls", "premier-league", "la-liga", "bundesliga", "serie-a", "champions-league", "international"]
                        },
                        "include_schedule": {
                            "type": "boolean",
                            "description": "Include upcoming schedule",
                            "default": True
                        }
                    },
                    "required": ["team"]
                }
            }
        },
        "service_url": SPORTS_URL,
        "guest_mode_allowed": True,
        "timeout_seconds": 20
    },
    {
        "tool_name": "get_sports_standings",
        "display_name": "Sports Standings & Rankings",
        "description": "Get league standings, rankings, and best teams",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "get_sports_standings",
                "description": "Get current standings and rankings for a league. Use this for questions about 'best team', 'top teams', 'rankings', 'standings', 'who is leading', or 'who has the best record'. Returns teams sorted by wins/points.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "league": {
                            "type": "string",
                            "description": "League to get standings for",
                            "enum": ["nfl", "nba", "mlb", "nhl", "premier-league", "la-liga", "bundesliga", "serie-a", "ligue-1", "mls", "ncaaf", "ncaab"]
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Number of top teams to return (default 10)",
                            "default": 10
                        }
                    },
                    "required": ["league"]
                }
            }
        },
        "service_url": SPORTS_URL,
        "guest_mode_allowed": True,
        "timeout_seconds": 15
    },
    {
        "tool_name": "get_airport_info",
        "display_name": "Airport Information",
        "description": "Get airport codes, terminals, and facility information",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "get_airport_info",
                "description": "Get detailed information about airports including codes, terminals, parking, transportation, and amenities. Returns IATA/ICAO codes, terminal maps, and facility details.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Airport name, city, or code (e.g., 'SFO', 'San Francisco Airport')"
                        }
                    },
                    "required": ["query"]
                }
            }
        },
        "service_url": AIRPORTS_URL,
        "guest_mode_allowed": True,
        "timeout_seconds": 15
    },
    {
        "tool_name": "search_flights",
        "display_name": "Flight Search",
        "description": "Search for flight information and schedules",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "search_flights",
                "description": "Search for flights between airports, check flight status, and get arrival/departure times. Returns flight numbers, airlines, gates, and status.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "origin": {
                            "type": "string",
                            "description": "Origin airport code (e.g., 'SFO')"
                        },
                        "destination": {
                            "type": "string",
                            "description": "Destination airport code (e.g., 'JFK')"
                        },
                        "date": {
                            "type": "string",
                            "description": "Travel date in YYYY-MM-DD format (default: today)"
                        }
                    },
                    "required": ["origin", "destination"]
                }
            }
        },
        "service_url": FLIGHTS_URL,
        "guest_mode_allowed": True,
        "timeout_seconds": 20
    },
    {
        "tool_name": "search_events",
        "display_name": "Local Events",
        "description": "Find concerts, shows, festivals, and local events",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "search_events",
                "description": "Search for local events, concerts, festivals, sports games, theater shows, and community activities. Returns event details, venues, dates, and ticket information.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {
                            "type": "string",
                            "description": "Search keyword (artist, event name, venue, etc.)"
                        },
                        "city": {
                            "type": "string",
                            "description": "City name (e.g., 'San Francisco', 'Los Angeles')"
                        },
                        "state_code": {
                            "type": "string",
                            "description": "State code (e.g., 'CA', 'NY', 'TX')"
                        },
                        "classification_name": {
                            "type": "string",
                            "description": "Event classification (Music, Sports, Arts & Theatre, Film, Miscellaneous)"
                        },
                        "start_date": {
                            "type": "string",
                            "description": "Start date for search range (YYYY-MM-DD format)"
                        },
                        "end_date": {
                            "type": "string",
                            "description": "End date for search range (YYYY-MM-DD format)"
                        },
                        "size": {
                            "type": "integer",
                            "description": "Number of results to return (1-200, default: 20)"
                        }
                    },
                    "required": []
                }
            }
        },
        "service_url": EVENTS_URL,
        "guest_mode_allowed": True,
        "timeout_seconds": 20
    },
    {
        "tool_name": "search_streaming",
        "display_name": "Streaming Content",
        "description": "Find movies and shows on streaming platforms",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "search_streaming",
                "description": "Search for movies and TV shows across streaming platforms (Netflix, Hulu, Disney+, etc.). Returns availability, ratings, cast, and synopsis.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Movie or show title, genre, or actor name"
                        },
                        "content_type": {
                            "type": "string",
                            "description": "Type of content to search for",
                            "enum": ["movie", "tv", "both"],
                            "default": "both"
                        }
                    },
                    "required": ["query"]
                }
            }
        },
        "service_url": STREAMING_URL,
        "guest_mode_allowed": True,
        "timeout_seconds": 20
    },
    {
        "tool_name": "get_news",
        "display_name": "News Articles",
        "description": "Get latest news articles on topics",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "get_news",
                "description": "Get latest news articles and headlines on specific topics or general news. Returns article titles, summaries, sources, and publication dates.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "News search query or topic (e.g., 'technology', 'politics', 'sports', 'latest AI developments')"
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of articles to return (1-100)",
                            "default": 10
                        }
                    },
                    "required": ["query"]
                }
            }
        },
        "service_url": NEWS_URL,
        "guest_mode_allowed": True,
        "timeout_seconds": 20
    },
    {
        "tool_name": "get_stock_info",
        "display_name": "Stock Market Data",
        "description": "Get stock prices, company info, and market data",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "get_stock_info",
                "description": "Get real-time stock prices, company information, historical data, and market analysis. Returns current price, change, volume, and company fundamentals.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Stock ticker symbol (e.g., 'AAPL', 'TSLA', 'GOOGL')"
                        },
                        "include_history": {
                            "type": "boolean",
                            "description": "Include historical price data",
                            "default": False
                        }
                    },
                    "required": ["symbol"]
                }
            }
        },
        "service_url": STOCKS_URL,
        "guest_mode_allowed": False,  # Financial data - owner only
        "timeout_seconds": 15
    },
    {
        "tool_name": "search_web",
        "display_name": "Web Search",
        "description": "Search the web for general information",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "search_web",
                "description": "Search the web for general information when other specialized tools don't apply. Returns relevant web results with titles, snippets, and URLs.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query"
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results (1-10)",
                            "default": 5
                        }
                    },
                    "required": ["query"]
                }
            }
        },
        "service_url": WEBSEARCH_URL,
        "guest_mode_allowed": True,
        "timeout_seconds": 20
    },
    {
        "tool_name": "search_restaurants",
        "display_name": "Places Search",
        "description": "Find restaurants, stores, and any local businesses",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "search_restaurants",
                "description": "Search for any local business or place. ALWAYS use this tool when the user asks about a specific business name (e.g., 'Cowboy Rose', 'Ikaros', 'The Food Market') to look up their address, hours, phone number, or location. Also use for finding places: restaurants, cafes, supermarkets, grocery stores, pharmacies, gas stations, etc. Returns place names, ratings, addresses, hours, and locations. IMPORTANT: For product searches (PS5, iPhone, etc.), include major retailer names in the term.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "City or neighborhood (e.g., 'Baltimore, MD', 'San Francisco')"
                        },
                        "term": {
                            "type": "string",
                            "description": "Type of place or product search. For products, include retailer names (e.g., 'Target Best Buy GameStop PS5', 'Walmart Target groceries'). For places: 'Italian restaurant', 'pharmacy', 'hardware store'"
                        },
                        "price_range": {
                            "type": "string",
                            "description": "Price range indicator",
                            "enum": ["$", "$$", "$$$", "$$$$"]
                        }
                    },
                    "required": ["location"]
                }
            }
        },
        "service_url": DINING_URL,
        "guest_mode_allowed": True,
        "timeout_seconds": 20
    },
    {
        "tool_name": "search_recipes",
        "display_name": "Recipe Search",
        "description": "Find cooking recipes and instructions",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "search_recipes",
                "description": "Search for cooking recipes with ingredients and step-by-step instructions. Returns recipe names, ingredients, cooking time, difficulty, and detailed instructions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Recipe name, ingredient, or cuisine type (e.g., 'pasta carbonara', 'chicken recipes')"
                        },
                        "dietary_restrictions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Dietary restrictions (vegetarian, vegan, gluten-free, etc.)"
                        },
                        "max_cook_time": {
                            "type": "integer",
                            "description": "Maximum cooking time in minutes"
                        }
                    },
                    "required": ["query"]
                }
            }
        },
        "service_url": RECIPES_URL,
        "guest_mode_allowed": True,
        "timeout_seconds": 20
    },
    {
        "tool_name": "search_transit",
        "display_name": "Transit & Transportation",
        "description": "Find nearby transit stops, routes, and schedules for buses, trains, ferries, and light rail",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "search_transit",
                "description": "Search for public transit options including MTA buses, metro, light rail, MARC trains, Charm City Circulator (free), Harbor Connector (free water taxi), Amtrak, and paid water taxi. Find nearby stops, routes, schedules, and departure times.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "lat": {
                            "type": "number",
                            "description": "Latitude for nearby stop search (e.g., 39.2904 for Inner Harbor)"
                        },
                        "lon": {
                            "type": "number",
                            "description": "Longitude for nearby stop search (e.g., -76.6122 for Inner Harbor)"
                        },
                        "query": {
                            "type": "string",
                            "description": "Search query for stops or routes (e.g., 'Penn Station', 'Route 11')"
                        },
                        "stop_id": {
                            "type": "string",
                            "description": "Specific stop ID to get departures"
                        },
                        "transit_type": {
                            "type": "string",
                            "description": "Filter by transit type",
                            "enum": ["bus", "metro", "light_rail", "rail", "ferry", "commuter_bus"]
                        },
                        "free_only": {
                            "type": "boolean",
                            "description": "Only show free transit options (Circulator, Harbor Connector)",
                            "default": False
                        }
                    },
                    "required": []
                }
            }
        },
        "service_url": TRANSPORTATION_URL,
        "guest_mode_allowed": True,
        "timeout_seconds": 20
    },
    {
        "tool_name": "get_directions",
        "display_name": "Directions & Navigation",
        "description": "Get driving, walking, or transit directions between locations",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "get_directions",
                "description": "Get turn-by-turn directions between two locations. Supports driving, walking, bicycling, and transit modes. Can also find stops along a route (gas stations, restaurants, etc.).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "origin": {
                            "type": "string",
                            "description": "Starting location (address, place name, or coordinates)"
                        },
                        "destination": {
                            "type": "string",
                            "description": "Ending location (address, place name, or coordinates)"
                        },
                        "mode": {
                            "type": "string",
                            "description": "Travel mode",
                            "enum": ["driving", "walking", "bicycling", "transit"],
                            "default": "driving"
                        },
                        "avoid": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["tolls", "highways", "ferries"]},
                            "description": "Features to avoid on the route"
                        },
                        "waypoints": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Intermediate stops along the route"
                        },
                        "find_stops": {
                            "type": "string",
                            "description": "Category of places to find along route (e.g., 'gas_station', 'restaurant', 'coffee')"
                        },
                        "stop_position": {
                            "type": "string",
                            "description": "Where along the route to find stops",
                            "enum": ["start", "quarter", "middle", "three_quarters", "end"],
                            "default": "middle"
                        }
                    },
                    "required": ["origin", "destination"]
                }
            }
        },
        "service_url": DIRECTIONS_URL,
        "guest_mode_allowed": True,
        "timeout_seconds": 30
    },
    {
        "tool_name": "get_train_schedule",
        "display_name": "Amtrak Train Schedules",
        "description": "Get Amtrak train schedules and times between stations",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "get_train_schedule",
                "description": "Get Amtrak train schedules between stations. Default origin is Baltimore Penn Station. Returns departure times, arrival times, train numbers, routes (Acela, Northeast Regional, etc.), and booking links.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "destination": {
                            "type": "string",
                            "description": "Destination station name or code (e.g., 'New York', 'NYC', 'NYP', 'Washington', 'DC', 'Boston', 'Philadelphia')"
                        },
                        "origin": {
                            "type": "string",
                            "description": "Origin station name or code (default: Baltimore Penn Station). Examples: 'Baltimore', 'BAL', 'Washington', 'DC', 'WAS'"
                        },
                        "date": {
                            "type": "string",
                            "description": "Travel date in YYYY-MM-DD format (default: today)"
                        },
                        "return_date": {
                            "type": "string",
                            "description": "Return date in YYYY-MM-DD format for round trip (optional)"
                        }
                    },
                    "required": ["destination"]
                }
            }
        },
        "service_url": AMTRAK_URL,
        "guest_mode_allowed": True,
        "timeout_seconds": 20
    },
    {
        "tool_name": "scrape_website",
        "display_name": "Website Content Scraper",
        "description": "Fetch and extract content from a specific website URL to answer follow-up questions",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "scrape_website",
                "description": "Fetch and extract content from a specific website URL. Use this when the user wants to know something about a business or place that requires checking their website, like 'do they have happy hour?', 'what are their hours?', 'do they take reservations?'. Also useful after finding a restaurant or business to get more details.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The URL to scrape (e.g., 'https://restaurant.com/menu')"
                        },
                        "extraction_hint": {
                            "type": "string",
                            "description": "Type of content to extract",
                            "enum": ["auto", "article", "table", "jsonld"],
                            "default": "auto"
                        }
                    },
                    "required": ["url"]
                }
            }
        },
        "service_url": SITE_SCRAPER_URL,
        "guest_mode_allowed": True,  # URL restrictions handled by site scraper config
        "timeout_seconds": 15
    },
    {
        "tool_name": "scrape_webpage_bright",
        "display_name": "Web Page Scraper (Reliable)",
        "description": "Scrape any webpage to markdown - bypasses anti-bot systems",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "scrape_webpage_bright",
                "description": "Scrape a webpage and extract its content as clean markdown. Use this when you need to read the actual content of a webpage that the user mentions. This tool can bypass CAPTCHAs and anti-bot systems. Use for: reading articles, checking business info from websites, extracting recipe details from food blogs.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The full URL of the webpage to scrape (must start with http:// or https://)"
                        }
                    },
                    "required": ["url"]
                }
            }
        },
        "service_url": BRIGHTDATA_URL,
        "guest_mode_allowed": True,
        "timeout_seconds": 30
    },
    {
        "tool_name": "compare_prices",
        "display_name": "Price Comparison",
        "description": "Find and compare prices for a product across multiple retailers",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "compare_prices",
                "description": "Search for product prices across multiple retailers to find the best deal. Use this when the user asks about prices, cheapest options, best deals, or wants to compare prices for a product. Returns prices from multiple sources sorted by lowest price.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Product to search for (e.g., 'iPhone 15 Pro 256GB', 'Sony WH-1000XM5 headphones', 'PS5')"
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of price results to return",
                            "default": 10
                        }
                    },
                    "required": ["query"]
                }
            }
        },
        "service_url": PRICE_COMPARE_URL,
        "guest_mode_allowed": True,
        "timeout_seconds": 30
    },
    {
        "tool_name": "get_tesla_metrics",
        "display_name": "Tesla Vehicle Metrics",
        "description": "Get Tesla vehicle metrics, status, and statistics from TeslaMate",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "get_tesla_metrics",
                "description": "Get Tesla vehicle metrics, status, and statistics. Use this for questions about the Tesla car including: battery level, range, charging history, drive history, efficiency, tire pressure, software updates, vampire drain, and overall statistics. OWNER MODE ONLY - contains sensitive vehicle data.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural language question about the Tesla (e.g., 'what is my battery level?', 'how many miles have I driven this month?', 'what is my vampire drain?')"
                        },
                        "metric_type": {
                            "type": "string",
                            "description": "Specific metric type to retrieve",
                            "enum": ["status", "battery", "drives", "charges", "efficiency", "states", "updates", "vampire_drain", "stats"],
                            "default": "status"
                        }
                    },
                    "required": ["query"]
                }
            }
        },
        "service_url": TESLA_URL,
        "guest_mode_allowed": False,  # OWNER MODE ONLY - sensitive vehicle data
        "timeout_seconds": 15
    },
    {
        "tool_name": "request_media",
        "display_name": "Media Requests",
        "description": "Request movies and TV shows via Overseerr, check library availability, and manage media requests",
        "category": "rag",
        "function_schema": {
            "type": "function",
            "function": {
                "name": "request_media",
                "description": "Request movies and TV shows via Overseerr. Use this for: requesting/adding movies or TV shows to the library, checking if media is available on Plex/Jellyfin, checking request status, and listing pending requests. OWNER MODE ONLY - controls media library.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural language request about media (e.g., 'add the movie Inception', 'is Breaking Bad available?', 'what movies have I requested?')"
                        }
                    },
                    "required": ["query"]
                }
            }
        },
        "service_url": MEDIA_URL,
        "guest_mode_allowed": False,  # OWNER MODE ONLY - controls media library
        "timeout_seconds": 30
    }
]


def get_rag_tools(enabled_tools: Optional[List[Dict[str, Any]]] = None, guest_mode: bool = False) -> List[Dict[str, Any]]:
    """
    Get RAG tools for LLM tool calling.

    Args:
        enabled_tools: List of enabled tools from database (with enable/disable state)
        guest_mode: If True, only return guest-mode-allowed tools

    Returns:
        List of tool function schemas in OpenAI format
    """
    # If no database config provided, use all hardcoded tools
    if not enabled_tools:
        tools = TOOL_DEFINITIONS
    else:
        # Use database configuration to filter enabled tools
        enabled_names = {t["tool_name"] for t in enabled_tools if t.get("enabled", True)}
        tools = [t for t in TOOL_DEFINITIONS if t["tool_name"] in enabled_names]

    # Filter by guest mode if needed
    if guest_mode:
        tools = [t for t in tools if t.get("guest_mode_allowed", False)]

    # Extract just the function schemas for LLM
    function_schemas = [t["function_schema"] for t in tools]

    logger.info(
        "rag_tools_loaded",
        total_tools=len(function_schemas),
        guest_mode=guest_mode,
        tool_names=[t["tool_name"] for t in tools]
    )

    return function_schemas


async def get_tool_service_url_from_registry(tool_name: str) -> Optional[str]:
    """
    Get service URL from registry database.

    Maps tool names to service names in registry.
    For example: "get_sports_scores" → "sports"
    """
    # Map tool names to service names
    tool_to_service = {
        "get_weather": "weather",
        "get_sports_scores": "sports",
        "get_airport_info": "airports",
        "search_flights": "flights",
        "search_events": "events",
        "search_streaming": "streaming",
        "get_news": "news",
        "get_stock_info": "stocks",
        "search_web": "websearch",
        "search_restaurants": "dining",
        "search_recipes": "recipes",
        "search_transit": "transportation",
        "get_train_schedule": "amtrak",
        "get_directions": "directions",  # Added - was missing, causing registry lookup failure
        "scrape_website": "site-scraper",
        "scrape_webpage_bright": "brightdata",
        "compare_prices": "price-compare",
        "get_tesla_metrics": "tesla",
        "request_media": "media"
    }

    service_name = tool_to_service.get(tool_name)
    if not service_name:
        return None

    try:
        from shared.service_registry import get_service_url
        url = await get_service_url(service_name)
        if url:
            logger.info(f"Service registry: {tool_name} → {service_name} → {url}")
            return url
    except Exception as e:
        logger.warning(f"Service registry lookup failed: {e}")

    return None


def get_tool_service_url(tool_name: str, db_tools: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """
    Get the service URL for a tool.

    Args:
        tool_name: Name of the tool
        db_tools: Optional list of tools from database (may override hardcoded URLs)

    Returns:
        Service URL for the tool, or None if not found
    """
    # Try service registry first (async call, will need to be handled in calling code)
    # For now, this is synchronous fallback behavior

    # Check database config first
    if db_tools:
        for tool in db_tools:
            if tool["tool_name"] == tool_name:
                return tool.get("service_url")

    # Fall back to hardcoded definitions
    for tool in TOOL_DEFINITIONS:
        if tool["tool_name"] == tool_name:
            return tool.get("service_url")

    logger.warning("tool_service_url_not_found", tool_name=tool_name)
    return None


def get_all_tool_names() -> List[str]:
    """Get list of all available tool names."""
    return [t["tool_name"] for t in TOOL_DEFINITIONS]
