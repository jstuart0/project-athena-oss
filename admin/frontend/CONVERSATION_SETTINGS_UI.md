# Conversation Settings UI - React Component Design

**Status:** Design Document (for future React migration)
**Current Frontend:** Vanilla JS/HTML
**Future Frontend:** React + TypeScript

## Overview

This document describes the React UI components for managing conversation context and clarification settings through the Admin Panel.

## Component Structure

```
ConversationSettings/
├── index.tsx                    # Main settings page
├── ConversationTab.tsx         # Conversation context settings
├── ClarificationTab.tsx        # Clarification settings
├── ClarificationTypes.tsx      # Clarification types management
├── SportsTeams.tsx            # Sports team disambiguation
├── DeviceRules.tsx            # Device disambiguation rules
└── AnalyticsDashboard.tsx     # Analytics viewer
```

## API Integration

```typescript
// API client for conversation endpoints
const api = {
  // Conversation Settings
  getConversationSettings: () => fetch('/api/conversation/settings'),
  updateConversationSettings: (data) => fetch('/api/conversation/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data)
  }),

  // Clarification Settings
  getClarificationSettings: () => fetch('/api/conversation/clarification'),
  updateClarificationSettings: (data) => fetch('/api/conversation/clarification', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data)
  }),

  // Clarification Types
  getClarificationTypes: () => fetch('/api/conversation/clarification/types'),
  updateClarificationType: (type, data) =>
    fetch(`/api/conversation/clarification/types/${type}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data)
    }),

  // Sports Teams
  getSportsTeams: () => fetch('/api/conversation/sports-teams'),
  createSportsTeam: (data) => fetch('/api/conversation/sports-teams', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data)
  }),

  // Analytics
  getAnalytics: (params) => fetch(`/api/conversation/analytics?${new URLSearchParams(params)}`),
  getAnalyticsSummary: () => fetch('/api/conversation/analytics/summary')
};
```

## Example Component: ConversationTab

```tsx
import React, { useState, useEffect } from 'react';
import { Switch, Input, Button, Card } from 'your-ui-library';

interface ConversationSettings {
  enabled: boolean;
  use_context: boolean;
  max_messages: number;
  timeout_seconds: number;
  cleanup_interval_seconds: number;
  session_ttl_seconds: number;
  max_llm_history_messages: number;
}

