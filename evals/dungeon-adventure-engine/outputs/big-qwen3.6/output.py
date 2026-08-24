#!/usr/bin/env python3
"""
Text-Based Adventure Game Engine
A production-quality, standard-library-only implementation featuring a modular
command system, state tracking, and a playable mini-dungeon.
"""

from __future__ import annotations
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Tuple, Set, Any

# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class Item:
    """Represents an interactive object in the game world."""
    id: str
    name: str
    description: str
    on_use: Optional[Callable[[GameState, Any], str]] = None

@dataclass
class Room:
    """Represents a location in the game world."""
    id: str
    name: str
    description: str
    exits: Dict[str, str]  # direction -> target_room_id
    items: List[str] = field(default_factory=list)
    locked_doors: Dict[str, str] = field(default_factory=dict)  # direction -> required_item_id

@dataclass
class GameState:
    """Tracks the current state of the player and game progression."""
    current_room: str
    inventory: List[str] = field(default_factory=list)
    visited_rooms: Set[str] = field(default_factory=set)
    flags: Dict[str, bool] = field(default_factory=dict)
    history: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core Engine
# ---------------------------------------------------------------------------

class GameEngine:
    """
    Central controller for the adventure game.
    Manages input parsing, command routing, state updates, and output formatting.
    """

    ASCII_BANNER: str = (
        "╔════════════════════════════════════════╗\n"
        "║         DUNGEON OF WHISPERS            ║\n"
        "║        A Text Adventure                ║\n"
        "╚════════════════════════════════════════╝"
    )

    DIRECTION_MAP: Dict[str, str] = {
        "n": "north", "s": "south", "e": "east", "w": "west",
        "north": "north", "south": "south", "east": "east", "west": "west",
    }

    COMMAND_ALIASES: Dict[str, str] = {
        "look": "look", "examine": "look",
        "go": "go", "move": "go",
        "take": "take", "grab": "take",
        "drop": "drop",
        "inventory": "inventory", "i": "inventory", "inv": "inventory",
        "use": "use",
        "help": "help",
        "history": "history",
        "quit": "quit", "exit": "quit",
    }

    def __init__(self, rooms: Dict[str, Room], items: Dict[str, Item]) -> None:
        """
        Initialize the game engine with world data.

        Args:
            rooms: Dictionary mapping room IDs to Room objects.
            items: Dictionary mapping item IDs to Item objects.
        """
        self.rooms = rooms
        self.items = items
        self.state = GameState(current_room="entrance")
        self._is_running = True

    def run(self) -> None:
        """Start the main game loop."""
        print(self.ASCII_BANNER)
        self._add_to_history("Game started.")
        print(self._cmd_look([]))

        while self._is_running:
            try:
                raw_input = input("> ")
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break

            cmd, args = self.parse_input(raw_input)
            if cmd == "unknown":
                msg = f"Unknown command: '{raw_input}'. Type 'help' for commands."
                self._add_to_history(msg)
                print(f"\n---\n{msg}")
                continue

            handler = getattr(self, f"_cmd_{cmd}", None)
            if handler:
                result = handler(args)
                print(f"\n---\n{result}")
                if cmd == "quit":
                    self._is_running = False
            else:
                print(f"\n---\nCommand not found: {cmd}")

    # -----------------------------------------------------------------------
    # Input Parsing
    # -----------------------------------------------------------------------

    def parse_input(self, raw: str) -> Tuple[str, List[str]]:
        """
        Normalize and parse player input into a command and arguments.

        Args:
            raw: The raw string input from the player.

        Returns:
            A tuple of (command_name, list_of_arguments).
        """
        parts = raw.strip().lower().split()
        if not parts:
            return "look", []

        first = parts[0]

        # Implicit direction command
        if first in self.DIRECTION_MAP:
            return "go", [self.DIRECTION_MAP[first]]

        # Handle 'look' / 'examine' with optional 'at'
        if first in ("look", "examine"):
            args = parts[1:]
            if args and args[0] == "at":
                args = args[1:]
            return "look", args

        # Match against aliases or partial prefixes
        for alias, cmd in self.COMMAND_ALIASES.items():
            if first == alias or first.startswith(alias):
                return cmd, parts[1:]

        # Fallback: partial match against base commands
        base_cmds = {"look", "go", "take", "drop", "inventory", "use", "help", "history", "quit"}
        for cmd in base_cmds:
            if cmd.startswith(first):
                return cmd, parts[1:]

        return "unknown", parts

    # -----------------------------------------------------------------------
    # Command Handlers
    # -----------------------------------------------------------------------

    def _cmd_look(self, args: List[str]) -> str:
        """Describe the current room or a specific item."""
        room = self.rooms[self.state.current_room]
        if args:
            item_id = self._resolve_item(" ".join(args))
            if item_id:
                return self.items[item_id].description
            return "You don't see that here."
        else:
            msg = f"{room.name}\n{room.description}\n"
            if room.items:
                item_names = [self.items[iid].name for iid in room.items]
                msg += f"Items here: {', '.join(item_names)}\n"
            return msg

    def _cmd_go(self, args: List[str]) -> str:
        """Move to an adjacent room."""
        if not args:
            return "Go where?"
        direction = self._resolve_direction(args[0])
        if not direction:
            return "I don't know that direction."

        room = self.rooms[self.state.current_room]
        if direction not in room.exits:
            return "You can't go that way."

        if direction in room.locked_doors:
            req_key_id = room.locked_doors[direction]
            if req_key_id not in self.state.inventory:
                key_name = self.items[req_key_id].name
                return f"The {direction} door is locked. You need the {key_name}."
            else:
                # Auto-use key
                self.state.inventory.remove(req_key_id)
                room.locked_doors.pop(direction)
                self._add_to_history(f"You unlock the {direction} door.")
                msg = f"You unlock the {direction} door and proceed."
        else:
            msg = "You move."

        self.state.current_room = room.exits[direction]
        self.state.visited_rooms.add(self.state.current_room)
        self._add_to_history(f"Moved to {room.exits[direction]}.")
        return msg + "\n" + self._cmd_look([])

    def _cmd_take(self, args: List[str]) -> str:
        """Pick up an item from the current room."""
        if not args:
            return "Take what?"
        item_id = self._resolve_item(" ".join(args))
        if not item_id:
            return "You don't see that here."
        if item_id not in self.rooms[self.state.current_room].items:
            return "You can't take that."

        item = self.items[item_id]
        self.rooms[self.state.current_room].items.remove(item_id)
        self.state.inventory.append(item_id)
        self._add_to_history(f"Took {item.name}.")

        # Special behavior: torch changes armory atmosphere
        if item_id == "torch" and self.state.current_room == "armory":
            self.rooms["armory"].description = (
                "A dimly lit armory, now illuminated by the flickering light of your torch. "
                "Racks of old weapons line the walls."
            )

        return f"You take the {item.name}."

    def _cmd_drop(self, args: List[str]) -> str:
        """Discard an item from inventory to the current room."""
        if not args:
            return "Drop what?"
        item_id = self._resolve_item(" ".join(args))
        if not item_id or item_id not in self.state.inventory:
            return "You aren't carrying that."

        item = self.items[item_id]
        self.state.inventory.remove(item_id)
        self.rooms[self.state.current_room].items.append(item_id)
        self._add_to_history(f"Dropped {item.name}.")
        return f"You drop the {item.name}."

    def _cmd_use(self, args: List[str]) -> str:
        """Activate an item's special behavior."""
        if not args:
            return "Use what?"
        item_id = self._resolve_item(" ".join(args))
        if not item_id or item_id not in self.state.inventory:
            return "You aren't carrying that."

        item = self.items[item_id]
        if item.on_use:
            return item.on_use(self.state, self)
        return f"You can't use the {item.name} here."

    def _cmd_inventory(self, args: List[str]) -> str:
        """List all items currently carried by the player."""
        if not self.state.inventory:
            return "Your inventory is empty."
        names = [self.items[iid].name for iid in self.state.inventory]
        return f"Inventory: {', '.join(names)}"

    def _cmd_help(self, args: List[str]) -> str:
        """Display available commands and usage."""
        return (
            "Available commands:\n"
            "  look / examine [at] <item> - Describe room or item\n"
            "  go / move <direction> / <direction> - Travel (n/s/e/w)\n"
            "  take / grab <item> - Pick up an item\n"
            "  drop <item> - Discard an item\n"
            "  inventory / i / inv - List carried items\n"
            "  use <item> - Activate an item\n"
            "  history - Show last 10 events\n"
            "  help - Show this message\n"
            "  quit / exit - End game"
        )

    def _cmd_history(self, args: List[str]) -> str:
        """Display the last 10 game events."""
        if not self.state.history:
            return "No history yet."
        last_events = self.state.history[-10:]
        return "\n".join(f"  • {evt}" for evt in last_events)

    def _cmd_quit(self, args: List[str]) -> str:
        """Gracefully exit the game."""
        self._add_to_history("Quit game.")
        return "Goodbye!"

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _add_to_history(self, msg: str) -> None:
        """Append a message to the game history log."""
        self.state.history.append(msg)

    def _resolve_item(self, name: str) -> Optional[str]:
        """
        Find an item ID by matching against inventory and current room items.
        Supports exact and partial prefix matching.
        """
        name = name.lower()
        candidates = self.state.inventory + self.rooms[self.state.current_room].items

        if name in candidates:
            return name
        for iid in candidates:
            if self.items[iid].name.lower().startswith(name):
                return iid
        return None

    def _resolve_direction(self, raw: str) -> Optional[str]:
        """Convert shorthand or full direction names to canonical form."""
        return self.DIRECTION_MAP.get(raw.lower())


