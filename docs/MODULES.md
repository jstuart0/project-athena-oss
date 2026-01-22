# Project Athena Module Guide

This guide covers the optional modules available in Project Athena and how to configure them.

## Table of Contents

1. [Module System Overview](#module-system-overview)
2. [Home Assistant Module](#home-assistant-module)
3. [Guest Mode Module](#guest-mode-module)
4. [Notifications Module](#notifications-module)
5. [Jarvis Web Module](#jarvis-web-module)
6. [Monitoring Module](#monitoring-module)
7. [RAG Services](#rag-services)

---

## Module System Overview

### How Modules Work

Modules are optional components that can be enabled or disabled via environment variables. When a module is disabled:

- Its services don't need to be running
- Related admin UI tabs are hidden
- Queries that would use the module return graceful fallback responses
- No errors are thrown for missing module dependencies

### Checking Module Status

**Via API:**
```bash
curl http://localhost:8080/api/modules | jq
```

**Response:**
```json
[
  {
    "id": "home_assistant",
    "name": "Home Assistant Integration",
    "enabled": true,
    "status": "enabled"
  },
  {
    "id": "guest_mode",
    "name": "Guest Mode",
    "enabled": false,
    "status": "disabled"
  }
]
```

**Via Admin UI:**
Navigate to Settings → Modules to see all module statuses.

---

## Home Assistant Module

**Purpose:** Smart home control, music playback, TV control, and automation.

### Enable/Disable

```bash
# .env
MODULE_HOME_ASSISTANT=true  # or false to disable
```

### Configuration

**Option 1: Via Admin UI (Recommended)**

1. Go to Admin UI → Settings → Integrations
2. Select "Home Assistant"
3. Enter your Home Assistant URL
4. Enter your Long-Lived Access Token
5. Save

**Option 2: Via Environment Variables**

```bash
# Home Assistant URL
HA_URL=http://homeassistant.local:8123

# Long-Lived Access Token (generate in HA Profile settings)
HA_TOKEN=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...
```

### Getting a Home Assistant Token

1. Open Home Assistant
2. Click your profile (bottom left)
3. Scroll to "Long-Lived Access Tokens"
4. Click "Create Token"
5. Name it "Project Athena"
6. Copy the token immediately (it won't be shown again)

### Features When Enabled

| Feature | Description |
|---------|-------------|
| Light Control | "Turn on the living room lights" |
| Climate Control | "Set the temperature to 72 degrees" |
| Music Playback | "Play jazz music in the kitchen" |
| TV Control | "Turn on the TV and play Netflix" |
| Scene Activation | "Activate movie night scene" |
| Device Status | "Is the garage door open?" |

### Features When Disabled

Queries about smart home control will return:
> "Smart home control is not available. The Home Assistant module is not enabled."

### Admin UI Tabs

When enabled, these tabs appear in the Admin UI:
- Room Audio Configuration
- Room TV Configuration
- Voice Pipelines
- Follow Me Audio
- Music Configuration

---

## Guest Mode Module

**Purpose:** Vacation rental / Airbnb guest restrictions and calendar integration.

### Enable/Disable

```bash
# .env
MODULE_GUEST_MODE=true  # or false to disable
```

### Configuration

**Service URL (for distributed deployment):**
```bash
MODE_SERVICE_URL=http://mode-service:8022
```

### Features When Enabled

| Feature | Description |
|---------|-------------|
| Owner/Guest Detection | Different capabilities based on mode |
| Calendar Integration | Automatic mode switching from Airbnb/VRBO calendars |
| Permission Control | Restrict guest access to certain features |
| Temperature Limits | Prevent guests from extreme thermostat settings |
| Override System | Temporary owner access during guest stays |

### Mode Types

| Mode | Description |
|------|-------------|
| `owner` | Full access to all features |
| `guest` | Restricted access based on configuration |
| `away` | Property vacant, minimal features |

### Guest Restrictions Example

```json
{
  "mode": "guest",
  "permissions": {
    "can_control_lights": true,
    "can_control_climate": true,
    "can_control_music": true,
    "can_control_tv": true,
    "can_access_cameras": false,
    "can_unlock_doors": false,
    "can_arm_security": false
  },
  "temperature_limits": {
    "min_temp": 65,
    "max_temp": 75
  }
}
```

### Admin UI Tabs

When enabled:
- Guest Mode Configuration
- Calendar Sources
- Guest Sessions

---

## Notifications Module

**Purpose:** Proactive context-aware voice notifications.

### Enable/Disable

```bash
# .env
MODULE_NOTIFICATIONS=true  # or false to disable
```

### Configuration

**Service URL:**
```bash
NOTIFICATIONS_SERVICE_URL=http://notifications:8050
```

### Features When Enabled

| Feature | Description |
|---------|-------------|
| Scheduled Notifications | "Remind me at 3pm about the meeting" |
| Event Triggers | Notifications based on HA state changes |
| Context-Aware Delivery | Delivers to the room where you are |
| Do Not Disturb | Respects quiet hours |
| Notification History | Track all delivered notifications |

### Notification Types

| Type | Example |
|------|---------|
| Reminder | "Don't forget your appointment in 30 minutes" |
| Weather Alert | "Rain expected in 2 hours, close the windows" |
| Security | "Motion detected at the front door" |
| Calendar | "Your next meeting starts in 10 minutes" |
| Custom | User-defined notification rules |

### Admin UI Tabs

When enabled:
- Notification Rules
- Notification History
- Delivery Preferences

---

## Jarvis Web Module

**Purpose:** Browser-based voice interface with real-time pipeline monitoring.

### Enable/Disable

```bash
# .env
MODULE_JARVIS_WEB=true  # or false to disable
```

### Configuration

```bash
# Jarvis Web service URL
JARVIS_WEB_URL=http://jarvis-web:3001
```

### Features When Enabled

| Feature | Description |
|---------|-------------|
| Browser Voice Control | Speak to Athena from any browser |
| Real-Time Transcription | See your speech converted to text live |
| Pipeline Visualization | Watch query processing stages |
| Music Player | Browser-based music playback |
| Mobile Support | Works on phones and tablets |

### Deployment

**Docker:**
```bash
docker compose --profile jarvis-web up -d
```

**Kubernetes:**
```bash
kubectl apply -f apps/jarvis-web/k8s/
```

### Admin UI Tabs

When enabled:
- Jarvis Web Configuration

---

## Monitoring Module

**Purpose:** Grafana dashboards and Prometheus metrics.

### Enable/Disable

```bash
# .env
MODULE_MONITORING=false  # Disabled by default - requires infrastructure setup
```

### Prerequisites

This module requires:
- Prometheus server
- Grafana server
- Proper network access between services

### Configuration

```bash
# Prometheus URL
PROMETHEUS_URL=http://prometheus:9090

# Grafana URL
GRAFANA_URL=http://grafana:3000
```

### Features When Enabled

| Feature | Description |
|---------|-------------|
| Query Latency Metrics | Track response times |
| LLM Performance | Tokens/second, model usage |
| Service Health | Uptime and error rates |
| Custom Dashboards | Pre-built Grafana dashboards |

### Admin UI Tabs

When enabled:
- Monitoring Dashboard (embedded Grafana)

---

## RAG Services

RAG (Retrieval-Augmented Generation) services provide domain-specific data for queries.

### Available RAG Services

| Service | Port | API Key Required | Free Tier |
|---------|------|-----------------|-----------|
| Weather | 8010 | `OPENWEATHER_API_KEY` | 1,000/day |
| Airports | 8011 | None | N/A |
| Sports | 8017 | `THESPORTSDB_API_KEY` | Yes |
| Flights | 8013 | `FLIGHTAWARE_API_KEY` | Paid only |
| Events | 8014 | `TICKETMASTER_API_KEY` | 5,000/day |
| Streaming | 8015 | `TMDB_API_KEY` | 1M/month |
| News | 8016 | `NEWSAPI_KEY` | 100/day |
| Stocks | 8012 | `ALPHA_VANTAGE_API_KEY` | 500/day |
| WebSearch | 8018 | `BRAVE_API_KEY` | 2,000/month |
| Dining | 8019 | `YELP_API_KEY` | 5,000/day |
| Recipes | 8020 | `SPOONACULAR_API_KEY` | 150/day |
| Directions | 8030 | None | N/A |

### Enable RAG Services

RAG services are enabled by adding their API keys:

```bash
# Add key to enable service
OPENWEATHER_API_KEY=your-key-here

# Service will be available once key is added
```

### Priority Order for Setup

1. **Weather** - Most commonly used, essential for daily queries
2. **WebSearch** - Fallback for unknown queries, highly recommended
3. **News** - Good for daily briefings
4. **Streaming** - Movie/TV recommendations
5. Others based on your needs

### Custom RAG Service URLs

For distributed deployment:

```bash
RAG_WEATHER_URL=http://rag-server:8010
RAG_SPORTS_URL=http://rag-server:8017
RAG_NEWS_URL=http://rag-server:8016
# ... etc
```

### RAG Service Health Check

```bash
# Check individual service
curl http://localhost:8010/health

# Check all RAG services via admin
curl http://localhost:8080/api/integrations | jq '.[] | select(.type == "rag_service")'
```

---

## Creating Custom Modules

### Module Registration

Add your module to `src/shared/module_registry.py`:

```python
"my_custom_module": Module(
    id="my_custom_module",
    name="My Custom Module",
    description="Description of what it does",
    env_var="MODULE_MY_CUSTOM",
    components=[
        ModuleComponent("my_service", "service", service_port=8099, health_endpoint="/health"),
        ModuleComponent("my_admin_tab", "admin_tab", admin_tab_id="my-module"),
    ],
    default_enabled=False
),
```

### Checking Module Status in Code

```python
from shared.module_registry import module_registry

# Check if enabled
if not module_registry.is_enabled("my_custom_module"):
    return "This feature is not available."

# Get status with health check
status = await module_registry.get_status("my_custom_module")
if status == ModuleStatus.UNAVAILABLE:
    return "Service is temporarily unavailable."
```

---

## Best Practices

### Start Minimal

Begin with core services only, then add modules as needed:

```bash
# Start minimal
MODULE_HOME_ASSISTANT=false
MODULE_GUEST_MODE=false
MODULE_NOTIFICATIONS=false
MODULE_JARVIS_WEB=false
MODULE_MONITORING=false
```

### Resource Considerations

| Module | CPU Impact | Memory Impact |
|--------|------------|---------------|
| Home Assistant | Low | Low |
| Guest Mode | Low | Low |
| Notifications | Low | Low |
| Jarvis Web | Medium | Medium |
| Monitoring | High | High |
| Each RAG Service | Low | Low |

### Production Recommendations

1. **Always enable:** Weather, WebSearch RAG services
2. **Enable if using HA:** Home Assistant module
3. **Enable for rentals:** Guest Mode module
4. **Enable for advanced users:** Monitoring module (separate infrastructure)

---

## Troubleshooting Modules

### Module Shows as "Unavailable"

1. Check if service is running:
   ```bash
   curl http://localhost:PORT/health
   ```

2. Check service logs:
   ```bash
   docker compose logs module-service
   ```

3. Verify environment variables are set

### Admin Tab Not Showing

1. Verify module is enabled in `.env`
2. Restart admin backend after changing module settings
3. Clear browser cache

### Module Queries Failing

1. Check module health via API
2. Verify all required API keys are set
3. Check for rate limiting on external APIs
