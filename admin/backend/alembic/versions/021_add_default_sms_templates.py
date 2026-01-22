"""Add default SMS templates for common requests.

Revision ID: 021_add_default_sms_templates
Revises: 020_add_sports_standings_tool
Create Date: 2025-12-03

Adds SMS templates for common guest requests:
- Weather forecast
- Restaurant/dining recommendations
- Directions/addresses
- WiFi credentials
- Local events
- Opt-out instructions
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '021_add_default_sms_templates'
down_revision = '020_add_sports_standings_tool'
branch_labels = None
depends_on = None


def upgrade():
    """Insert default SMS templates for common requests."""

    # Weather template
    op.execute("""
        INSERT INTO sms_templates (name, category, subject, body, variables, enabled)
        VALUES (
            'weather_forecast',
            'info',
            'Weather Forecast',
            '{location} Weather:

{forecast}

Have a great day!

Msg&data rates may apply. Reply STOP to unsubscribe.',
            '["location", "forecast"]'::jsonb,
            true
        )
        ON CONFLICT (name) DO NOTHING;
    """)

    # Restaurant/Dining recommendations
    op.execute("""
        INSERT INTO sms_templates (name, category, subject, body, variables, enabled)
        VALUES (
            'dining_recommendations',
            'recommendations',
            'Restaurant Recommendations',
            'Top restaurants near you:

{restaurants}

Enjoy your meal!

Msg&data rates may apply. Reply STOP to unsubscribe.',
            '["restaurants"]'::jsonb,
            true
        )
        ON CONFLICT (name) DO NOTHING;
    """)

    # Directions/Address template
    op.execute("""
        INSERT INTO sms_templates (name, category, subject, body, variables, enabled)
        VALUES (
            'directions',
            'info',
            'Directions',
            '{destination}

Address: {address}

{directions}

Msg&data rates may apply. Reply STOP to unsubscribe.',
            '["destination", "address", "directions"]'::jsonb,
            true
        )
        ON CONFLICT (name) DO NOTHING;
    """)

    # WiFi credentials template
    op.execute("""
        INSERT INTO sms_templates (name, category, subject, body, variables, enabled)
        VALUES (
            'wifi_credentials',
            'property',
            'WiFi Information',
            'WiFi Details:

Network: {wifi_name}
Password: {wifi_password}

Connect and enjoy!

Msg&data rates may apply. Reply STOP to unsubscribe.',
            '["wifi_name", "wifi_password"]'::jsonb,
            true
        )
        ON CONFLICT (name) DO NOTHING;
    """)

    # Local events template
    op.execute("""
        INSERT INTO sms_templates (name, category, subject, body, variables, enabled)
        VALUES (
            'local_events',
            'recommendations',
            'Local Events',
            'Upcoming events near {location}:

{events}

Have fun!

Msg&data rates may apply. Reply STOP to unsubscribe.',
            '["location", "events"]'::jsonb,
            true
        )
        ON CONFLICT (name) DO NOTHING;
    """)

    # Property info/address template
    op.execute("""
        INSERT INTO sms_templates (name, category, subject, body, variables, enabled)
        VALUES (
            'property_address',
            'property',
            'Property Address',
            '{property_name}

{address}

{additional_info}

Msg&data rates may apply. Reply STOP to unsubscribe.',
            '["property_name", "address", "additional_info"]'::jsonb,
            true
        )
        ON CONFLICT (name) DO NOTHING;
    """)

    # General info template (for text-me-that)
    op.execute("""
        INSERT INTO sms_templates (name, category, subject, body, variables, enabled)
        VALUES (
            'general_info',
            'info',
            'Information',
            '{content}

Msg&data rates may apply. Reply STOP to unsubscribe.',
            '["content"]'::jsonb,
            true
        )
        ON CONFLICT (name) DO NOTHING;
    """)

    # Opt-out confirmation
    op.execute("""
        INSERT INTO sms_templates (name, category, subject, body, variables, enabled)
        VALUES (
            'opt_out_confirmation',
            'system',
            'Opt-Out Confirmed',
            'You have been unsubscribed from Athena messages. Reply START to receive messages again.',
            '[]'::jsonb,
            true
        )
        ON CONFLICT (name) DO NOTHING;
    """)

    # Opt-in confirmation
    op.execute("""
        INSERT INTO sms_templates (name, category, subject, body, variables, enabled)
        VALUES (
            'opt_in_confirmation',
            'system',
            'Opt-In Confirmed',
            'Welcome back! You will now receive messages from Athena. Reply STOP anytime to opt out.',
            '[]'::jsonb,
            true
        )
        ON CONFLICT (name) DO NOTHING;
    """)

    # Checkout reminder template
    op.execute("""
        INSERT INTO sms_templates (name, category, subject, body, variables, enabled)
        VALUES (
            'checkout_reminder',
            'checkout',
            'Checkout Reminder',
            'Hi {guest_name}! Reminder: Checkout is at {checkout_time}.

Please:
- Leave keys {key_instructions}
- {additional_checkout_info}

Thanks for staying with us!

Msg&data rates may apply. Reply STOP to unsubscribe.',
            '["guest_name", "checkout_time", "key_instructions", "additional_checkout_info"]'::jsonb,
            true
        )
        ON CONFLICT (name) DO NOTHING;
    """)


def downgrade():
    """Remove default SMS templates."""
    op.execute("""
        DELETE FROM sms_templates
        WHERE name IN (
            'weather_forecast',
            'dining_recommendations',
            'directions',
            'wifi_credentials',
            'local_events',
            'property_address',
            'general_info',
            'opt_out_confirmation',
            'opt_in_confirmation',
            'checkout_reminder'
        );
    """)
