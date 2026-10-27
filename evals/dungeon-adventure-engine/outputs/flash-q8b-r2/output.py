"""
Text-Based Adventure Game Engine
=================================
A self-contained, production-quality text adventure engine with a small dungeon world.
No external dependencies — standard library only.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Item:
    """Represents a collectible or usable object in the game world.

    Attributes:
        name: The item's display name (lowercase, used for matching).
        description: Flavor text shown when the player examines the item.
        on_use: Optional callback invoked when the player uses the item.
                Receives the current GameState and returns a message string.
    """

    name: str
    description: str
    on_use: Optional[Callable[["GameState"], str]] = None


@dataclass
class Room:
    """Represents a location in the game world.

    Attributes:
        name: Human-readable room name.
        description: Base description text.
        exits: Mapping of direction strings to target room IDs.
        items: List of item IDs currently present in the room.
        locked_exits: Mapping of direction strings to the item ID required
                      to unlock that passage.
        dynamic_descriptions: Mapping of flag names to alternative description
                             strings shown when that flag is True.
    """

    name: str
    description: str
    exits: dict[str, str] = field(default_factory=dict)
    items: list[str] = field(default_factory=list)
    locked_exits: dict[str, str] = field(default_factory=dict)
    dynamic_descriptions: dict[str, str] = field(default_factory=dict)


@dataclass
class GameState:
    """Encapsulates all mutable game state. No global mutable state is used.

    Attributes:
        current_room: ID of the room the player currently occupies.
        inventory: Ordered list of item IDs the player carries.
        visited_rooms: Set of room IDs the player has entered.
        flags: Boolean flags representing world state changes.
        history: Append-only log of all messages/events shown to the player.
    """

    current_room: str
    inventory: list[str] = field(default_factory=list)
    visited_rooms: set[str] = field(default_factory=set)
    flags: dict[str, bool] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)

    def add_history(self, message: str) -> None:
        """Append a message to the game history log."""
        self.history.append(message)

    def has_item(self, item_id: str) -> bool:
        """Return True if the player is carrying the specified item."""
        return item_id in self.inventory

    def set_flag(self, flag: str) -> None:
        """Set a world-state flag to True."""
        self.flags[flag] = True

    def get_flag(self, flag: str) -> bool:
        """Return the boolean value of a flag (False if unset)."""
        return self.flags.get(flag, False)


# ─────────────────────────────────────────────────────────────────────────────
# Game Engine
# ─────────────────────────────────────────────────────────────────────────────


class GameEngine:
    """Core engine that drives the adventure: parsing, routing, rendering.

    Attributes:
        rooms: Registry of all Room objects keyed by room ID.
        items: Registry of all Item objects keyed by item ID.
        state: The current GameState instance.
        running: Whether the main game loop should continue.
    """

    # Synonyms mapping for command resolution
    _SYNONYMS: dict[str, str] = {
        "examine": "look",
        "inspect": "look",
        "grab": "take",
        "get": "take",
        "pick": "take",
        "pick up": "take",
        "move": "go",
        "walk": "go",
        "travel": "go",
        "i": "inventory",
        "inv": "inventory",
        "h": "help",
        "?": "help",
        "q": "quit",
        "exit": "quit",
        "leave": "quit",
        "check": "history",
        "log": "history",
    }

    # Valid directions for movement
    _DIRECTIONS: set[str] = {"north", "south", "east", "west"}

    # Abbreviations for directions
    _DIR_ABBREVS: dict[str, str] = {
        "n": "north",
        "s": "south",
        "e": "east",
        "w": "west",
        "ne": "north",
        "nw": "north",
        "se": "south",
        "sw": "south",
    }

    def __init__(self, rooms: dict[str, Room], items: dict[str, Item]) -> None:
        """Initialize the engine with a room and item registry.

        Args:
            rooms: Mapping of room IDs to Room instances.
            items: Mapping of item IDs to Item instances.
        """
        self.rooms: dict[str, Room] = rooms
        self.items: dict[str, Item] = items
        self.state: GameState = GameState(current_room="entrance_hall")
        self.running: bool = True

    # ── Main Loop ─────────────────────────────────────────────────────────

    def run(self) -> None:
        """Run the interactive game loop until the player quits or an error occurs."""
        # Mark starting room as visited
        self.state.visited_rooms.add(self.state.current_room)
        self._print_room()

        while self.running:
            try:
                raw_input: str = input("\n> ")
            except (EOFError, KeyboardInterrupt):
                print("\n\n  Farewell, adventurer. The dungeon remembers your name.")
                break

            raw_input = raw_input.strip()
            if not raw_input:
                continue

            self._process_command(raw_input)

    # ── Command Processing ────────────────────────────────────────────────

    def _process_command(self, raw: str) -> None:
        """Parse raw player input and dispatch to the appropriate handler."""
        tokens: list[str] = raw.lower().split()
        command: str = tokens[0]
        args: list[str] = tokens[1:]

        # Resolve synonyms
        command = self._SYNONYMS.get(command, command)

        # Check if the raw token is a direction abbreviation
        if command in self._DIR_ABBREVS:
            self._cmd_go(self._DIR_ABBREVS[command])
            return

        # Route to command handlers
        match command:
            case "look":
                self._cmd_look(args)
            case "go":
                self._cmd_go(args)
            case "take":
                self._cmd_take(args)
            case "drop":
                self._cmd_drop(args)
            case "inventory":
                self._cmd_inventory()
            case "use":
                self._cmd_use(args)
            case "help":
                self._cmd_help()
            case "history":
                self._cmd_history()
            case "quit":
                self._cmd_quit()
            case _:
                self._output(f"I don't understand '{raw}'. Type 'help' for commands.")

    # ── Command Handlers ──────────────────────────────────────────────────

    def _cmd_look(self, args: list[str]) -> None:
        """Handle 'look' and 'look at <item>' commands."""
        if not args:
            self._print_room()
            return

        # Strip leading "at" if present (e.g., "look at torch")
        if args[0] == "at":
            args = args[1:]

        item_id: Optional[str] = self._resolve_item(args)

        if item_id is None:
            self._output(f"I don't see '{' '.join(args)}' here.")
            return

        item: Item = self.items[item_id]
        self._output(f"{item.name.capitalize()}: {item.description}")

    def _cmd_go(self, args: list[str]) -> None:
        """Handle movement commands (go north, n, etc.)."""
        if not args:
            self._output("Go where? Specify a direction: north, south, east, west.")
            return

        direction: str = args[0]
        # Resolve direction abbreviations
        direction = self._DIR_ABBREVS.get(direction, direction)

        if direction not in self._DIRECTIONS:
            self._output(f"'{direction}' is not a valid direction.")
            return

        room: Room = self.rooms[self.state.current_room]

        # Check if exit exists at all
        if direction not in room.exits:
            self._output(f"There is no passage to the {direction}.")
            return

        # Check if exit is locked
        if direction in room.locked_exits:
            required_item_id: str = room.locked_exits[direction]
            if not self.state.has_item(required_item_id):
                required_item: Item = self.items[required_item_id]
                self._output(
                    f"The passage to the {direction} is locked. "
                    f"You need a {required_item.name} to open it."
                )
                return
            # Auto-use the key: consume it and unlock permanently
            self.state.inventory.remove(required_item_id)
            del room.locked_exits[direction]
            self._output(
                f"You use the {required_item.name} to unlock the passage to the {direction}."
            )

        # Move the player
        target_id: str = room.exits[direction]
        self.state.current_room = target_id
        self.state.visited_rooms.add(target_id)
        self._output(f"You travel {direction}.")
        self._print_room()

    def _cmd_take(self, args: list[str]) -> None:
        """Handle 'take <item>' command."""
        item_id: Optional[str] = self._resolve_item(args)

        if item_id is None:
            self._output(f"I don't see '{' '.join(args)}' here.")
            return

        room: Room = self.rooms[self.state.current_room]
        if item_id not in room.items:
            self._output(f"The {self.items[item_id].name} is not in this room.")
            return

        room.items.remove(item_id)
        self.state.inventory.append(item_id)
        self._output(f"You pick up the {self.items[item_id].name}.")

    def _cmd_drop(self, args: list[str]) -> None:
        """Handle 'drop <item>' command."""
        item_id: Optional[str] = self._resolve_item(args)

        if item_id is None:
            self._output(f"You don't have '{' '.join(args)}'.")
            return

        if item_id not in self.state.inventory:
            self._output(f"You're not carrying the {self.items[item_id].name}.")
            return

        self.state.inventory.remove(item_id)
        room: Room = self.rooms[self.state.current_room]
        room.items.append(item_id)
        self._output(f"You drop the {self.items[item_id].name}.")

    def _cmd_inventory(self) -> None:
        """Handle 'inventory' command."""
        if not self.state.inventory:
            self._output("Your inventory is empty.")
            return

        lines: list[str] = ["You are carrying:"]
        for item_id in self.state.inventory:
            lines.append(f"  • {self.items[item_id].name}")
        self._output("\n".join(lines))

    def _cmd_use(self, args: list[str]) -> None:
        """Handle 'use <item>' command."""
        item_id: Optional[str] = self._resolve_item(args)

        if item_id is None:
            self._output(f"You don't have '{' '.join(args)}'.")
            return

        if item_id not in self.state.inventory:
            self._output(f"You're not carrying the {self.items[item_id].name}.")
            return

        item: Item = self.items[item_id]

        if item.on_use is None:
            self._output(f"The {item.name} can't be used for anything.")
            return

        result: str = item.on_use(self.state)
        self._output(result)

        # Check for victory condition
        if self.state.get_flag("won"):
            self._print_victory()

    def _cmd_help(self) -> None:
        """Handle 'help' command — print available commands."""
        help_text: str = (
            "Available Commands:\n"
            "  look [at <item>]   — Examine room or item\n"
            "  go <direction>     — Move (north/south/east/west)\n"
            "  take <item>        — Pick up an item\n"
            "  drop <item>        — Drop an item\n"
            "  inventory / i      — Show carried items\n"
            "  use <item>         — Use an item\n"
            "  help / h           — Show this help\n"
            "  history            — Last 10 events\n"
            "  quit / q           — Exit the game\n"
            "\n"
            "Shortcuts: n, s, e, w for directions.\n"
            "Synonyms: examine=look, grab=take, move=go."
        )
        self._output(help_text)

    def _cmd_history(self) -> None:
        """Handle 'history' command — show last 10 events."""
        recent: list[str] = self.state.history[-10:]
        if not recent:
            self._output("No history yet.")
            return

        lines: list[str] = ["── Recent Events ──"]
        for entry in recent:
            lines.append(f"  {entry}")
        self._output("\n".join(lines))

    def _cmd_quit(self) -> None:
        """Handle 'quit' command — exit gracefully."""
        self._output("Farewell, adventurer. The dungeon remembers your name.")
        self.running = False

    # ── Helper Methods ────────────────────────────────────────────────────

    def _resolve_item(self, args: list[str]) -> Optional[str]:
        """Resolve a list of tokens to a known item ID via partial matching.

        Args:
            args: Tokens following the command verb.

        Returns:
            The matching item ID, or None if no match is found.
        """
        if not args:
            return None

        query: str = " ".join(args).strip()

        # Exact match first
        if query in self.items:
            return query

        # Partial / prefix match
        matches: list[str] = [
            item_id for item_id in self.items if item_id.startswith(query)
        ]

        if len(matches) == 1:
            return matches[0]

        if len(matches) > 1:
            # Prefer the shortest match
            matches.sort(key=len)
            return matches[0]

        # Also try matching by item display name
        name_matches: list[str] = [
            item_id
            for item_id, item in self.items.items()
            if item.name.startswith(query)
        ]
        if name_matches:
            name_matches.sort(key=len)
            return name_matches[0]

        return None

    def _get_room_description(self, room: Room) -> str:
        """Build the effective room description, accounting for dynamic flags.

        Args:
            room: The Room to describe.

        Returns:
            The description string, potentially modified by active flags.
        """
        # Check for dynamic descriptions keyed by flag
        if room.dynamic_descriptions:
            for flag_name, alt_desc in room.dynamic_descriptions.items():
                if self.state.get_flag(flag_name):
                    return alt_desc
        return room.description

    def _print_room(self) -> None:
        """Print the current room's description and visible items."""
        room: Room = self.rooms[self.state.current_room]
        desc: str = self._get_room_description(room)

        # Build item list
        visible_items: list[str] = [
            self.items[item_id].name for item_id in room.items
        ]

        output_lines: list[str] = []
        output_lines.append(f"── {room.name} ──")
        output_lines.append(desc)

        if visible_items:
            output_lines.append(f"\nYou see: {', '.join(visible_items)}.")
        else:
            output_lines.append("\nThere is nothing to pick up here.")

        # Show available exits
        exits_str: str = ", ".join(room.exits.keys())
        locked_str: str = ""
        if room.locked_exits:
            locked_dirs: list[str] = list(room.locked_exits.keys())
            locked_str = f" (locked: {', '.join(locked_dirs)})"
        output_lines.append(f"Exits: {exits_str}{locked_str}")

        self._output("\n".join(output_lines))

    def _print_victory(self) -> None:
        """Print the grand victory banner when the secret ending is triggered."""
        victory: str = (
            "\n"
            "╔══════════════════════════════════════════════════════════╗\n"
            "║                                                        ║\n"
            "║   ★  YOU HAVE UNCOVERED THE SECRET ENDING!  ★         ║\n"
            "║                                                        ║\n"
            "║   The ancient scroll's final words echo through the    ║\n"
            "║   chamber: \"You are the one the dungeon awaited.\"     ║\n"
            "║                                                        ║\n"
            "║   Congratulations, true adventurer. Your legend is     ║\n"
            "║   now etched in stone forever.                         ║\n"
            "║                                                        ║\n"
            "╚══════════════════════════════════════════════════════════╝"
        )
        self._output(victory)

    def _output(self, message: str) -> None:
        """Print a formatted message block with separator and log it.

        Args:
            message: The text to display.
        """
        separator: str = "─" * 60
        print(f"\n{separator}")
        print(message)
        self.state.add_history(message.split("\n")[0] if message else "")


