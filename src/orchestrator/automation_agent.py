"""
Dynamic Automation Agent for Smart Home Control

Uses LLM with primitive tools to handle any automation request dynamically.
No pattern matching - the LLM decides how to execute commands including:
- Immediate actions
- Sequences with delays
- Scheduled automations
- Recurring schedules

Supports guest-scoped automations with archival/restoration.
"""

import asyncio
import json
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Union
import structlog

logger = structlog.get_logger()


# Tool definitions for the LLM
AUTOMATION_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ha_service",
            "description": "Execute an immediate Home Assistant service call. Use for actions that should happen RIGHT NOW. Works with any entity type (lights, switches, climate, locks, covers, media players, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Service domain (light, switch, climate, lock, cover, media_player, scene, script, etc.)"
                    },
                    "service": {
                        "type": "string",
                        "description": "Service name (turn_on, turn_off, toggle, set_temperature, lock, unlock, open_cover, close_cover, etc.)"
                    },
                    "entity_id": {
                        "type": "string",
                        "description": "Target entity ID or comma-separated list (e.g., 'light.office', 'light.kitchen,light.living_room')"
                    },
                    "data": {
                        "type": "object",
                        "description": "Optional service data (brightness, color, temperature, etc.)",
                        "properties": {
                            "brightness": {"type": "integer", "description": "Brightness 0-255"},
                            "hs_color": {"type": "array", "description": "[hue 0-360, saturation 0-100]"},
                            "rgb_color": {"type": "array", "description": "[red, green, blue] 0-255 each"},
                            "temperature": {"type": "number", "description": "Temperature setpoint"},
                            "hvac_mode": {"type": "string", "description": "HVAC mode (heat, cool, auto, off)"}
                        }
                    }
                },
                "required": ["domain", "service", "entity_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Pause execution for specified duration. Use between immediate actions for sequences like 'turn on, wait 3 seconds, turn off'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "integer",
                        "description": "Number of seconds to wait (max 300 = 5 minutes)"
                    }
                },
                "required": ["seconds"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_automation",
            "description": "Create an automation in Home Assistant triggered by time, motion, device state changes, or sun events. Supports compound conditions. Returns immediately with confirmation - the automation runs later.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Human-readable name for this automation (e.g., 'Kitchen motion lights', 'Evening porch lights')"
                    },
                    "triggers": {
                        "type": "array",
                        "description": "One or more triggers (ANY trigger activates the automation)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["time", "sunset", "sunrise", "state_change", "motion", "numeric_state", "time_pattern", "device"],
                                    "description": "Trigger type: time (at specific time), sunset/sunrise (sun events), state_change (entity state changes), motion (motion sensor activated), numeric_state (sensor crosses threshold), time_pattern (recurring intervals), device (button press, switch toggle)"
                                },
                                "above": {
                                    "type": "number",
                                    "description": "Trigger when value goes ABOVE this threshold (for numeric_state)"
                                },
                                "below": {
                                    "type": "number",
                                    "description": "Trigger when value goes BELOW this threshold (for numeric_state)"
                                },
                                "time": {
                                    "type": "string",
                                    "description": "Time in HH:MM 24-hour format (for type=time)"
                                },
                                "offset": {
                                    "type": "string",
                                    "description": "Offset from sun event, e.g., '-00:30:00' for 30min before (for sunset/sunrise)"
                                },
                                "entity_id": {
                                    "type": "string",
                                    "description": "Entity to monitor (for state_change/motion/device). Motion sensors: binary_sensor.{room}_motion. Buttons: button.{name}, binary_sensor.{name}_button"
                                },
                                "to_state": {
                                    "type": "string",
                                    "description": "Target state to trigger on (e.g., 'on', 'off', 'home', 'away')"
                                },
                                "from_state": {
                                    "type": "string",
                                    "description": "Optional: Only trigger when changing FROM this state"
                                },
                                "hours": {
                                    "type": "string",
                                    "description": "Hour pattern for time_pattern (e.g., '/2' for every 2 hours, '8' for 8am, '*' for every hour)"
                                },
                                "minutes": {
                                    "type": "string",
                                    "description": "Minute pattern for time_pattern (e.g., '/30' for every 30 min, '0' for on the hour, '*' for every minute)"
                                },
                                "seconds": {
                                    "type": "string",
                                    "description": "Second pattern for time_pattern (e.g., '/10' for every 10 seconds)"
                                },
                                "event_type": {
                                    "type": "string",
                                    "description": "Device event type (for device trigger): 'pressed', 'double_pressed', 'long_pressed', 'released'"
                                }
                            },
                            "required": ["type"]
                        }
                    },
                    "trigger": {
                        "type": "object",
                        "description": "DEPRECATED: Single trigger (use 'triggers' array instead for flexibility)",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["time", "sunset", "sunrise", "state_change", "motion"],
                                "description": "Trigger type"
                            },
                            "time": {
                                "type": "string",
                                "description": "Time in HH:MM 24-hour format (for type=time)"
                            },
                            "offset": {
                                "type": "string",
                                "description": "Offset from sun event (for sunset/sunrise)"
                            },
                            "entity_id": {
                                "type": "string",
                                "description": "Entity to monitor (for state_change/motion)"
                            },
                            "to_state": {
                                "type": "string",
                                "description": "Target state to trigger on"
                            }
                        },
                        "required": ["type"]
                    },
                    "conditions": {
                        "type": "array",
                        "description": "Optional conditions that must be true for automation to run",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["weekday", "time_range", "state"],
                                    "description": "Condition type"
                                },
                                "weekdays": {
                                    "type": "array",
                                    "items": {"type": "string", "enum": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
                                    "description": "Days when automation should run (for weekday condition)"
                                },
                                "after": {"type": "string", "description": "Start time HH:MM (for time_range)"},
                                "before": {"type": "string", "description": "End time HH:MM (for time_range)"},
                                "entity_id": {"type": "string", "description": "Entity to check (for state condition)"},
                                "state": {"type": "string", "description": "Required state value (for state condition)"}
                            }
                        }
                    },
                    "actions": {
                        "type": "array",
                        "description": "Actions to perform when automation triggers",
                        "items": {
                            "type": "object",
                            "properties": {
                                "service": {"type": "string", "description": "Service call (e.g., 'light.turn_on')"},
                                "entity_id": {"type": "string", "description": "Target entity"},
                                "data": {"type": "object", "description": "Service data"},
                                "delay": {"type": "string", "description": "Delay before next action (HH:MM:SS format)"}
                            },
                            "required": ["service", "entity_id"]
                        }
                    },
                    "one_time": {
                        "type": "boolean",
                        "description": "If true, automation deletes itself after running once"
                    }
                },
                "required": ["name", "trigger", "actions"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_automations",
            "description": "List voice-created automations. Use when user asks about their routines, schedules, or automations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "include_archived": {
                        "type": "boolean",
                        "description": "Include archived guest automations (for returning guests)"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_automation",
            "description": "Delete or archive an automation. Guests' automations are archived (can be restored), owner's are deleted.",
            "parameters": {
                "type": "object",
                "properties": {
                    "automation_id": {
                        "type": "integer",
                        "description": "ID of automation to delete (from list_automations)"
                    },
                    "name_search": {
                        "type": "string",
                        "description": "Or search by name (partial match)"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_entity_state",
            "description": "Get current state of an entity. Use to check conditions or report status before taking action.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "Entity to check (e.g., 'light.office', 'climate.living_room')"
                    }
                },
                "required": ["entity_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "notify",
            "description": "Send a notification or announcement. Use for alerting the user about something. Can speak, send push notification, or flash lights.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The notification message (for TTS/mobile)"
                    },
                    "target": {
                        "type": "string",
                        "enum": ["tts", "mobile", "flash", "flash_all", "all"],
                        "description": "How to notify: tts (speak), mobile (push), flash (flash lights in room), flash_all (flash all house lights), all (tts + mobile + flash)"
                    },
                    "room": {
                        "type": "string",
                        "description": "For TTS or flash, which room (defaults to current room)"
                    },
                    "flash_count": {
                        "type": "integer",
                        "description": "Number of times to flash lights (default 3)"
                    },
                    "flash_color": {
                        "type": "array",
                        "description": "Optional RGB color for flash [R, G, B] 0-255 each. Default is bright white."
                    }
                },
                "required": ["message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Complete the task and respond to user. ALWAYS call this when finished with all actions. The message will be spoken aloud.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Brief, natural response to speak to the user (e.g., 'Done!', 'I've set that up for 6pm.')"
                    }
                },
                "required": ["message"]
            }
        }
    }
]


