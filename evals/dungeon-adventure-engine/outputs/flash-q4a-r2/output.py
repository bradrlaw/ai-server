"""
Text-Based Adventure Game Engine
=================================
A self-contained, production-quality dungeon crawler using only the Python standard library.

Run with: python adventure.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Item
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Item:
    """Represents a collectible object within the game world.

    Attributes:
        name: Unique identifier for the item (lowercase).
        description: Flavor text shown when the player examines the item.
        on_use: Optional callback invoked when the player uses the item.
                Receives (game_state: GameState, engine: GameEngine) and returns a message string.
    """

    name: str
    description: str
    on_use: Optional[Callable[["GameState", "GameEngine"], str]] = field(default=None, repr=False)


# ─────────────────────────────────────────────────────────────────────────────
# Room
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Room:
    """Represents a location within the dungeon.

    Attributes:
        name: Human-readable room title.
        description: Base description of the room.
        dynamic_description: Optional callable that returns an alternate description
                             based on current game state (e.g., torch lit, lever pulled).
        exits: Mapping of direction strings to target room IDs.
        items: List of item IDs currently present in this room.
        locked_exits: Mapping of direction → required item name to unlock.
        locked_flags: Mapping of direction → flag name that must be True to unlock.
    """

    name: str
    description: str
    dynamic_description: Optional[Callable[["GameState"], str]] = field(default=None, repr=False)
    exits: dict[str, str] = field(default_factory=dict)
    items: list[str] = field(default_factory=list)
    locked_exits: dict[str, str] = field(default_factory=dict)  # direction -> required item
    locked_flags: dict[str, str] = field(default_factory=dict)  # direction -> required flag

    def get_description(self, state: "GameState") -> str:
        """Return the room description, using dynamic variant if available."""
        if self.dynamic_description is not None:
            return self.dynamic_description(state)
        return self.description


# ─────────────────────────────────────────────────────────────────────────────
# GameState
# ─────────────────────────────────────────────────────────────────────────────


class GameState:
    """Immutable-ish container tracking all mutable game state.

    Attributes:
        current_room: ID string of the room the player currently occupies.
        inventory: List of item name strings the player is carrying.
        visited_rooms: Set of room IDs the player has entered.
        flags: Dictionary of boolean flags representing world events.
        history: Chronological log of all messages displayed to the player.
    """

    def __init__(self, starting_room: str) -> None:
        self.current_room: str = starting_room
        self.inventory: list[str] = []
        self.visited_rooms: set[str] = {starting_room}
        self.flags: dict[str, bool] = {}
        self.history: list[str] = []

    def record(self, message: str) -> None:
        """Append a message to the game history log."""
        self.history.append(message)

    def has_item(self, item_name: str) -> bool:
        """Check whether the player is carrying a given item (case-insensitive)."""
        return any(item.lower() == item_name.lower() for item in self.inventory)

    def set_flag(self, flag: str, value: bool = True) -> None:
        """Set a world-state flag."""
        self.flags[flag] = value

    def get_flag(self, flag: str) -> bool:
        """Retrieve a world-state flag, defaulting to False."""
        return self.flags.get(flag, False)


# ─────────────────────────────────────────────────────────────────────────────
# GameEngine
# ─────────────────────────────────────────────────────────────────────────────


class GameEngine:
    """Central game loop: parses commands, mutates state, renders output.

    Attributes:
        state: The active GameState instance.
        rooms: Registry of all rooms keyed by room ID.
        items: Registry of all items keyed by item name.
        running: Whether the game loop should continue.
    """

    # ── Synonym / partial-match tables ──────────────────────────────────────

    _COMMAND_SYNONYMS: dict[str, str] = {
        "examine": "look",
        "inspect": "look",
        "grab": "take",
        "pick": "take",
        "get": "take",
        "carry": "take",
        "remove": "drop",
        "throw": "drop",
        "bag": "inventory",
        "inv": "inventory",
        "i": "inventory",
        "n": "go north",
        "s": "go south",
        "e": "go east",
        "w": "go west",
        "north": "go north",
        "south": "go south",
        "east": "go east",
        "west": "go west",
        "move": "go",
        "walk": "go",
        "quit": "quit",
        "exit": "quit",
        "q": "quit",
    }

    _KNOWN_COMMANDS: list[str] = [
        "look", "go", "move", "take", "drop", "inventory",
        "use", "help", "history", "quit",
    ]

    _DIRECTIONS: list[str] = ["north", "south", "east", "west"]

    def __init__(self, rooms: dict[str, Room], items: dict[str, Item], state: GameState) -> None:
        self.rooms: dict[str, Room] = rooms
        self.items: dict[str, Item] = items
        self.state: GameState = state
        self.running: bool = True

    # ── Public entry point ──────────────────────────────────────────────────

    def run(self) -> None:
        """Main interactive loop. Handles EOF and keyboard interrupts gracefully."""
        self._print_banner()
        self._enter_room(self.state.current_room)

        while self.running:
            try:
                raw: str = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                self._print_goodbye()
                break

            if not raw:
                continue

            output: str = self._dispatch(raw)
            self._render(output)

    # ── Command dispatch ────────────────────────────────────────────────────

    def _dispatch(self, raw_input: str) -> str:
        """Normalize input and route to the appropriate handler.

        Args:
            raw_input: The raw string typed by the player.

        Returns:
            A string representing the command result.
        """
        normalized: str = self._normalize(raw_input)

        # Split into verb and remainder
        parts: list[str] = normalized.split(" ", 1)
        verb: str = parts[0].lower()
        remainder: str = parts[1].strip().lower() if len(parts) > 1 else ""

        handlers: dict[str, Callable[[str], str]] = {
            "look": self._cmd_look,
            "go": self._cmd_go,
            "move": self._cmd_go,
            "take": self._cmd_take,
            "drop": self._cmd_drop,
            "inventory": self._cmd_inventory,
            "use": self._cmd_use,
            "help": self._cmd_help,
            "history": self._cmd_history,
            "quit": self._cmd_quit,
        }

        handler: Optional[Callable[[str], str]] = handlers.get(verb)
        if handler is not None:
            return handler(remainder)

        return f'Unknown command: "{raw_input}". Type "help" for a list of commands.'

    def _normalize(self, raw: str) -> str:
        """Resolve synonyms, single-letter shortcuts, and partial command matches."""
        lower: str = raw.lower().strip()

        # Exact synonym hit (e.g., "n" → "go north", "grab key" → "take key")
        if lower in self._COMMAND_SYNONYMS:
            return self._COMMAND_SYNONYMS[lower]

        # Check if the first word is a synonym and rebuild
        tokens: list[str] = lower.split(" ", 1)
        first: str = tokens[0]
        rest: str = tokens[1] if len(tokens) > 1 else ""

        if first in self._COMMAND_SYNONYMS:
            expanded: str = self._COMMAND_SYNONYMS[first]
            return f"{expanded} {rest}".strip() if rest else expanded

        # Partial match: "inv" → "inventory", "tak" → "take"
        for cmd in self._KNOWN_COMMANDS:
            if cmd.startswith(first):
                return f"{cmd} {rest}".strip() if rest else cmd

        return lower

    # ── Command handlers ────────────────────────────────────────────────────

    def _cmd_look(self, arg: str) -> str:
        """Handle 'look' and 'look at <item>' commands."""
        if arg:
            # Strip optional "at" prefix
            target: str = arg.removeprefix("at ").strip()
            return self._look_at_item(target)

        return self._look_room()

    def _look_room(self) -> str:
        """Describe the current room and its visible items."""
        room: Room = self.rooms[self.state.current_room]
        lines: list[str] = [room.get_description(self.state)]

        visible_items: list[str] = [
            item_name for item_name in room.items if item_name in self.items
        ]
        if visible_items:
            lines.append("\nYou notice: " + ", ".join(visible_items) + ".")
        else:
            lines.append("\nThe room appears empty.")

        # Show available exits
        available: list[str] = self._available_exits(room)
        lines.append("\nExits: " + (", ".join(available) if available else "none"))

        return "\n".join(lines)

    def _look_at_item(self, target: str) -> str:
        """Describe a specific item in the current room or inventory."""
        room: Room = self.rooms[self.state.current_room]

        # Search room items
        for item_name in room.items:
            if self._fuzzy_match(item_name, target):
                return self.items[item_name].description

        # Search inventory
        for item_name in self.state.inventory:
            if self._fuzzy_match(item_name, target):
                return self.items[item_name].description

        return f'I don\'t see "{target}" here.'

    def _cmd_go(self, direction: str) -> str:
        """Handle movement commands including locked-door logic."""
        if not direction:
            return "Go where? Specify a direction: north, south, east, or west."

        # Normalize direction (partial: "nor" → "north")
        resolved: Optional[str] = self._resolve_direction(direction)
        if resolved is None:
            return f'"{direction}" is not a valid direction. Try north, south, east, or west.'

        room: Room = self.rooms[self.state.current_room]

        # Check if exit exists
        if resolved not in room.exits:
            return f"There is no exit to the {resolved}."

        # Check flag-based locks (e.g., lever must be pulled)
        if resolved in room.locked_flags:
            flag_name: str = room.locked_flags[resolved]
            if not self.state.get_flag(flag_name):
                return (
                    f"The way {resolved} is blocked. "
                    "You'll need to find a way to open it."
                )

        # Check item-based locks (e.g., rusted key)
        if resolved in room.locked_exits:
            required_item: str = room.locked_exits[resolved]
            if not self.state.has_item(required_item):
                return (
                    f"The door to the {resolved} is locked. "
                    f"You need a {required_item} to open it."
                )
            # Auto-use: consume the key
            self.state.inventory.remove(required_item)
            self.state.record(f"Used the {required_item} to unlock the door to the {resolved}.")

        # Move
        target_id: str = room.exits[resolved]
        self.state.current_room = target_id
        self.state.visited_rooms.add(target_id)

        return self._enter_room(target_id)

    def _cmd_take(self, target: str) -> str:
        """Pick up an item from the current room."""
        if not target:
            return "Take what?"

        room: Room = self.rooms[self.state.current_room]

        for item_name in room.items:
            if self._fuzzy_match(item_name, target):
                room.items.remove(item_name)
                self.state.inventory.append(item_name)
                self.state.record(f"Picked up the {item_name}.")

                # Special side-effect: torch taken from Armory
                if item_name == "torch":
                    self.state.set_flag("torch_taken")

                return f"You pick up the {item_name}."

        return f"There is no "{target}" here to take."

    def _cmd_drop(self, target: str) -> str:
        """Drop an item from inventory into the current room."""
        if not target:
            return "Drop what?"

        room: Room = self.rooms[self.state.current_room]

        for item_name in self.state.inventory:
            if self._fuzzy_match(item_name, target):
                self.state.inventory.remove(item_name)
                room.items.append(item_name)
                self.state.record(f"Dropped the {item_name}.")
                return f"You drop the {item_name}."

        return f"You aren't carrying a "{target}"."

    def _cmd_inventory(self, _arg: str) -> str:
        """List the player's inventory."""
        if not self.state.inventory:
            return "Your inventory is empty."

        lines: list[str] = ["You are carrying:"]
        for item_name in self.state.inventory:
            lines.append(f"  • {item_name}")
        return "\n".join(lines)

    def _cmd_use(self, target: str) -> str:
        """Activate an item's on_use callback."""
        if not target:
            return "Use what?"

        # Check inventory first, then room
        for item_name in self.state.inventory:
            if self._fuzzy_match(item_name, target):
                item: Item = self.items[item_name]
                if item.on_use is None:
                    return f"The {item_name} doesn't seem to do anything when used."
                result: str = item.on_use(self.state, self)
                self.state.record(result)
                return result

        # Check current room
        room: Room = self.rooms[self.state.current_room]
        for item_name in room.items:
            if self._fuzzy_match(item_name, target):
                item = self.items[item_name]
                if item.on_use is None:
                    return f"The {item_name} doesn't seem to do anything when used."
                result = item.on_use(self.state, self)
                self.state.record(result)
                return result

        return f'I don't see a "{target}" to use.'

    def _cmd_help(self, _arg: str) -> str:
        """Print the command reference."""
        return (
            "╔══════════════════════════════════════════╗\n"
            "║           AVAILABLE COMMANDS             ║\n"
            "╠══════════════════════════════════════════╣\n"
            "║  look              Examine the room      ║\n"
            "║  look at <item>    Examine an item       ║\n"
            "║  go <dir>          Move (n/s/e/w)        ║\n"
            "║  take <item>       Pick up an item       ║\n"
            "║  drop <item>       Drop an item          ║\n"
            "║  inventory / i     List carried items    ║\n"
            "║  use <item>        Activate an item      ║\n"
            "║  history           Last 10 events        ║\n"
            "║  help              This screen           ║\n"
            "║  quit              Exit the game         ║\n"
            "╚══════════════════════════════════════════╝"
        )

    def _cmd_history(self, _arg: str) -> str:
        """Display the last 10 recorded events."""
        recent: list[str] = self.state.history[-10:]
        if not recent:
            return "No history yet."
        lines: list[str] = ["── Recent Events ──"]
        for entry in recent:
            lines.append(f"  • {entry}")
        return "\n".join(lines)

    def _cmd_quit(self, _arg: str) -> str:
        """End the game session."""
        self.running = False
        return "Thanks for playing. Farewell, adventurer!"

    # ── Helper methods ──────────────────────────────────────────────────────

    def _enter_room(self, room_id: str) -> str:
        """Trigger room-entry logic and return the description."""
        room: Room = self.rooms[room_id]
        self.state.record(f"Entered the {room.name}.")
        return self._look_room()

    def _available_exits(self, room: Room) -> list[str]:
        """Return a list of currently passable exit directions."""
        available: list[str] = []
        for direction in room.exits:
            if direction in room.locked_flags:
                if not self.state.get_flag(room.locked_flags[direction]):
                    continue
            if direction in room.locked_exits:
                if not self.state.has_item(room.locked_exits[direction]):
                    continue
            available.append(direction)
        return available

    def _resolve_direction(self, raw: str) -> Optional[str]:
        """Match a partial direction string to a full direction name."""
        for d in self._DIRECTIONS:
            if d.startswith(raw):
                return d
        return None

    @staticmethod
    def _fuzzy_match(item_name: str, query: str) -> bool:
        """Check if query matches item_name (exact, substring, or word match)."""
        item_lower: str = item_name.lower()
        query_lower: str = query.lower()

        if item_lower == query_lower:
            return True
        if query_lower in item_lower:
            return True
        # Match last word: "key" matches "rusted key"
        if item_lower.split()[-1] == query_lower:
            return True
        return False

    # ── Output rendering ────────────────────────────────────────────────────

    @staticmethod
    def _render(text: str) -> None:
        """Print a separator followed by the result text."""
        separator: str = "─" * 50
        print(f"\n{separator}")
        print(text)
        print(separator)

    @staticmethod
    def _print_banner() -> None:
        """Display the ASCII art title banner."""
        banner: str = (
            "\n"
            "  ╔═══════════════════════════════════════════════╗\n"
            "  ║                                             ║\n"
            "  ║   ██████╗  ██████╗ ███╗   ██╗██████╗        ║\n"
            "  ║   ██╔══██╗██╔═══██╗████╗  ██║██╔══██╗       ║\n"
            "  ║   ██████╔╝██║   ██║██╔██╗ ██║██║  ██║       ║\n"
            "  ║   ██╔══██╗██║   ██║██║╚██╗██║██║  ██║       ║\n"
            "  ║   ██║  ██║╚██████╔╝██║ ╚████║██████╔╝       ║\n"
            "  ║   ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝╚═════╝        ║\n"
            "  ║                                             ║\n"
            "  ║        ~ The Forgotten Dungeon ~            ║\n"
            "  ║                                             ║\n"
            "  ║   Type 'help' for commands, 'quit' to end   ║\n"
            "  ║                                             ║\n"
            "  ╚═══════════════════════════════════════════════╝\n"
        )
        print(banner)

    @staticmethod
    def _print_goodbye() -> None:
        """Print a farewell message on unexpected exit."""
        print("\n\n  ╭─────────────────────────────────────╮")
        print("  │  The dungeon fades into darkness…  │")
        print("  │     Thank you for playing!         │")
        print("  ╰─────────────────────────────────────╯\n")