# ─────────────────────────────────────────────────────────────────────────────
# World Builder
# ─────────────────────────────────────────────────────────────────────────────


def build_dungeon() -> tuple[dict[str, Room], dict[str, Item]]:
    """Construct and return the mini-dungeon world.

    Returns:
        A tuple of (rooms_dict, items_dict) ready to pass to GameEngine.
    """

    # ── Items ─────────────────────────────────────────────────────────────

    def use_lever(state: GameState) -> str:
        """Pull the lever in the Library, revealing the secret passage."""
        if state.get_flag("lever_pulled"):
            return "The lever is already in its new position. The bookshelf stands aside."
        state.set_flag("lever_pulled")
        # Unlock the west exit of the library
        return (
            "You pull the lever with a grinding effort. Deep within the walls, "
            "gears turn. The dusty bookshelf slides aside, revealing a hidden "
            "passage to the west!"
        )

    def use_scroll(state: GameState) -> str:
        """Read the ancient scroll to trigger the secret ending."""
        if state.get_flag("won"):
            return "You have already read the scroll. Its words are burned into your memory."
        state.set_flag("won")
        return (
            "You unroll the ancient scroll. The ink still glistens after millennia.\n\n"
            "The final line reads:\n\n"
            '   "Only those who seek the hidden path, who look beyond the obvious,\n'
            '    shall find what was always meant to be found. You are that seeker.\n'
            '    The dungeon is yours, and you are the dungeon\'s."\n\n'
            "The chamber hums with golden light..."
        )

    def use_torch(state: GameState) -> str:
        """Light the torch — sets a flag that changes the Armory description."""
        state.set_flag("torch_lit")
        return "You strike the torch to life. Warm, flickering light pushes back the shadows."

    items: dict[str, Item] = {
        "rusted_key": Item(
            name="rusted key",
            description=(
                "A small iron key, orange with rust. It looks old, but the teeth "
                "are still sharp enough to work a lock."
            ),
        ),
        "lever": Item(
            name="lever",
            description=(
                "A heavy iron lever mounted to the wall, half-hidden behind "
                "the bookshelf. It feels important."
            ),
            on_use=use_lever,
        ),
        "torch": Item(
            name="torch",
            description=(
                "A pitch-soaked torch, dry and ready. A single spark would "
                "bring it to life."
            ),
            on_use=use_torch,
        ),
        "golden_coin": Item(
            name="golden coin",
            description=(
                "A gleaming gold coin stamped with a face that looks suspiciously "
                "like your own. On the reverse: 'TIP THE DUNGEON MASTER.' "
                "You pocket it anyway. You've earned it."
            ),
        ),
        "ancient_scroll": Item(
            name="ancient scroll",
            description=(
                "A scroll of impossibly preserved vellum, sealed with a wax "
                "sigil of an eye. It pulses faintly with warmth."
            ),
            on_use=use_scroll,
        ),
    }

    # ── Rooms ─────────────────────────────────────────────────────────────

    rooms: dict[str, Room] = {
        "entrance_hall": Room(
            name="Entrance Hall",
            description=(
                "A vast stone hall stretches before you. Torches in iron sconces "
                "line the walls, their flames long dead. Dust motes drift in the "
                "grey light filtering through a cracked ceiling. The air smells "
                "of damp earth and forgotten centuries."
            ),
            exits={"north": "library", "east": "armory", "south": "garden"},
            items=["rusted_key"],
        ),
        "library": Room(
            name="Library",
            description=(
                "Towering bookshelves groan under the weight of rotting tomes. "
                "A thick layer of dust coats every surface. One shelf stands "
                "slightly askew, as if concealing something behind it. "
                "A heavy iron lever protrudes from the wall."
            ),
            dynamic_descriptions={
                "lever_pulled": (
                    "Towering bookshelves line the walls, but one section has "
                    "slid aside, revealing a narrow passage to the west. Dust "
                    "still swirls in its wake. The lever sits in its new position, "
                    "satisfied."
                ),
            },
            exits={"south": "entrance_hall", "west": "secret_chamber"},
            items=["lever"],
            locked_exits={"west": "__lever_flag__"},  # Special sentinel
        ),
        "armory": Room(
            name="Armory",
            description=(
                "Rusted weapon racks line the walls, most still holding corroded "
                "blades and dented shields. A single unlit torch leans against "
                "a barrel. To the north, a heavy iron door stands shut, "
                "a keyhole glinting in the gloom."
            ),
            dynamic_descriptions={
                "torch_lit": (
                    "Rusted weapon racks line the walls, now bathed in warm, "
                    "flickering light from your torch. The corroded blades "
                    "almost look new in this glow. To the north, a heavy iron "
                    "door stands shut, a keyhole glinting."
                ),
            },
            exits={"west": "entrance_hall", "north": "treasure_room"},
            items=["torch"],
            locked_exits={"north": "rusted_key"},
        ),
        "treasure_room": Room(
            name="Treasure Room",
            description=(
                "A circular vault, its domed ceiling painted with faded stars. "
                "On a stone pedestal rests a single golden coin — the dungeon's "
                "entire wealth. An inscription is carved into the wall:\n\n"
                '   "THE ANSWER LIES WHERE NO DOOR WAS MEANT TO BE FOUND."'
            ),
            exits={"south": "armory"},
            items=["golden_coin"],
        ),
        "secret_chamber": Room(
            name="Secret Chamber",
            description=(
                "A hidden room, untouched by time. Bioluminescent moss casts a "
                "soft blue glow. At its center, on a marble plinth, rests a "
                "single scroll, sealed with a wax sigil. The air hums with "
                "ancient power."
            ),
            exits={"east": "library"},
            items=["ancient_scroll"],
        ),
        "garden": Room(
            name="Garden",
            description=(
                "A peaceful clearing, open to the sky. Wildflowers grow between "
                "cracked flagstones. A gentle breeze carries the scent of rain. "
                "A stone bench sits beneath an ancient oak. Carved into the "
                "bench, in simple letters:\n\n"
                '   "You\'ve found peace."\n\n'
                "For a moment, the dungeon feels far away."
            ),
            exits={"north": "entrance_hall"},
            items=[],
        ),
    }

    return rooms, items