class AutomationAgent:
    """
    Agent loop for dynamic automation handling.

    Uses LLM with tools to handle any smart home automation request.
    Supports immediate actions, sequences, scheduled automations, and recurring schedules.
    """

    def __init__(self, ha_client, llm_router, admin_client=None, entity_manager=None):
        """
        Initialize the automation agent.

        Args:
            ha_client: Home Assistant client for service calls
            llm_router: LLM router for chat completions with tools
            admin_client: Optional admin client for storing automations
            entity_manager: Optional entity manager for resolving room names to entities
        """
        self.ha_client = ha_client
        self.llm = llm_router
        self.admin = admin_client
        self.entity_manager = entity_manager
        self.tools = AUTOMATION_TOOLS

    async def execute(
        self,
        query: str,
        context: Dict[str, Any],
        model: str = "llama3.1:8b"
    ) -> str:
        """
        Execute agent loop for automation-related queries.

        Args:
            query: User's natural language request
            context: Contains room, mode (owner/guest), session_id, guest_name
            model: LLM model to use

        Returns:
            Single response message to user
        """
        mode = context.get("mode", "owner")
        room = context.get("room", "office")
        session_id = context.get("session_id")
        guest_name = context.get("guest_name")

        # Build system prompt with context
        system_prompt = self._build_system_prompt(mode, room, guest_name)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query}
        ]

        max_iterations = 20  # Safety limit for complex sequences
        iteration = 0
        start_time = time.time()

        logger.info(f"AutomationAgent starting: query='{query[:50]}...', mode={mode}, room={room}")

        while iteration < max_iterations:
            iteration += 1

            try:
                # Get LLM response with tool calling
                response = await self._call_llm_with_tools(messages, model)

                # Check for tool calls
                tool_calls = response.get("tool_calls", [])

                if not tool_calls:
                    # LLM wants to respond directly (unusual but allowed)
                    content = response.get("content", "I'm not sure how to help with that.")
                    logger.info(f"AutomationAgent completed without tools: {content[:50]}...")
                    return content

                # Execute each tool call
                for tool_call in tool_calls:
                    tool_name = tool_call.get("function", {}).get("name")
                    tool_args = tool_call.get("function", {}).get("arguments", {})

                    # Parse arguments if they're a string
                    if isinstance(tool_args, str):
                        try:
                            tool_args = json.loads(tool_args)
                        except json.JSONDecodeError:
                            tool_args = {}

                    logger.info(f"AutomationAgent executing tool: {tool_name}, args={tool_args}")

                    # Execute the tool
                    result = await self._execute_tool(tool_name, tool_args, context)

                    # Check if this is the done tool
                    if tool_name == "done":
                        elapsed = time.time() - start_time
                        logger.info(f"AutomationAgent completed in {elapsed:.2f}s after {iteration} iterations: {result[:50]}...")
                        return result

                    # Add tool result to conversation for next iteration
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [tool_call]
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.get("id", f"call_{iteration}"),
                        "content": str(result)
                    })

            except Exception as e:
                logger.error(f"AutomationAgent error in iteration {iteration}: {e}")
                return f"I encountered an error: {str(e)}"

        # Safety fallback
        logger.warning(f"AutomationAgent hit max iterations ({max_iterations})")
        return "I had trouble completing that request. Please try again with a simpler command."

    def _build_system_prompt(self, mode: str, room: str, guest_name: Optional[str]) -> str:
        """Build system prompt with context."""
        current_time = datetime.now().strftime("%H:%M")
        current_date = datetime.now().strftime("%A, %B %d")

        prompt = f"""You are Jarvis, a smart home assistant. You help control devices and create automations.

Current Context:
- Time: {current_time}
- Date: {current_date}
- Room: {room}
- Mode: {mode}{"" if mode == "owner" else f" (Guest: {guest_name})"}

Your Tools:
1. ha_service - Execute immediate actions (lights, climate, locks, etc.)
2. wait - Pause between actions for sequences
3. create_automation - Create triggered automations (time, motion, state changes, sun events)
4. list_automations - Show existing automations
5. delete_automation - Remove an automation
6. get_entity_state - Check current state of devices
7. notify - Alert user via TTS, mobile push, or flashing lights (target: tts/mobile/flash/flash_all/all)
8. done - Complete the task with a spoken response

Guidelines:
- For immediate actions like "turn on the lights", use ha_service then done
- For sequences like "turn on, wait 5 seconds, turn off", chain ha_service + wait + ha_service + done
- For scheduled actions like "at 6pm turn on lights", use create_automation with time trigger then done
- For motion-triggered like "when motion in kitchen turn on lights", use create_automation with motion trigger
- For alerts like "let me know when X is full", use create_automation with state_change trigger and notify action
- For compound triggers, use create_automation with triggers array + conditions
- Always end with done() to speak a response to the user
- Keep responses brief and natural - they will be spoken aloud
- Use the current room ({room}) if no room is specified
- Resolve room names to entity IDs using the pattern: light.{{room}}, switch.{{room}}, etc.

Trigger Types:
- time: Fixed time (e.g., "18:00" for 6pm)
- motion: Motion sensor activated (entity_id: binary_sensor.{{room}}_motion)
- state_change: Any entity state change (specify entity_id and to_state)
- numeric_state: Sensor crosses threshold (entity_id + above/below value)
- sunset/sunrise: Sun events with optional offset
- time_pattern: Recurring intervals (hours: "/2" for every 2 hours, minutes: "/30" for every 30 min)
- device: Button press or switch toggle (entity_id + event_type: pressed/double_pressed/long_pressed)

Time Pattern Examples:
- Every 30 minutes: time_pattern with minutes: "/30"
- Every 2 hours: time_pattern with hours: "/2"
- On the hour: time_pattern with minutes: "0"

Device Trigger Examples:
- When button pressed: device with entity_id: button.office_button, event_type: pressed
- When doorbell rings: device with entity_id: binary_sensor.doorbell, event_type: pressed
- Double press: device with entity_id: button.bedroom, event_type: double_pressed

Compound Trigger Examples:
- Motion AND after 6pm: Use motion trigger + time_range condition (after: "18:00")
- Motion OR sunset: Use triggers array with both types

Common Entity Patterns:
- Lights: light.office, light.kitchen, light.beta, light.alpha, light.living_room
- Switches: switch.office_fan, switch.porch
- Climate: climate.main, climate.bedroom
- Locks: lock.front_door, lock.back_door
- Covers: cover.garage, cover.blinds
- Motion: binary_sensor.kitchen_motion, binary_sensor.office_motion, binary_sensor.living_room_motion
- Doors: binary_sensor.front_door, binary_sensor.back_door, binary_sensor.garage_door
- Buttons: button.office_button, binary_sensor.doorbell, button.bedroom_switch
- Temperature: sensor.living_room_temperature, sensor.office_temperature, sensor.outdoor_temperature

Color Reference (hue values for hs_color):
- Red: [0, 100]
- Orange: [30, 100]
- Yellow: [60, 100]
- Green: [120, 100]
- Cyan: [180, 100]
- Blue: [240, 100]
- Purple: [280, 100]
- Pink: [330, 100]
- White: [0, 0] (with high brightness)
"""
        return prompt

    async def _call_llm_with_tools(self, messages: List[Dict], model: str) -> Dict:
        """Call LLM with tool definitions."""
        try:
            # Use the LLM router's chat_with_tools method if available
            if hasattr(self.llm, 'chat_with_tools'):
                return await self.llm.chat_with_tools(
                    messages=messages,
                    tools=self.tools,
                    model=model,
                    temperature=0.1  # Low temperature for consistent tool use
                )

            # Fallback: Use generate with tool instructions in prompt
            # Convert messages to a prompt string
            prompt = self._messages_to_prompt(messages)

            response = await self.llm.generate(
                model=model,
                prompt=prompt,
                temperature=0.1,
                max_tokens=2000
            )

            # Parse tool calls from response
            return self._parse_tool_calls_from_text(response.get('response', ''))

        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            raise

    def _messages_to_prompt(self, messages: List[Dict]) -> str:
        """Convert messages to a prompt string for non-tool-calling models."""
        prompt_parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                prompt_parts.append(f"System: {content}")
            elif role == "user":
                prompt_parts.append(f"User: {content}")
            elif role == "assistant":
                if content:
                    prompt_parts.append(f"Assistant: {content}")
            elif role == "tool":
                prompt_parts.append(f"Tool Result: {content}")

        prompt_parts.append("\nRespond with a JSON tool call or a direct response. Format for tool calls:")
        prompt_parts.append('{"tool": "tool_name", "arguments": {...}}')
        prompt_parts.append("\nAssistant:")

        return "\n".join(prompt_parts)

    def _parse_tool_calls_from_text(self, text: str) -> Dict:
        """Parse tool calls from LLM text response.

        Handles multiple formats in ORDER of appearance:
        - Standard JSON: {"tool": "ha_service", "arguments": {...}}
        - Shorthand: wait(2), done("message")
        - Multiple tool calls separated by newlines or whitespace
        """
        import re

        # Build a list of (position, tool_call) tuples
        found_calls = []

        # Find JSON tool calls - balanced braces
        i = 0
        while i < len(text):
            if text[i] == '{':
                depth = 1
                j = i + 1
                while j < len(text) and depth > 0:
                    if text[j] == '{':
                        depth += 1
                    elif text[j] == '}':
                        depth -= 1
                    j += 1

                if depth == 0:
                    json_str = text[i:j]
                    try:
                        parsed = json.loads(json_str)
                        if 'tool' in parsed:
                            found_calls.append((i, {
                                "id": f"call_{uuid.uuid4().hex[:8]}",
                                "function": {
                                    "name": parsed["tool"],
                                    "arguments": parsed.get("arguments", {})
                                }
                            }))
                            logger.info("automation_agent_parsed_tool_call",
                                       tool=parsed["tool"],
                                       args=parsed.get("arguments", {}),
                                       position=i)
                    except json.JSONDecodeError:
                        pass
                    i = j
                else:
                    i += 1
            else:
                i += 1

        # Find shorthand function calls like wait(2), done("message")
        shorthand_pattern = r'(wait|done|get_entity_state)\(([^)]*)\)'
        for match in re.finditer(shorthand_pattern, text):
            func_name = match.group(1)
            args_str = match.group(2).strip()
            pos = match.start()

            args = {}
            if func_name == 'wait' and args_str:
                try:
                    args = {"seconds": int(args_str)}
                except ValueError:
                    args = {"seconds": 1}
            elif func_name == 'done' and args_str:
                args = {"message": args_str.strip('"\'').strip()}

            found_calls.append((pos, {
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "function": {
                    "name": func_name,
                    "arguments": args
                }
            }))
            logger.info("automation_agent_parsed_shorthand",
                       tool=func_name,
                       args=args,
                       position=pos)

        # Sort by position to maintain order
        found_calls.sort(key=lambda x: x[0])
        tool_calls = [tc for _, tc in found_calls]

        if tool_calls:
            logger.info("automation_agent_total_tools_parsed", count=len(tool_calls))
            return {"tool_calls": tool_calls}

        # No tool call found, return as content
        # Clean any JSON-like content from the response for voice output
        clean_text = self._strip_json_from_response(text)
        return {"content": clean_text}

    def _strip_json_from_response(self, text: str) -> str:
        """
        Remove JSON blocks from text intended for voice output.

        LLMs sometimes include JSON tool calls in their response even when
        they should be speaking naturally. This strips those out.
        """
        import re

        # Remove JSON blocks {...}
        result = text
        i = 0
        while i < len(result):
            if result[i] == '{':
                depth = 1
                j = i + 1
                while j < len(result) and depth > 0:
                    if result[j] == '{':
                        depth += 1
                    elif result[j] == '}':
                        depth -= 1
                    j += 1

                if depth == 0:
                    # Found a complete JSON block, remove it
                    result = result[:i] + result[j:]
                else:
                    i += 1
            else:
                i += 1

        # Remove shorthand tool calls like done("message"), wait(2)
        result = re.sub(r'\b(wait|done|get_entity_state)\([^)]*\)', '', result)

        # Clean up extra whitespace and newlines
        result = re.sub(r'\n\s*\n', '\n', result)
        result = result.strip()

        return result if result else "I'm having trouble with that request."

    async def _execute_tool(self, name: str, args: Dict, context: Dict) -> Any:
        """Execute a single tool call."""
        try:
            if name == "ha_service":
                return await self._exec_ha_service(args, context)

            elif name == "wait":
                seconds = min(args.get("seconds", 1), 300)  # Max 5 min
                await asyncio.sleep(seconds)
                return f"Waited {seconds} seconds"

            elif name == "create_automation":
                return await self._create_automation(args, context)

            elif name == "list_automations":
                return await self._list_automations(args, context)

            elif name == "delete_automation":
                return await self._delete_automation(args, context)

            elif name == "get_entity_state":
                return await self._get_state(args)

            elif name == "notify":
                return await self._send_notification(args, context)

            elif name == "done":
                return args.get("message", "Done.")

            else:
                return f"Unknown tool: {name}"

        except Exception as e:
            logger.error(f"Tool execution error ({name}): {e}")
            return f"Error executing {name}: {str(e)}"

    async def _exec_ha_service(self, args: Dict, context: Dict) -> str:
        """Execute immediate HA service call."""
        entity_id = args.get("entity_id", "")
        service = args.get("service", "turn_on")
        data = args.get("data", {})

        # Extract domain from entity_id if not explicitly provided
        # e.g., "media_player.office" -> domain = "media_player"
        if args.get("domain"):
            domain = args["domain"]
        elif entity_id and "." in entity_id:
            domain = entity_id.split(".")[0]
        else:
            domain = "light"  # Last resort default

        # Resolve room name to entity if needed
        if not entity_id and context.get("room"):
            entity_id = f"{domain}.{context['room']}"

        if entity_id:
            data["entity_id"] = entity_id

        try:
            await self.ha_client.call_service(domain, service, data)
            return f"Called {domain}.{service} on {entity_id}"
        except Exception as e:
            logger.error(f"HA service call failed: {e}")
            return f"Failed to call {domain}.{service}: {str(e)}"

    async def _create_automation(self, args: Dict, context: Dict) -> str:
        """Create automation in HA and optionally store in admin backend."""
        mode = context.get("mode", "owner")
        session_id = context.get("session_id")
        guest_name = context.get("guest_name")
        room = context.get("room")

        # Generate unique automation ID
        automation_id = f"voice_{mode}_{int(time.time())}_{uuid.uuid4().hex[:8]}"

        try:
            # Build HA automation config
            ha_config = self._build_ha_automation(automation_id, args)

            # Register with Home Assistant
            await self._register_ha_automation(automation_id, ha_config)

            # Store in admin backend if available
            if self.admin:
                await self._store_automation_record(
                    automation_id, args, context
                )

            name = args.get("name", "Automation")
            trigger = args.get("trigger", {})
            trigger_desc = trigger.get("time", trigger.get("type", "scheduled"))

            return f"Created automation '{name}' (triggers at {trigger_desc})"

        except Exception as e:
            logger.error(f"Failed to create automation: {e}")
            return f"Failed to create automation: {str(e)}"

    def _build_ha_automation(self, automation_id: str, args: Dict) -> Dict:
        """Convert tool args to HA automation format."""
        # Support both single trigger and triggers array
        triggers = args.get("triggers", [])
        if not triggers and args.get("trigger"):
            triggers = [args.get("trigger")]

        conditions = args.get("conditions", [])
        actions = args.get("actions", [])

        ha_automation = {
            "id": automation_id,
            "alias": args.get("name", "Voice Automation"),
            "trigger": [],
            "condition": [],
            "action": []
        }

        # Build triggers
        for trigger in triggers:
            ha_trigger = self._build_single_trigger(trigger)
            if ha_trigger:
                ha_automation["trigger"].append(ha_trigger)

        # Fallback if no triggers parsed
        if not ha_automation["trigger"]:
            ha_automation["trigger"].append({
                "platform": "time",
                "at": "12:00"
            })

        # Build conditions
        for cond in conditions:
            cond_type = cond.get("type")
            if cond_type == "weekday":
                weekdays = cond.get("weekdays", [])
                ha_automation["condition"].append({
                    "condition": "time",
                    "weekday": weekdays
                })
            elif cond_type == "time_range":
                ha_automation["condition"].append({
                    "condition": "time",
                    "after": cond.get("after"),
                    "before": cond.get("before")
                })
            elif cond_type == "state":
                ha_automation["condition"].append({
                    "condition": "state",
                    "entity_id": cond.get("entity_id"),
                    "state": cond.get("state")
                })

        # Build actions - handle multiple formats
        for action in actions:
            ha_action = self._build_single_action(action)
            if ha_action:
                ha_automation["action"].append(ha_action)

            # Add delay if specified
            if action.get("delay"):
                ha_automation["action"].append({
                    "delay": action.get("delay")
                })

        return ha_automation

    def _build_single_action(self, action: Dict) -> Optional[Dict]:
        """Build a single HA action from tool action format.

        Handles multiple formats:
        - Tool format: {'service': 'light.turn_on', 'entity_id': 'light.office'}
        - HA native: {'service': 'light.turn_on', 'target': {'entity_id': 'light.office'}}
        - LLM variant: {'platform': 'light', 'entity_id': 'light.kitchen', 'turn_on': {}}
        - ha_service variant: {'ha_service': 'light.office', 'service': 'turn_off'}
        """
        # Already in HA native format with target
        if "service" in action and "target" in action:
            return action

        # Tool format: {'service': 'light.turn_on', 'entity_id': 'light.office'}
        if "service" in action and "entity_id" in action and "." in action.get("service", ""):
            return {
                "service": action["service"],
                "target": {"entity_id": action["entity_id"]},
                "data": action.get("data", {})
            }

        # ha_service variant: {'ha_service': 'light.office', 'service': 'turn_off'}
        if "ha_service" in action:
            entity_id = action.get("ha_service")
            service_name = action.get("service", "turn_on")
            # Extract domain from entity_id
            domain = entity_id.split(".")[0] if "." in entity_id else "homeassistant"
            return {
                "service": f"{domain}.{service_name}",
                "target": {"entity_id": entity_id},
                "data": action.get("data", {})
            }

        # LLM platform variant: {'platform': 'light', 'entity_id': 'light.kitchen', 'turn_on': {}}
        if "platform" in action and "entity_id" in action:
            platform = action.get("platform")
            entity_id = action.get("entity_id")

            # Find the action (turn_on, turn_off, etc.)
            service_name = None
            service_data = {}
            for key in action:
                if key not in ("platform", "entity_id", "delay"):
                    service_name = key
                    if isinstance(action[key], dict):
                        service_data = action[key]
                    break

            if service_name:
                return {
                    "service": f"{platform}.{service_name}",
                    "target": {"entity_id": entity_id},
                    "data": service_data
                }

        # Simple service without domain
        if "service" in action:
            service = action.get("service", "")
            if "." not in service:
                service = f"homeassistant.{service}"
            return {
                "service": service,
                "target": {"entity_id": action.get("entity_id", "")},
                "data": action.get("data", {})
            }

        logger.warning(f"Unknown action format: {action}")
        return None

    def _build_single_trigger(self, trigger: Dict) -> Optional[Dict]:
        """Build a single HA trigger from tool trigger format.

        Handles both the expected tool format (with 'type' key) and
        HA's native format (with 'platform' key) for robustness.
        """
        # Check if this is already in HA's native format
        if "platform" in trigger and "type" not in trigger:
            # Already in HA format - validate and pass through
            platform = trigger.get("platform")
            if platform in ("time", "sun", "state", "numeric_state", "template"):
                logger.debug(f"Passing through HA-native trigger format: {platform}")
                return trigger
            else:
                logger.warning(f"Unknown HA trigger platform: {platform}")
                return None

        trigger_type = trigger.get("type", "time")

        if trigger_type == "time":
            time_val = trigger.get("time") or trigger.get("at", "12:00")

            # Handle nested time format: {'time': {'at': 'sunrise'}}
            if isinstance(time_val, dict):
                nested_at = time_val.get("at", "12:00")
                # Check if it's actually a sun event
                if nested_at in ("sunrise", "sunset"):
                    return {
                        "platform": "sun",
                        "event": nested_at
                    }
                time_val = nested_at

            # Handle case where time_val is "sunrise" or "sunset" string
            if time_val in ("sunrise", "sunset"):
                return {
                    "platform": "sun",
                    "event": time_val
                }

            return {
                "platform": "time",
                "at": time_val
            }

        elif trigger_type == "sunset":
            ha_trigger = {
                "platform": "sun",
                "event": "sunset"
            }
            if trigger.get("offset"):
                ha_trigger["offset"] = trigger.get("offset")
            return ha_trigger

        elif trigger_type == "sunrise":
            ha_trigger = {
                "platform": "sun",
                "event": "sunrise"
            }
            if trigger.get("offset"):
                ha_trigger["offset"] = trigger.get("offset")
            return ha_trigger

        elif trigger_type == "motion":
            # Motion sensors are binary_sensor.{room}_motion or binary_sensor.{name}_occupancy
            entity_id = trigger.get("entity_id")
            if not entity_id:
                # Try to infer from context - will be set by caller if needed
                logger.warning("Motion trigger without entity_id")
                return None

            ha_trigger = {
                "platform": "state",
                "entity_id": entity_id,
                "to": trigger.get("to_state", "on")  # Motion detected = on
            }
            if trigger.get("from_state"):
                ha_trigger["from"] = trigger.get("from_state")
            return ha_trigger

        elif trigger_type == "state_change":
            entity_id = trigger.get("entity_id")
            if not entity_id:
                logger.warning("State change trigger without entity_id")
                return None

            ha_trigger = {
                "platform": "state",
                "entity_id": entity_id
            }
            if trigger.get("to_state"):
                ha_trigger["to"] = trigger.get("to_state")
            if trigger.get("from_state"):
                ha_trigger["from"] = trigger.get("from_state")
            return ha_trigger

        elif trigger_type == "numeric_state":
            entity_id = trigger.get("entity_id")
            if not entity_id:
                logger.warning("Numeric state trigger without entity_id")
                return None

            ha_trigger = {
                "platform": "numeric_state",
                "entity_id": entity_id
            }
            if trigger.get("above") is not None:
                ha_trigger["above"] = trigger.get("above")
            if trigger.get("below") is not None:
                ha_trigger["below"] = trigger.get("below")
            return ha_trigger

        elif trigger_type == "time_pattern":
            # Recurring time patterns like "every 30 minutes", "every 2 hours"
            ha_trigger = {
                "platform": "time_pattern"
            }
            # Add hour pattern if specified
            if trigger.get("hours"):
                ha_trigger["hours"] = trigger.get("hours")
            # Add minute pattern if specified
            if trigger.get("minutes"):
                ha_trigger["minutes"] = trigger.get("minutes")
            # Add second pattern if specified
            if trigger.get("seconds"):
                ha_trigger["seconds"] = trigger.get("seconds")

            # Default to every hour if no pattern specified
            if len(ha_trigger) == 1:
                ha_trigger["hours"] = "/1"

            return ha_trigger

        elif trigger_type == "device":
            # Device triggers for buttons, switches, remotes
            entity_id = trigger.get("entity_id")
            if not entity_id:
                logger.warning("Device trigger without entity_id")
                return None

            event_type = trigger.get("event_type", "pressed")

            # For most button/switch devices, use state trigger
            # The entity transitions to "on" when pressed
            ha_trigger = {
                "platform": "state",
                "entity_id": entity_id
            }

            # Map event types to state changes
            if event_type in ["pressed", "single_pressed", "clicked"]:
                ha_trigger["to"] = "on"
            elif event_type in ["released", "off"]:
                ha_trigger["to"] = "off"
            elif event_type in ["double_pressed", "double_clicked"]:
                # Some devices expose double-press as a separate entity or attribute
                ha_trigger["to"] = "on"
                ha_trigger["attribute"] = "action"
                ha_trigger["to"] = "double"
            elif event_type in ["long_pressed", "held"]:
                ha_trigger["to"] = "on"
                ha_trigger["attribute"] = "action"
                ha_trigger["to"] = "hold"
            else:
                # Default to detecting any state change to "on"
                ha_trigger["to"] = "on"

            return ha_trigger

        else:
            logger.warning(f"Unknown trigger type: {trigger_type}")
            return None

    async def _register_ha_automation(self, automation_id: str, config: Dict):
        """Register automation with Home Assistant using the config API."""
        try:
            # Use the HA client's create_automation method
            success = await self.ha_client.create_automation(automation_id, config)

            if success:
                logger.info(
                    "ha_automation_registered",
                    automation_id=automation_id,
                    alias=config.get("alias")
                )
            else:
                logger.warning(
                    "ha_automation_registration_failed",
                    automation_id=automation_id,
                    message="HA API returned error"
                )
                raise Exception("Home Assistant rejected the automation configuration")

        except Exception as e:
            logger.error(
                "ha_automation_registration_error",
                automation_id=automation_id,
                error=str(e)
            )
            raise

    async def _store_automation_record(self, automation_id: str, args: Dict, context: Dict):
        """Store automation record in admin backend."""
        if not self.admin:
            return

        try:
            # Normalize trigger config - handle both 'trigger' (single) and 'triggers' (array)
            trigger_config = args.get("trigger") or {}
            triggers = args.get("triggers", [])
            if triggers and not trigger_config:
                # Use first trigger from array
                trigger_config = triggers[0] if triggers else {}

            # Normalize actions config - handle both 'action' (single) and 'actions' (array)
            actions = args.get("actions", [])
            action = args.get("action")
            if action and not actions:
                # Handle case where LLM puts a list in 'action' instead of 'actions'
                if isinstance(action, list):
                    actions = action
                else:
                    actions = [action]

            await self.admin.create_voice_automation({
                "name": args.get("name", "Voice Automation"),
                "ha_automation_id": automation_id,
                "owner_type": context.get("mode", "owner"),
                "guest_session_id": context.get("session_id") if context.get("mode") == "guest" else None,
                "guest_name": context.get("guest_name"),
                "created_by_room": context.get("room"),
                "trigger_config": trigger_config,
                "conditions_config": args.get("conditions", []),
                "actions_config": actions,
                "is_one_time": args.get("one_time", False),
                "status": "active"
            })
            logger.info(
                "voice_automation_stored",
                automation_id=automation_id,
                name=args.get("name")
            )
        except Exception as e:
            logger.warning(f"Could not store automation record: {e}")

    async def _list_automations(self, args: Dict, context: Dict) -> str:
        """List voice-created automations."""
        if not self.admin:
            return "Automation listing not available."

        try:
            include_archived = args.get("include_archived", False)
            mode = context.get("mode", "owner")
            guest_name = context.get("guest_name")

            automations = await self.admin.get_voice_automations(
                owner_type=mode,
                guest_name=guest_name if mode == "guest" else None,
                include_archived=include_archived
            )

            if not automations:
                return "You don't have any automations set up."

            descriptions = []
            for auto in automations:
                status = f" (archived)" if auto.get("status") == "archived" else ""
                descriptions.append(f"- {auto['name']}{status}")

            return f"Your automations:\n" + "\n".join(descriptions)

        except Exception as e:
            logger.error(f"Failed to list automations: {e}")
            return "Could not retrieve automations."

    async def _delete_automation(self, args: Dict, context: Dict) -> str:
        """Delete or archive an automation."""
        automation_id = args.get("automation_id")
        name_search = args.get("name_search")
        mode = context.get("mode", "owner")

        if not self.admin:
            return "Automation management not available."

        try:
            # Find automation by ID or name
            if name_search:
                automations = await self.admin.get_voice_automations(
                    owner_type=mode,
                    name_search=name_search
                )
                if automations:
                    automation_id = automations[0]["id"]
                else:
                    return f"No automation found matching '{name_search}'."

            if not automation_id:
                return "Please specify an automation to delete."

            # Archive for guests, delete for owner
            if mode == "guest":
                await self.admin.archive_voice_automation(automation_id, "user_deleted")
                return "I've archived that automation."
            else:
                await self.admin.delete_voice_automation(automation_id)
                return "I've deleted that automation."

        except Exception as e:
            logger.error(f"Failed to delete automation: {e}")
            return "Could not delete automation."

    async def _get_state(self, args: Dict) -> str:
        """Get entity state from Home Assistant."""
        entity_id = args.get("entity_id", "")

        try:
            state = await self.ha_client.get_state(entity_id)
            if state:
                state_value = state.get("state", "unknown")
                attributes = state.get("attributes", {})

                # Format based on entity type
                domain = entity_id.split(".")[0] if "." in entity_id else ""

                if domain == "light":
                    brightness = attributes.get("brightness", 0)
                    if state_value == "on":
                        return f"{entity_id} is on (brightness: {int(brightness/255*100)}%)"
                    return f"{entity_id} is off"

                elif domain == "climate":
                    temp = attributes.get("current_temperature")
                    target = attributes.get("temperature")
                    return f"{entity_id} is {state_value}, current: {temp}°, target: {target}°"

                elif domain == "lock":
                    return f"{entity_id} is {state_value}"

                else:
                    return f"{entity_id} is {state_value}"

            return f"Could not get state for {entity_id}"

        except Exception as e:
            logger.error(f"Failed to get entity state: {e}")
            return f"Error getting state: {str(e)}"

    async def _send_notification(self, args: Dict, context: Dict) -> str:
        """Send a notification via TTS, mobile push, or flashing lights."""
        message = args.get("message", "")
        target = args.get("target", "tts")
        room = args.get("room", context.get("room", "office"))
        flash_count = args.get("flash_count", 3)
        flash_color = args.get("flash_color")  # Optional RGB array

        results = []

        try:
            if target in ["tts", "all"]:
                # Use TTS to announce
                media_player = f"media_player.{room}"
                await self.ha_client.call_service(
                    "tts",
                    "speak",
                    {
                        "entity_id": media_player,
                        "message": message
                    }
                )
                results.append(f"Announced in {room}")

            if target in ["mobile", "all"]:
                # Send mobile push notification
                await self.ha_client.call_service(
                    "notify",
                    "mobile_app",
                    {
                        "message": message,
                        "title": "Jarvis"
                    }
                )
                results.append("Sent mobile notification")

            if target in ["flash", "all"]:
                # Flash lights in specified room
                await self._flash_lights(f"light.{room}", flash_count, flash_color)
                results.append(f"Flashed lights in {room}")

            if target == "flash_all":
                # Flash all lights in the house
                # Use light.all_lights group or call without entity_id
                await self._flash_lights("all", flash_count, flash_color)
                results.append("Flashed all house lights")

            return " and ".join(results) if results else "Notification sent"

        except Exception as e:
            logger.error(f"Failed to send notification: {e}")
            return f"Notification failed: {str(e)}"

    async def _flash_lights(self, entity_id: str, count: int = 3, color: Optional[List[int]] = None):
        """Flash lights as a visual notification."""
        # First, save current state if possible
        original_state = None
        original_brightness = None
        original_color = None

        if entity_id != "all":
            try:
                state = await self.ha_client.get_state(entity_id)
                if state:
                    original_state = state.get("state")
                    attrs = state.get("attributes", {})
                    original_brightness = attrs.get("brightness")
                    original_color = attrs.get("rgb_color")
            except Exception:
                pass  # Continue anyway

        # Flash sequence
        flash_data = {"entity_id": entity_id} if entity_id != "all" else {}

        # Set flash color if specified, otherwise use bright white
        if color:
            flash_data["rgb_color"] = color
        flash_data["brightness"] = 255

        for i in range(count):
            # Turn on (flash)
            await self.ha_client.call_service("light", "turn_on", flash_data)
            await asyncio.sleep(0.3)

            # Turn off
            off_data = {"entity_id": entity_id} if entity_id != "all" else {}
            await self.ha_client.call_service("light", "turn_off", off_data)
            await asyncio.sleep(0.3)

        # Restore original state
        if entity_id != "all" and original_state == "on":
            restore_data = {"entity_id": entity_id}
            if original_brightness:
                restore_data["brightness"] = original_brightness
            if original_color:
                restore_data["rgb_color"] = original_color
            await self.ha_client.call_service("light", "turn_on", restore_data)