# ─────────────────────────────────────────────────────────────────────────────
# World Builder
# ─────────────────────────────────────────────────────────────────────────────


def build_world() -> tuple[dict[str, Room], dict[str, Item], GameState]:
    """Construct the dungeon world: rooms, items, and initial game state.

    Returns:
        A tuple of (rooms, items, initial_game_state).
    """

    # ── Item callbacks ──────────────────────────────────────────────────────

    def lever_on_use(state: GameState, engine: GameEngine) -> str:
        """Pull the lever: unlock the secret passage west from the Library."""
        if state.get_flag("lever_pulled"):
            return "The lever is already in the pulled position."
        state.set_flag("lever_pulled")
        return (
            "You pull the heavy iron lever with a grinding groan.\n"
            "The dusty bookshelf shudders and slides aside, revealing a\n"
            "narrow passage leading WEST. Cool air rushes from beyond."
        )

    def scroll_on_use(state: GameState, engine: GameEngine) -> str:
        """Read the ancient scroll: reveal the secret ending."""
        if state.get_flag("won"):
            return "You've already read the scroll. Its secrets are etched in your mind."
        state.set_flag("won")
        return (
            "You unroll the ancient scroll with trembling hands.\n"
            "The ink glows faintly as words materialize:\n\n"
            '  "He who finds this scroll has pierced the veil of the dungeon.\n'
            '   The Forgotten King smiles upon you. His treasure was never gold—\n'
            '   it was the knowledge that the labyrinth had an end.\n\n'
            '   You are FREE, adventurer. The walls dissolve to starlight."\n\n'
            "★ ═══════════════════════════════════════════════ ★\n"
            "★   🏆  S E C R E T   E N D I N G   A C H I E V E D  🏆   ★\n"
            "★ ═══════════════════════════════════════════════ ★\n"
            "★   Congratulations! You have escaped the dungeon.  ★\n"
            "★ ═══════════════════════════════════════════════ ★"
        )

    # ── Dynamic room descriptions ───────────────────────────────────────────

    def library_description(state: GameState) -> str:
        """Library description changes after lever is pulled."""
        base: str = (
            "A vast library stretches before you. Towering shelves groan\n"
            "under the weight of forgotten tomes. Dust motes dance in the\n"
            "pale light filtering through cracked windows. A massive\n"
            "bookshelf blocks the southern wall, immovable."
        )
        if state.get_flag("lever_pulled"):
            base += (
                "\n\nThe great bookshelf has been shifted aside. A dark\n"
                "passage yawns open to the WEST, exhaling ancient air."
            )
        else:
            base += (
                "\n\nA large iron LEVER protrudes from the wall beside the bookshelf."
            )
        return base

    def armory_description(state: GameState) -> str:
        """Armory description changes when torch is taken."""
        base: str = (
            "Rusted weapon racks line the walls of this cramped armory.\n"
            "Empty scabbards and cracked shields hang like shed skins.\n"
            "A heavy iron door with a keyhole bars the northern passage."
        )
        if state.get_flag("torch_taken"):
            base += (
                "\n\nYour torch casts flickering light across the room,\n"
                "making the shadows dance like restless spirits."
            )
        return base

    # ── Items ───────────────────────────────────────────────────────────────

    items: dict[str, Item] = {
        "rusted key": Item(
            name="rusted key",
            description=(
                "A small, corroded iron key. Its teeth are worn but intact.\n"
                "It smells faintly of copper and old blood."
            ),
        ),
        "lever": Item(
            name="lever",
            description="A heavy iron lever mounted on the wall. It begs to be pulled.",
            on_use=lever_on_use,
        ),
        "torch": Item(
            name="torch",
            description=(
                "A sturdy wooden torch with a charred tip. It still smells\n"
                "of pine resin. Somehow, it's still warm to the touch."
            ),
        ),
        "golden coin": Item(
            name="golden coin",
            description=(
                "A gleaming gold coin stamped with the face of a king who\n"
                "clearly lost a fight with a sculptor. On the back, an\n"
                'inscription reads: "Spend me wisely. Or don\'t. I\'m a coin."'
            ),
        ),
        "ancient scroll": Item(
            name="ancient scroll",
            description=(
                "A brittle scroll sealed with black wax. The parchment\n"
                "crackles as you handle it. Strange symbols shift at the\n"
                "corner of your vision."
            ),
            on_use=scroll_on_use,
        ),
    }

    # ── Rooms ───────────────────────────────────────────────────────────────

    rooms: dict[str, Room] = {
        "entrance": Room(
            name="Entrance Hall",
            description=(
                "You stand in a crumbling entrance hall. Cobwebs drape\n"
                "from vaulted ceilings lost in shadow. The air is thick\n"
                "with the scent of mildew and old stone.\n\n"
                "A rusted key lies on the floor, half-buried in dirt.\n"
                "Passages lead NORTH, EAST, and SOUTH."
            ),
            exits={"north": "library", "east": "armory", "south": "garden"},
            items=["rusted key"],
        ),
        "library": Room(
            name="Library",
            description="",  # Handled dynamically
            dynamic_description=library_description,
            exits={"south": "entrance", "west": "secret_chamber"},
            items=["lever"],
            locked_flags={"west": "lever_pulled"},
        ),
        "armory": Room(
            name="Armory",
            description="",  # Handled dynamically
            dynamic_description=armory_description,
            exits={"west": "entrance", "north": "treasure_room"},
            items=["torch"],
            locked_exits={"north": "rusted key"},
        ),
        "treasure_room": Room(
            name="Treasure Room",
            description=(
                "A small, opulent chamber. Gilded sconces line the walls,\n"
                "though their flames long died. Upon a velvet pedestal\n"
                "rests a single golden coin.\n\n"
                "An inscription is carved into the far wall:\n"
                '  "What is lost to greed is found by wisdom."\n\n'
                "The only exit leads SOUTH."
            ),
            exits={"south": "armory"},
            items=["golden coin"],
        ),
        "secret_chamber": Room(
            name="Secret Chamber",
            description=(
                "A hidden chamber beyond the walls of the world. Bioluminescent\n"
                "moss paints everything in ghostly blue-green. At the center,\n"
                "on a stone altar, rests a single ancient scroll.\n\n"
                "The passage back lies to the EAST."
            ),
            exits={"east": "library"},
            items=["ancient scroll"],
        ),
        "garden": Room(
            name="Garden",
            description=(
                "A peaceful clearing bathed in soft golden light. Birds sing\n"
                "in unseen trees. A stone bench sits beside a trickling fountain.\n"
                "Carved into the fountain's basin are the words:\n\n"
                '  "You\'ve found peace."\n\n'
                "The only way out is NORTH."
            ),
            exits={"north": "entrance"},
            items=[],
        ),
    }

    # ── Initial state ───────────────────────────────────────────────────────

    state: GameState = GameState(starting_room="entrance")

    return rooms, items, state


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    """Instantiate the dungeon world and run the game loop."""
    rooms, items, state = build_world()
    engine: GameEngine = GameEngine(rooms=rooms, items=items, state=state)
    engine.run()


if __name__ == "__main__":
    main()