export const ConversationTab: React.FC = () => {
  const [settings, setSettings] = useState<ConversationSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    loadSettings();
  }, []);

  const loadSettings = async () => {
    setLoading(true);
    try {
      const response = await fetch('/api/conversation/settings');
      const data = await response.json();
      setSettings(data);
    } catch (error) {
      console.error('Failed to load settings:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      await fetch('/api/conversation/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings)
      });
      // Show success message
    } catch (error) {
      console.error('Failed to save settings:', error);
      // Show error message
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div>Loading...</div>;
  if (!settings) return <div>Error loading settings</div>;

  return (
    <Card title="Conversation Context Settings">
      {/* Enable/Disable Conversation Context */}
      <div className="setting-row">
        <label>Enable Conversation Context</label>
        <Switch
          checked={settings.enabled}
          onChange={(checked) => setSettings({ ...settings, enabled: checked })}
        />
      </div>

      {/* Use Context */}
      <div className="setting-row">
        <label>Use Context for Follow-ups</label>
        <Switch
          checked={settings.use_context}
          onChange={(checked) => setSettings({ ...settings, use_context: checked })}
          disabled={!settings.enabled}
        />
      </div>

      {/* Max Messages */}
      <div className="setting-row">
        <label>Maximum Messages to Track</label>
        <Input
          type="number"
          value={settings.max_messages}
          onChange={(e) => setSettings({ ...settings, max_messages: parseInt(e.target.value) })}
          min={1}
          max={100}
          disabled={!settings.enabled}
        />
        <span className="help-text">1-100 messages</span>
      </div>

      {/* Timeout */}
      <div className="setting-row">
        <label>Session Timeout</label>
        <Input
          type="number"
          value={settings.timeout_seconds}
          onChange={(e) => setSettings({ ...settings, timeout_seconds: parseInt(e.target.value) })}
          min={60}
          max={7200}
          disabled={!settings.enabled}
        />
        <span className="help-text">{settings.timeout_seconds / 60} minutes</span>
      </div>

      {/* LLM History */}
      <div className="setting-row">
        <label>Messages Sent to LLM</label>
        <Input
          type="number"
          value={settings.max_llm_history_messages}
          onChange={(e) => setSettings({ ...settings, max_llm_history_messages: parseInt(e.target.value) })}
          min={2}
          max={50}
          disabled={!settings.enabled}
        />
        <span className="help-text">
          Last {settings.max_llm_history_messages} messages ({settings.max_llm_history_messages / 2} exchanges)
        </span>
      </div>

      {/* Advanced Settings (Collapsible) */}
      <details>
        <summary>Advanced Settings</summary>

        <div className="setting-row">
          <label>Cleanup Interval</label>
          <Input
            type="number"
            value={settings.cleanup_interval_seconds}
            onChange={(e) => setSettings({ ...settings, cleanup_interval_seconds: parseInt(e.target.value) })}
            min={10}
            max={600}
          />
          <span className="help-text">Seconds between cleanup runs</span>
        </div>

        <div className="setting-row">
          <label>Session TTL</label>
          <Input
            type="number"
            value={settings.session_ttl_seconds}
            onChange={(e) => setSettings({ ...settings, session_ttl_seconds: parseInt(e.target.value) })}
            min={300}
            max={86400}
          />
          <span className="help-text">{settings.session_ttl_seconds / 3600} hours</span>
        </div>
      </details>

      {/* Save Button */}
      <div className="actions">
        <Button onClick={loadSettings} disabled={saving}>
          Reset
        </Button>
        <Button onClick={handleSave} disabled={saving} variant="primary">
          {saving ? 'Saving...' : 'Save Changes'}
        </Button>
      </div>
    </Card>
  );
};
```

## Example Component: SportsTeams

```tsx
import React, { useState, useEffect } from 'react';
import { Table, Button, Modal, Input, Badge } from 'your-ui-library';

interface SportsTeam {
  id: number;
  team_name: string;
  requires_disambiguation: boolean;
  options: Array<{
    id: string;
    label: string;
    sport: string;
  }>;
}