def should_use_automation_agent(query: str) -> bool:
    """
    Determine if a query should be handled by the automation agent.

    Returns True for:
    - Sequences with delays (turn on, wait, turn off)
    - Scheduled HOME AUTOMATION actions (turn on lights at 6pm)
    - Recurring home automation schedules
    - Motion/sensor triggered actions

    Returns False for:
    - Simple immediate commands (turn on the light)
    - Status queries
    - Recommendations/planning (itinerary, restaurants, events)
    - Information queries
    """
    query_lower = query.lower()

    # EXCLUSIONS FIRST - these should NEVER go to automation agent
    # Simple brightness commands - immediate, not scheduled
    brightness_exclusions = [
        'lights at half', 'light at half', 'lights to half', 'lights at fifty',
        'lights to fifty', 'lights at 50', 'lights to 50',
        'brightness to', 'brightness at', 'dim to', 'set brightness',
        'all lights at half', 'all lights to half',
        # Word-based brightness (e.g., "lights at twenty", "lights to thirty")
        'lights at twenty', 'lights at thirty', 'lights at forty',
        'lights at sixty', 'lights at seventy', 'lights at eighty',
        'lights to twenty', 'lights to thirty', 'lights to forty',
    ]
    if any(p in query_lower for p in brightness_exclusions):
        return False

    # Planning, recommendations, and information queries
    exclusion_patterns = [
        # Travel/itinerary planning
        'itinerary', 'plan my day', 'plan my trip', 'plan for',
        'things to do', 'places to go', 'where should i',
        'what should i do', 'suggest', 'recommend', 'recommendation',
        # Food/dining
        'restaurant', 'where to eat', 'food', 'dining', 'breakfast',
        'lunch', 'dinner', 'brunch', 'cafe', 'coffee shop',
        # Events/entertainment (external, not home automation)
        'events happening', 'events going on', 'events near', 'events in',
        'what events', 'find events', 'search events', 'local events',
        'community events', 'concerts', 'festivals', 'shows happening',
        'activities', 'what\'s happening', 'what is happening',
        # Information queries
        'weather', 'news', 'sports score', 'stock', 'traffic',
        'how do i get to', 'directions', 'navigate',
        # General assistance
        'help me', 'can you', 'tell me about', 'what is', 'who is',
        'surprise me', 'fun things', 'good things', 'enjoy',
        # Baltimore/location specific queries (likely tourism/recommendations)
        'represent baltimore', 'baltimore has to offer', 'best of baltimore',
    ]
    if any(p in query_lower for p in exclusion_patterns):
        return False

    # Sequence indicators - requires home device context
    sequence_patterns = [
        'wait', 'then turn', 'after that turn', 'seconds then',
        'pause', 'delay',
        'on and off', 'off and on', 'flash the light', 'blink',
        'on then off', 'off then on'
    ]

    # Schedule indicators - MUST be combined with home automation action
    # Just having "saturday" or "morning" alone is NOT enough
    schedule_with_action_patterns = [
        # Time + action
        'turn on at', 'turn off at', 'lights at', 'light at',
        'at 6 pm turn', 'at 7 pm turn', 'at 8 pm turn', 'at 9 pm turn',
        'at 6 am turn', 'at 7 am turn', 'at 8 am turn',
        "o'clock turn", 'oclock turn',
        # Schedule + device
        'schedule the light', 'schedule the fan', 'schedule the',
        'every day turn', 'every night turn', 'every morning turn',
        'daily turn on', 'daily turn off',
        # Specific time device control
        'tonight turn', 'tomorrow turn', 'morning turn on', 'evening turn on',
    ]

    # Event/trigger indicators - home automation triggers
    event_patterns = [
        'when motion', 'when there is motion', 'when i walk', 'when someone',
        'motion is detected', 'motion detected', 'motion in',
        'when the door', 'when door opens', 'when door closes',
        'when i arrive', 'when i leave', 'when i get home', 'when i come home',
        'at sunset turn', 'at sunrise turn', 'before sunset turn', 'after sunset turn',
        'sunset lights', 'sunrise lights',
        'when it gets dark', 'when it gets light',
        'triggered by', 'activate when', 'turn on when', 'turn off when',
        # Numeric state triggers (temperature, humidity, etc.)
        'when the temperature', 'when temperature', 'temperature goes above', 'temperature goes below',
        'temperature drops', 'temperature rises',
        'when the humidity', 'when humidity',
        # Device triggers (buttons, switches, remotes)
        'when button', 'when the button', 'button is pressed', 'button pressed',
        'when i press the', 'when pressed',
        'when doorbell', 'doorbell rings',
        'when switch', 'switch is toggled',
        'double press', 'long press', 'hold button',
    ]

    # Time pattern triggers (recurring intervals) - home automation
    time_pattern_patterns = [
        'every hour turn', 'every minute turn',
        'every 30 minutes turn', 'every 15 minutes turn',
        'on the hour turn', 'hourly turn',
        'periodically turn', 'repeatedly turn',
        'every hour check', 'every minute check',
    ]

    # Automation management - explicit automation keywords
    automation_management_patterns = [
        'my routine', 'my automation', 'my schedule',
        'create a routine', 'create an automation', 'set up a routine',
        'delete routine', 'delete automation', 'remove routine', 'cancel routine',
        'list routines', 'list automations', 'show me my routines',
        'what routines', 'what automations',
    ]

    # Check for home device keywords - required for schedule/sequence patterns
    home_device_keywords = [
        'light', 'lamp', 'switch', 'fan', 'thermostat', 'heat', 'cool',
        'lock', 'door lock', 'garage', 'blind', 'shade', 'curtain',
        'tv', 'television', 'speaker', 'music', 'play music',
    ]
    has_home_device = any(k in query_lower for k in home_device_keywords)

    has_sequence = any(p in query_lower for p in sequence_patterns)
    has_schedule_action = any(p in query_lower for p in schedule_with_action_patterns)
    has_event = any(p in query_lower for p in event_patterns)
    has_time_pattern = any(p in query_lower for p in time_pattern_patterns)
    has_automation_mgmt = any(p in query_lower for p in automation_management_patterns)

    # Only return True if we have clear home automation intent
    if has_automation_mgmt:
        return True
    if has_event:
        return True
    if has_time_pattern:
        return True
    if has_schedule_action:
        return True
    if has_sequence and has_home_device:
        return True

    return False