# ─────────────────────────────────────────────────────────────────────────────
# Custom Engine Subclass for Flag-Based Locks
# ─────────────────────────────────────────────────────────────────────────────


class DungeonEngine(GameEngine):
    """Extended engine that supports flag-based locked exits (e.g., lever)."""

    def _cmd_go(self, args: list[str]) -> None:
        """Override to handle flag-based locks (lever) in addition to item locks."""
        if not args:
            self._output("Go where? Specify a direction: north, south, east, west.")
            return

        direction: str = args[0]
        direction = self._DIR_ABBREVS.get(direction, direction)

        if direction not in self._DIRECTIONS:
            self._output(f"'{direction}' is not a valid direction.")
            return

        room: Room = self.rooms[self.state.current_room]

        if direction not in room.exits:
            self._output(f"There is no passage to the {direction}.")
            return

        # Check locked exits
        if direction in room.locked_exits:
            required: str = room.locked_exits[direction]

            # Flag-based lock (e.g., lever)
            if required.startswith("__") and required.endswith("__"):
                flag_name: str = required.strip("_")
                if not self.state.get_flag(flag_name):
                    self._output(
                        f"The passage to the {direction} is sealed. "
                        f"Something in this room might reveal the way."
                    )
                    return
                # Unlock permanently
                del room.locked_exits[direction]
                self._output(
                    f"The way to the {direction} is now open."
                )
            else:
                # Item-based lock
                if not self.state.has_item(required):
                    required_item: Item = self.items[required]
                    self._output(
                        f"The passage to the {direction} is locked. "
                        f"You need a {required_item.name} to open it."
                    )
                    return
                self.state.inventory.remove(required)
                del room.locked_exits[direction]
                required_item = self.items[required]
                self._output(
                    f"You use the {required_item.name} to unlock the passage "
                    f"to the {direction}."
                )

        # Move the player
        target_id: str = room.exits[direction]
        self.state.current_room = target_id
        self.state.visited_rooms.add(target_id)
        self._output(f"You travel {direction}.")
        self._print_room()