# ---------------------------------------------------------------------------
# World Setup & Callbacks
# ---------------------------------------------------------------------------

def _lever_on_use(state: GameState, engine: Any) -> str:
    """Callback for pulling the lever in the Library."""
    if state.flags.get("lever_pulled"):
        return "You pull the lever again, but nothing happens."
    state.flags["lever_pulled"] = True
    lib = engine.rooms["library"]
    lib.locked_doors.pop("west", None)
    lib.description = (
        "A dusty library. The bookshelf has been moved aside, "
        "revealing a dark passage to the west."
    )
    return "You pull the lever with a heavy clunk. The bookshelf grinds aside, revealing a secret passage to the west."

def _scroll_on_use(state: GameState, engine: Any) -> str:
    """Callback for reading the ancient scroll."""
    state.flags["won"] = True
    return (
        "You unroll the ancient scroll. It glows with an ethereal light, "
        "revealing the secret ending: You have conquered the dungeon and "
        "found true wisdom. The game is won!"
    )


def _build_world() -> Tuple[Dict[str, Room], Dict[str, Item]]:
    """Construct and return the game world data structures."""
    items: Dict[str, Item] = {
        "rusted_key": Item("rusted_key", "rusted key", "A heavy iron key, heavily corroded but still functional."),
        "lever": Item("lever", "lever", "A brass lever set into the wall. It looks like it hasn't been touched in years.", on_use=_lever_on_use),
        "torch": Item("torch", "torch", "A sturdy wooden torch. It hasn't been lit yet."),
        "golden_coin": Item("golden_coin", "golden coin", "A gleaming gold coin stamped with a forgotten monarch's face. 'Riches fade, but curiosity endures,' it seems to whisper."),
        "ancient_scroll": Item("ancient_scroll", "ancient scroll", "A fragile parchment sealed with wax. Ancient runes cover its surface.", on_use=_scroll_on_use),
    }

    rooms: Dict[str, Room] = {
        "entrance": Room(
            "entrance", "Entrance Hall",
            "A cold stone hall. Damp air seeps from the cracks in the walls. "
            "A heavy wooden door stands to the south, leading to a garden.",
            {"north": "library", "east": "armory", "south": "garden"},
            items=["rusted_key"]
        ),
        "library": Room(
            "library", "Library",
            "Rows of rotting books line the shelves. A heavy oak bookshelf blocks the way west.",
            {"south": "entrance"},
            items=["lever"],
            locked_doors={"west": "lever_pulled_flag"}  # Placeholder, logic handled in callback
        ),
        "armory": Room(
            "armory", "Armory",
            "Racks of rusted weapons line the walls. A sturdy door to the north appears locked.",
            {"west": "entrance"},
            items=["torch"],
            locked_doors={"north": "rusted_key"}
        ),
        "treasure_room": Room(
            "treasure_room", "Treasure Room",
            "A small vault filled with dust and forgotten wealth. An inscription on the wall reads: "
            "'The greatest treasure is the path itself.'",
            {"south": "armory"},
            items=["golden_coin"]
        ),
        "secret_chamber": Room(
            "secret_chamber", "Secret Chamber",
            "A hidden alcove, untouched by time. A pedestal stands in the center.",
            {"east": "library"},
            items=["ancient_scroll"]
        ),
        "garden": Room(
            "garden", "Garden",
            "A peaceful clearing outside the dungeon. Sunlight filters through ancient trees. "
            "You've found peace.",
            {"north": "entrance"}
        ),
    }

    return rooms, items


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    rooms, items = _build_world()
    engine = GameEngine(rooms, items)
    engine.run()