export const SportsTeams: React.FC = () => {
  const [teams, setTeams] = useState<SportsTeam[]>([]);
  const [editingTeam, setEditingTeam] = useState<SportsTeam | null>(null);
  const [showModal, setShowModal] = useState(false);

  useEffect(() => {
    loadTeams();
  }, []);

  const loadTeams = async () => {
    const response = await fetch('/api/conversation/sports-teams');
    const data = await response.json();
    setTeams(data);
  };

  const handleSave = async (team: SportsTeam) => {
    if (team.id) {
      // Update existing
      await fetch(`/api/conversation/sports-teams/${team.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(team)
      });
    } else {
      // Create new
      await fetch('/api/conversation/sports-teams', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(team)
      });
    }
    setShowModal(false);
    loadTeams();
  };

  const handleDelete = async (id: number) => {
    if (confirm('Delete this team disambiguation rule?')) {
      await fetch(`/api/conversation/sports-teams/${id}`, { method: 'DELETE' });
      loadTeams();
    }
  };

  return (
    <div>
      <div className="header">
        <h2>Sports Team Disambiguation</h2>
        <Button onClick={() => { setEditingTeam(null); setShowModal(true); }}>
          Add Team
        </Button>
      </div>

      <Table
        data={teams}
        columns={[
          {
            key: 'team_name',
            header: 'Team Name',
            render: (team) => <strong>{team.team_name}</strong>
          },
          {
            key: 'requires_disambiguation',
            header: 'Status',
            render: (team) => (
              team.requires_disambiguation
                ? <Badge color="yellow">Requires Clarification</Badge>
                : <Badge color="green">No Clarification</Badge>
            )
          },
          {
            key: 'options',
            header: 'Options',
            render: (team) => (
              <div>
                {team.options.map(opt => (
                  <span key={opt.id} className="option-badge">
                    {opt.label}
                  </span>
                ))}
              </div>
            )
          },
          {
            key: 'actions',
            header: 'Actions',
            render: (team) => (
              <div>
                <Button size="small" onClick={() => { setEditingTeam(team); setShowModal(true); }}>
                  Edit
                </Button>
                <Button size="small" variant="danger" onClick={() => handleDelete(team.id)}>
                  Delete
                </Button>
              </div>
            )
          }
        ]}
      />

      {/* Edit Modal - Implementation details omitted */}
    </div>
  );
};
```

## Styling Approach

### Tailwind CSS (Recommended)

```tsx
// Example with Tailwind classes
<div className="flex items-center justify-between p-4 border-b">
  <label className="text-sm font-medium text-gray-700">
    Enable Conversation Context
  </label>
  <Switch
    className="ml-4"
    checked={settings.enabled}
    onChange={(checked) => setSettings({ ...settings, enabled: checked })}
  />
</div>

<div className="mt-4 p-4 bg-blue-50 border border-blue-200 rounded-md">
  <p className="text-sm text-blue-800">
    💡 Conversation context allows Athena to remember previous messages and provide more intelligent follow-up responses.
  </p>
</div>
```

## State Management

Consider using React Context or a state management library for shared state:

```tsx
// ConversationSettingsContext.tsx
import React, { createContext, useContext, useState } from 'react';

interface ConversationState {
  settings: ConversationSettings | null;
  clarificationSettings: ClarificationSettings | null;
  types: ClarificationType[];
  reload: () => Promise<void>;
}

const ConversationContext = createContext<ConversationState | null>(null);

export const useConversationSettings = () => {
  const context = useContext(ConversationContext);
  if (!context) throw new Error('Must be used within ConversationProvider');
  return context;
};

export const ConversationProvider: React.FC = ({ children }) => {
  const [settings, setSettings] = useState<ConversationSettings | null>(null);
  // ... other state

  const reload = async () => {
    // Reload all settings
  };

  return (
    <ConversationContext.Provider value={{ settings, clarificationSettings, types, reload }}>
      {children}
    </ConversationContext.Provider>
  );
};
```

## Real-time Updates

For live configuration updates, consider using WebSocket or Server-Sent Events:

```tsx
useEffect(() => {
  const eventSource = new EventSource('/api/conversation/events');

  eventSource.addEventListener('settings-updated', (event) => {
    const data = JSON.parse(event.data);
    setSettings(data);
  });

  return () => eventSource.close();
}, []);
```

## Validation

```tsx
const validateSettings = (settings: ConversationSettings): string[] => {
  const errors = [];

  if (settings.max_messages < 1 || settings.max_messages > 100) {
    errors.push('Max messages must be between 1 and 100');
  }

  if (settings.timeout_seconds < 60 || settings.timeout_seconds > 7200) {
    errors.push('Timeout must be between 60 and 7200 seconds');
  }

  if (settings.max_llm_history_messages > settings.max_messages) {
    errors.push('LLM history cannot exceed max messages');
  }

  return errors;
};
```

## Testing

```tsx
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ConversationTab } from './ConversationTab';

describe('ConversationTab', () => {
  it('loads and displays settings', async () => {
    global.fetch = jest.fn(() =>
      Promise.resolve({
        json: () => Promise.resolve({
          enabled: true,
          max_messages: 20,
          timeout_seconds: 1800
        })
      })
    );

    render(<ConversationTab />);

    await waitFor(() => {
      expect(screen.getByLabelText(/enable conversation context/i)).toBeChecked();
    });
  });

  it('saves settings when button clicked', async () => {
    // ... test implementation
  });
});
```

## Deployment Notes

1. **Build Process**: Uses React + TypeScript + Vite/Webpack
2. **Static Assets**: Built files served by nginx
3. **API Proxy**: nginx proxies `/api/*` to FastAPI backend
4. **Authentication**: Uses OIDC tokens from Authentik

## Current Implementation

Since the current frontend is vanilla JS, you can add conversation settings using vanilla JavaScript and fetch API. See `admin/frontend/app.js` for the current implementation pattern.

## Future Migration Path

1. Set up React project with TypeScript
2. Configure build process (Vite recommended)
3. Migrate existing pages one by one
4. Implement conversation settings components
5. Add state management
6. Deploy updated frontend

---

**Last Updated:** 2025-11-15
**Next Steps:** Implement orchestrator integration, then build React UI when frontend migrates to React
