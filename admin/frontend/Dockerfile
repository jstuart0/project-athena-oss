FROM nginx:alpine

# Copy static files
COPY index.html /usr/share/nginx/html/

# Phase 0: Core State & API Infrastructure (MUST be first)
COPY state.js /usr/share/nginx/html/
COPY api-client.js /usr/share/nginx/html/
COPY auth.js /usr/share/nginx/html/
COPY refresh-manager.js /usr/share/nginx/html/

# Phase 1-7: UI Components
COPY router.js /usr/share/nginx/html/
COPY status-bar.js /usr/share/nginx/html/
COPY icons.js /usr/share/nginx/html/
COPY skeleton.js /usr/share/nginx/html/
COPY command-palette.js /usr/share/nginx/html/

# Mission Control Overhaul - Design System
COPY athena.js /usr/share/nginx/html/
COPY charts.js /usr/share/nginx/html/
COPY toast.js /usr/share/nginx/html/
COPY drawer.js /usr/share/nginx/html/
COPY activity-sidebar.js /usr/share/nginx/html/

# Mission Control Overhaul - Page Modules
COPY mission-control.js /usr/share/nginx/html/
COPY voice-pipelines.js /usr/share/nginx/html/
COPY memory-context.js /usr/share/nginx/html/
COPY integrations.js /usr/share/nginx/html/
COPY keyboard.js /usr/share/nginx/html/

# Legacy application scripts
COPY utils.js /usr/share/nginx/html/
COPY app.js /usr/share/nginx/html/
COPY crud-extensions.js /usr/share/nginx/html/
COPY conversation.js /usr/share/nginx/html/
COPY routing.js /usr/share/nginx/html/
COPY metrics.js /usr/share/nginx/html/
COPY analytics.js /usr/share/nginx/html/
COPY features.js /usr/share/nginx/html/
COPY tool-calling.js /usr/share/nginx/html/
COPY base-knowledge.js /usr/share/nginx/html/
COPY guest-mode.js /usr/share/nginx/html/
COPY sms.js /usr/share/nginx/html/
COPY llm-components.js /usr/share/nginx/html/
COPY model-config.js /usr/share/nginx/html/
COPY model-downloads.js /usr/share/nginx/html/
COPY service-control.js /usr/share/nginx/html/
COPY gateway-settings.js /usr/share/nginx/html/
COPY room-groups.js /usr/share/nginx/html/
COPY guest-context.js /usr/share/nginx/html/
COPY calendar-sources.js /usr/share/nginx/html/
COPY memory-management.js /usr/share/nginx/html/
COPY directions-settings.js /usr/share/nginx/html/
COPY emerging-intents.js /usr/share/nginx/html/
COPY music-config.js /usr/share/nginx/html/
COPY room-audio.js /usr/share/nginx/html/
COPY user-api-keys.js /usr/share/nginx/html/
COPY voice-config.js /usr/share/nginx/html/
COPY room-tv.js /usr/share/nginx/html/
COPY follow-me.js /usr/share/nginx/html/
COPY admin-jarvis.js /usr/share/nginx/html/
COPY site-scraper.js /usr/share/nginx/html/
COPY system-config.js /usr/share/nginx/html/
COPY presets.js /usr/share/nginx/html/
COPY voice-automations.js /usr/share/nginx/html/
COPY alerts.js /usr/share/nginx/html/
COPY cloud-providers.js /usr/share/nginx/html/
COPY service-bypass.js /usr/share/nginx/html/
COPY escalation.js /usr/share/nginx/html/
COPY debug-logs.js /usr/share/nginx/html/

# Fix permissions
RUN chmod 644 /usr/share/nginx/html/*.js /usr/share/nginx/html/*.html

# Copy nginx config
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