# ─────────────────────────────────────────────────────────────────────────────
# Banner & Entry Point
# ─────────────────────────────────────────────────────────────────────────────

_BANNER: str = """
╔══════════════════════════════════════════════════════════╗
║                                                        ║
║        ██████╗ ██╗   ██╗███╗   ██╗ ██████╗ ███████╗   ║
║        ██╔══██╗██║   ██║████╗  ██║██╔════╝ ██╔════╝   ║
║        ██║  ██║██║   ██║██╔██╗ ██║██║  ███╗█████╗     ║
║        ██║  ██║██║   ██║██║╚██╗██║██║   ██║██╔══╝     ║
║        ██████╔╝╚██████╔╝██║ ╚████║╚██████╔╝███████╗   ║
║        ╚═════╝  ╚═════╝ ╚═╝  ╚═══╝ ╚═════╝ ╚══════╝   ║
║                                                        ║
║              A  T E X T  A D V E N T U R E             ║
║                                                        ║
║          Type 'help' for commands. 'quit' to exit.     ║
║                                                        ║
╚══════════════════════════════════════════════════════════╝
"""


def main() -> None:
    """Build the dungeon, print the banner, and start the game loop."""
    print(_BANNER)

    rooms, items = build_dungeon()
    engine: DungeonEngine = DungeonEngine(rooms, items)

    try:
        engine.run()
    except KeyboardInterrupt:
        print("\n\n  Farewell, adventurer. The dungeon remembers your name.")
    except EOFError:
        print("\n\n  Farewell, adventurer. The dungeon remembers your name.")
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
