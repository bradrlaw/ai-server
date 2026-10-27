"""
Text-Based Adventure Game Engine
================================
A self-contained, single-file text adventure engine with a small dungeon world.
Uses only the Python standard library.

Run with: python dungeon.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class Item:
    """Represents a physical item that can be found, carried, and used.

    Attributes:
        name: Display name of the item.
        description: What the player sees when examining the item.
        on_use: Optional callback invoked when the player uses the item.
                Receives the current GameState and returns a message string.
    """

    name: str
    description: str
    on_use: Optional[Callable[[GameState], str]] = None


@dataclass
class LockedDoor:
    """Describes a locked exit and how to unlock it.

    Attributes:
        required_item: Item ID the player must possess (consumed on unlock).
        required_flag: Game flag that must be True to pass.
        locked_message: Shown when the player cannot pass.
        unlock_message: Shown the moment the door opens.
    """

    required_item: Optional[str] = None
    required_flag: Optional[str] = None
    locked_message: str = "The door is locked."
    unlock_message: str = "The door swings open."


@dataclass
class Room:
    """A single location in the game world.

    Attributes:
        name: Display name of the room.
        description: Base description shown to the player.
        exits: Mapping of direction (e.g. "north") to target room ID.
        items: List of item IDs currently present in this room.
        locked_doors: Mapping of direction to a LockedDoor descriptor.
    """

    name: str
    description: str
    exits: Dict[str, str] = field(default_factory=dict)
    items: List[str] = field(default_factory=list)
    locked_doors: Dict[str, LockedDoor] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Game State
# ---------------------------------------------------------------------------

class GameState:
    """Central mutable state for a single play-through.

    Attributes:
        current_room: ID of the room the player is standing in.
        inventory: List of item IDs the player is carrying.
        visited_rooms: Set of room IDs the player has entered.
        flags: Arbitrary boolean flags for tracking world events.
        history: Chronological list of all engine-generated messages.
    """

    def __init__(self, starting_room: str) -> None:
        self.current_room: str = starting_room
        self.inventory: List[str] = []
        self.visited_rooms: Set[str] = {starting_room}
        self.flags: Dict[str, bool] = {}
        self.history: List[str] = []

    def record(self, message: str) -> None:
        """Append a message to the game history."""
        self.history.append(message)

    def set_flag(self, flag: str, value: bool = True) -> None:
        """Set a boolean flag in the game state."""
        self.flags[flag] = value

    def get_flag(self, flag: str) -> bool:
        """Return the value of a boolean flag (default False)."""
        return self.flags.get(flag, False)


# ---------------------------------------------------------------------------
# Game Engine
# ---------------------------------------------------------------------------

class GameEngine:
    """Core engine that parses input, routes commands, and renders output.

    The engine owns the world definition (rooms + items) and a single
    GameState. All mutation flows through engine methods to keep state
    transitions explicit and testable.
    """

    # --- Static lookup tables (frozen after init) ---

    _COMMAND_ALIASES: Dict[str, str] = {
        "look": "look",
        "l": "look",
        "examine": "look_at",
        "x": "look_at",
        "look at": "look_at",
        "go": "go",
        "move": "go",
        "walk": "go",
        "take": "take",
        "grab": "take",
        "get": "take",
        "drop": "drop",
        "put": "drop",
        "inventory": "inventory",
        "i": "inventory",
        "inv": "inventory",
        "use": "use",
        "activate": "use",
        "help": "help",
        "h": "help",
        "?": "help",
        "history": "history",
        "hist": "history",
        "quit": "quit",
        "q": "quit",
        "exit": "quit",
    }

    _DIRECTION_ALIASES: Dict[str, str] = {
        "north": "north",
        "n": "north",
        "south": "south",
        "s": "south",
        "east": "east",
        "e": "east",
        "west": "west",
        "w": "west",
    }

    _BANNER: str = r"""
    ╔══════════════════════════════════════════════════════╗
    ║                                                    ║
    ║        ░▒▓█  T H E   D U N G E O N   █▓▒░          ║
    ║                                                    ║
    ║   A tiny text adventure in six rooms and four      ║
    ║   items. Can you find the secret ending?           ║
    ║                                                    ║
    ╚══════════════════════════════════════════════════════╝
    """

    def __init__(
        self,
        rooms: Dict[str, Room],
        items: Dict[str, Item],
        starting_room: str,
    ) -> None:
        """Initialize the engine with a world definition.

        Args:
            rooms: Mapping of room ID → Room.
            items: Mapping of item ID → Item.
            starting_room: Room ID where the player begins.
        """
        self._rooms: Dict[str, Room] = rooms
        self._items: Dict[str, Item] = items
        self._state: GameState = GameState(starting_room)
        self._running: bool = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Enter the main game loop until the player quits or EOF is hit."""
        self._print_banner()
        self._print_separator()
        self._cmd_look()
        print()

        while self._running:
            try:
                raw = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n\n  ...The darkness closes in. Goodbye, adventurer.")
                self._running = False
                break

            if not raw:
                continue

            self._print_separator()
            self._dispatch(raw)
            print()

    # ------------------------------------------------------------------
    # Input Parsing & Dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, raw: str) -> None:
        """Parse a raw input string and route to the appropriate handler.

        Handles two-word commands (look at, go, take, etc.) and
        bare direction words.
        """
        lowered = raw.lower().strip()

        # Two-word commands: "look at <x>", "go <dir>", "take <x>", etc.
        parts = lowered.split(None, 1)
        verb = parts[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        # Check two-word aliases first (longest match)
        if len(parts) == 2:
            two_word = f"{verb} {arg.split()[0]}" if arg else ""
            # "look at" is special: the rest after "at" is the argument
            if verb == "look" and arg.lower().startswith("at "):
                self._cmd_look_at(arg[3:].strip())
                return
            if verb == "look" and arg.lower() == "at":
                print("  Look at what? Name an item in this room.")
                return

        # Bare direction?
        if verb in self._DIRECTION_ALIASES:
            self._cmd_go(self._DIRECTION_ALIASES[verb])
            return

        # Single-word command lookup
        canonical = self._COMMAND_ALIASES.get(verb)
        if canonical is None:
            self._cmd_unknown(raw)
            return

        # Route
        if canonical == "look":
            self._cmd_look()
        elif canonical == "look_at":
            if arg:
                self._cmd_look_at(arg)
            else:
                print("  Look at what? Name an item in this room.")
        elif canonical == "go":
            if arg:
                direction = self._DIRECTION_ALIASES.get(arg)
                if direction:
                    self._cmd_go(direction)
                else:
                    print(f"  You can't go '{arg}'. Try north, south, east, or west.")
            else:
                print("  Go where? Try: go north, go south, go east, go west.")
        elif canonical == "take":
            if arg:
                self._cmd_take(arg)
            else:
                print("  Take what? Name an item in this room.")
        elif canonical == "drop":
            if arg:
                self._cmd_drop(arg)
            else:
                print("  Drop what? Name an item you're carrying.")
        elif canonical == "inventory":
            self._cmd_inventory()
        elif canonical == "use":
            if arg:
                self._cmd_use(arg)
            else:
                print("  Use what? Name an item you're carrying.")
        elif canonical == "help":
            self._cmd_help()
        elif canonical == "history":
            self._cmd_history()
        elif canonical == "quit":
            self._cmd_quit()

    def _cmd_unknown(self, raw: str) -> None:
        """Print a helpful error for unrecognized input."""
        print(f"  I don't understand '{raw}'. Type 'help' for available commands.")
        self._state.record(f"Unknown command: {raw}")

    # ------------------------------------------------------------------
    # Command Handlers
    # ------------------------------------------------------------------

    def _cmd_look(self) -> None:
        """Display the current room's description and visible items."""
        room = self._rooms[self._state.current_room]
        print(f"  {room.name}")
        print(f"  {self._get_room_description(room)}")
        print(f"  Exits: {', '.join(room.exits.keys()) if room.exits else 'none'}")

        visible_items = [self._items[iid].name for iid in room.items]
        if visible_items:
            print(f"  You see: {', '.join(visible_items)}")
        else:
            print("  You see nothing of note here.")

        self._state.record(f"Looked at {room.name}.")

    def _get_room_description(self, room: Room) -> str:
        """Return the effective description, applying dynamic modifications.

        Checks game flags and inventory to alter the base text:
          - Library: bookshelf moves when lever is pulled.
          - Armory: torch light mentioned if player carries one.
        """
        desc = room.description

        # Library dynamic: lever pulled
        if room.name == "Library" and self._state.get_flag("lever_pulled"):
            desc = (
                "The dusty bookshelf has been pushed aside, revealing a "
                "narrow stone passage to the west. Cold air breathes from "
                "the darkness beyond."
            )

        # Armory dynamic: torch in inventory
        if room.name == "Armory" and "torch" in self._state.inventory:
            desc += " A flickering light from your torch dances across the walls."

        return desc

    def _cmd_look_at(self, item_name: str) -> None:
        """Display the description of a specific item in the room or inventory."""
        item_id = self._find_item_by_name(item_name)
        if item_id is None:
            print(f"  You don't see anything called '{item_name}' here.")
            self._state.record(f"Looked for '{item_name}' — not found.")
            return

        item = self._items[item_id]
        print(f"  {item.name}: {item.description}")
        self._state.record(f"Examined {item.name}.")

    def _cmd_go(self, direction: str) -> None:
        """Attempt to move the player in the given direction.

        Handles locked doors, auto-use of keys, and room transitions.
        """
        room = self._rooms[self._state.current_room]

        if direction not in room.exits:
            print(f"  You can't go {direction} from here.")
            self._state.record(f"Tried to go {direction} — no exit.")
            return

        # Check for locked door
        if direction in room.locked_doors:
            door = room.locked_doors[direction]

            # Flag-based lock
            if door.required_flag and not self._state.get_flag(door.required_flag):
                print(f"  {door.locked_message}")
                self._state.record(f"Blocked at {direction} (flag lock).")
                return

            # Item-based lock (auto-use)
            if door.required_item:
                if door.required_item in self._state.inventory:
                    # Consume the key
                    self._state.inventory.remove(door.required_item)
                    print(f"  {door.unlock_message}")
                    self._state.record(f"Used {self._items[door.required_item].name} to open {direction} door.")
                else:
                    print(f"  {door.locked_message}")
                    self._state.record(f"Blocked at {direction} (needs {door.required_item}).")
                    return

        # Perform the move
        target_id = room.exits[direction]
        target_room = self._rooms[target_id]
        self._state.current_room = target_id
        self._state.visited_rooms.add(target_id)

        print(f"  You move {direction} into the {target_room.name}.")
        self._state.record(f"Moved {direction} to {target_room.name}.")

    def _cmd_take(self, item_name: str) -> None:
        """Pick up an item from the current room into inventory."""
        item_id = self._find_item_by_name(item_name)
        if item_id is None:
            print(f"  There's no '{item_name}' here to take.")
            self._state.record(f"Tried to take '{item_name}' — not here.")
            return

        room = self._rooms[self._state.current_room]
        if item_id not in room.items:
            print(f"  You can't take the {self._items[item_id].name} — it's not here.")
            self._state.record(f"Tried to take {item_id} — not in room.")
            return

        room.items.remove(item_id)
        self._state.inventory.append(item_id)
        print(f"  You pick up the {self._items[item_id].name}.")
        self._state.record(f"Took {self._items[item_id].name}.")

        # Special: taking the torch changes Armory description (handled in _get_room_description)
        # No extra logic needed here; the dynamic description check covers it.

    def _cmd_drop(self, item_name: str) -> None:
        """Place an item from inventory into the current room."""
        item_id = self._find_item_by_name(item_name)
        if item_id is None:
            print(f"  You don't have a '{item_name}' to drop.")
            self._state.record(f"Tried to drop '{item_name}' — not in inventory.")
            return

        if item_id not in self._state.inventory:
            print(f"  You aren't carrying the {self._items[item_id].name}.")
            self._state.record(f"Tried to drop {item_id} — not carried.")
            return

        self._state.inventory.remove(item_id)
        room = self._rooms[self._state.current_room]
        room.items.append(item_id)
        print(f"  You set down the {self._items[item_id].name}.")
        self._state.record(f"Dropped {self._items[item_id].name}.")

    def _cmd_inventory(self) -> None:
        """List all items in the player's inventory."""
        if not self._state.inventory:
            print("  Your pack is empty.")
        else:
            print("  You are carrying:")
            for item_id in self._state.inventory:
                item = self._items[item_id]
                print(f"    • {item.name}")
        self._state.record("Checked inventory.")

    def _cmd_use(self, item_name: str) -> None:
        """Activate an item's on_use callback (if it has one)."""
        item_id = self._find_item_by_name(item_name)
        if item_id is None:
            print(f"  You don't have a '{item_name}' to use.")
            self._state.record(f"Tried to use '{item_name}' — not found.")
            return

        if item_id not in self._state.inventory:
            print(f"  You aren't carrying the {self._items[item_id].name}.")
            self._state.record(f"Tried to use {item_id} — not carried.")
            return

        item = self._items[item_id]

        # Context-sensitive checks
        if item_id == "lever" and self._state.current_room != "library":
            print("  The lever is bolted to the bookshelf. You can only pull it here.")
            self._state.record("Tried to use lever out of context.")
            return

        if item.on_use is not None:
            message = item.on_use(self._state)
            print(f"  {message}")
            self._state.record(f"Used {item.name}.")
        else:
            print(f"  You fiddle with the {item.name}, but nothing happens.")
            self._state.record(f"Used {item.name} — no effect.")

    def _cmd_help(self) -> None:
        """Print a formatted list of all available commands."""
        help_text = (
            "  ┌─────────────────────────────────────────────────┐\n"
            "  │  AVAILABLE COMMANDS                              │\n"
            "  ├─────────────────────────────────────────────────┤\n"
            "  │  look / l              — describe current room    │\n"
            "  │  look at <item> / x    — examine an item          │\n"
            "  │  go <dir> / <dir>      — move (n/s/e/w)           │\n"
            "  │  take <item> / grab    — pick up an item          │\n"
            "  │  drop <item> / put     — set down an item         │\n"
            "  │  inventory / i         — list what you carry      │\n"
            "  │  use <item>            — activate an item         │\n"
            "  │  history / hist        — last 10 events           │\n"
            "  │  help / h              — show this help           │\n"
            "  │  quit / q              — leave the game           │\n"
            "  └─────────────────────────────────────────────────┘"
        )
        print(help_text)
        self._state.record("Viewed help.")

    def _cmd_history(self) -> None:
        """Print the last 10 events from the game history."""
        recent = self._state.history[-10:]
        if not recent:
            print("  No events recorded yet.")
        else:
            print("  Recent events:")
            for i, event in enumerate(recent, 1):
                print(f"    {i:2d}. {event}")
        self._state.record("Reviewed history.")

    def _cmd_quit(self) -> None:
        """End the game gracefully."""
        print("\n  You step back into the light. The dungeon keeps its secrets.")
        if self._state.get_flag("won"):
            print("  ...but you carry the truth of the ancient scroll with you.")
        print("  Goodbye, adventurer.\n")
        self._running = False
        self._state.record("Quit the game.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_item_by_name(self, name: str) -> Optional[str]:
        """Resolve a player-supplied name to an item ID via partial match.

        Matching is case-insensitive. Tries exact match first, then
        substring (item name contains the query), then prefix.

        Args:
            name: The (possibly partial) name typed by the player.

        Returns:
            The item ID if a match is found, else None.
        """
        name = name.lower()

        # Exact match
        for item_id, item in self._items.items():
            if item.name.lower() == name:
                return item_id

        # Substring: item name contains the query
        for item_id, item in self._items.items():
            if name in item.name.lower():
                return item_id

        # Prefix
        for item_id, item in self._items.items():
            if item.name.lower().startswith(name):
                return item_id

        return None

    def _print_banner(self) -> None:
        """Display the ASCII art title banner."""
        print(self._BANNER)

    def _print_separator(self) -> None:
        """Print a horizontal separator line between turns."""
        print("  " + "─" * 50)


# ---------------------------------------------------------------------------
# World Construction
# ---------------------------------------------------------------------------

def build_dungeon() -> tuple[Dict[str, Room], Dict[str, Item], str]:
    """Construct the mini-dungeon world: 6 rooms, 5 items.

    Returns:
        A tuple of (rooms, items, starting_room_id).
    """

    # --- Items -----------------------------------------------------------

    def _lever_use(state: GameState) -> str:
        """Pull the lever: permanently unlocks the Library's west passage."""
        if state.get_flag("lever_pulled"):
            return "The lever is already in the 'pulled' position. Nothing more to do."
        state.set_flag("lever_pulled", True)
        # Remove lever from inventory (it's now stuck in the wall)
        if "lever" in state.inventory:
            state.inventory.remove("lever")
        return (
            "You yank the lever. With a grinding groan, the massive bookshelf "
            "slides sideways, revealing a narrow stone passage to the west. "
            "Cold air wafts through."
        )

    def _scroll_use(state: GameState) -> str:
        """Read the ancient scroll: triggers the secret ending."""
        state.set_flag("won", True)
        return (
            "You unroll the ancient scroll. The ink shimmers with an inner "
            "light. The words read:\n"
            "\n"
            "    'The dungeon was never a prison. It was a question.\n"
            "     Those who seek gold find only coin.\n"
            "     Those who seek the path find the way home.\n"
            "     You have found the way home.'\n"
            "\n"
            "The scroll crumbles to dust in your hands. The walls of the "
            "chamber glow warm, and a soft light fills the air. You feel "
            "the stones beneath you become a door, and the door opens "
            "onto a field of golden grass under a summer sky.\n"
            "\n"
            "★ ★ ★  SECRET ENDING UNLOCKED  ★ ★ ★"
        )

    items: Dict[str, Item] = {
        "rusted_key": Item(
            name="rusted key",
            description=(
                "A small iron key, green with rust. It looks old — "
                "centuries old, perhaps. It fits a lock you haven't seen yet."
            ),
        ),
        "lever": Item(
            name="lever",
            description=(
                "A heavy iron lever mounted on the bookshelf's base. "
                "The paint is chipped, but the mechanism looks sturdy. "
                "A brass plate reads: 'PULL TO REVEAL'."
            ),
            on_use=_lever_use,
        ),
        "torch": Item(
            name="torch",
            description=(
                "A short wooden torch, still damp with tallow. "
                "It hasn't been lit, but it should be easy to strike."
            ),
        ),
        "golden_coin": Item(
            name="golden coin",
            description=(
                "A heavy gold coin, stamped with the face of a king no "
                "historian has ever catalogued. It is warm to the touch, "
                "as if it still remembers being held. Worth a fortune, "
                "or worthless — depending on who you ask."
            ),
        ),
        "ancient_scroll": Item(
            name="ancient scroll",
            description=(
                "A scroll of vellum so old it seems to breathe. The "
                "edges are brittle, but the ink is impossibly fresh. "
                "You can feel it wanting to be read."
            ),
            on_use=_scroll_use,
        ),
    }

    # --- Rooms -----------------------------------------------------------

    rooms: Dict[str, Room] = {
        "entrance": Room(
            name="Entrance Hall",
            description=(
                "You stand in a low-ceilinged hall of rough-hewn stone. "
                "Dust motes drift in a shaft of pale light from a crack "
                "above. A rusted key lies on the floor near your boots. "
                "The air smells of damp earth and forgotten centuries."
            ),
            exits={"north": "library", "east": "armory", "south": "garden"},
            items=["rusted_key"],
        ),
        "library": Room(
            name="Library",
            description=(
                "Towering bookshelves line the walls, their spines cracked "
                "and illegible. A thick layer of dust coats every surface. "
                "A heavy bookshelf in the center of the room seems "
                "disproportionately solid — almost like a door in disguise. "
                "A small iron lever is mounted at its base."
            ),
            exits={"south": "entrance", "west": "secret_chamber"},
            items=["lever"],
            locked_doors={
                "west": LockedDoor(
                    required_flag="lever_pulled",
                    locked_message=(
                        "The bookshelf blocks the way. It doesn't budge. "
                        "Perhaps the lever at its base is the key."
                    ),
                    unlock_message="The bookshelf grinds open. A passage yawns west.",
                ),
            },
        ),
        "armory": Room(
            name="Armory",
            description=(
                "Racks of corroded swords and dented shields line the walls. "
                "A torch leans against the far wall, still wrapped in "
                "waxed cloth. A heavy iron door to the north is barred "
                "with a lock — old, rusted, but firmly shut."
            ),
            exits={"west": "entrance", "north": "treasure"},
            items=["torch"],
            locked_doors={
                "north": LockedDoor(
                    required_item="rusted_key",
                    locked_message=(
                        "The iron door is locked. The keyhole is small and "
                        "rust-streaked. You need a key — an old, rusty one, "
                        "perhaps."
                    ),
                    unlock_message=(
                        "The rusted key fits perfectly. With a satisfying "
                        "clunk, the lock releases and the door swings open."
                    ),
                ),
            },
        ),
        "treasure": Room(
            name="Treasure Room",
            description=(
                "A small circular chamber. In the center, on a stone "
                "pedestal, a single golden coin catches the light. Above "
                "the pedestal, an inscription is carved into the wall:\n"
                "\n"
                "    'Gold is a answer to a question no one asked.\n"
                "     The real treasure walks a different path.'\n"
                "\n"
                "The only exit is the door you came through, south."
            ),
            exits={"south": "armory"},
            items=["golden_coin"],
        ),
        "secret_chamber": Room(
            name="Secret Chamber",
            description=(
                "A hidden room, barely larger than a closet. The walls are "
                "smooth white marble — a stark contrast to the rough stone "
                "of the rest of the dungeon. On a small shelf rests a "
                "scroll of ancient vellum, glowing faintly. The air here "
                "is still and warm, as if time moved differently in this "
                "place."
            ),
            exits={"east": "library"},
            items=["ancient_scroll"],
        ),
        "garden": Room(
            name="Garden",
            description=(
                "You emerge into a peaceful clearing. Green grass stretches "
                "under a canopy of ancient oaks. A stream bubbles over "
                "smooth stones. Birdsong fills the air. For a moment, the "
                "dungeon feels very far away.\n"
                "\n"
                "    You've found peace."
            ),
            exits={"north": "entrance"},
            items=[],
        ),
    }

    return rooms, items, "entrance"


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    """Instantiate the dungeon, create the engine, and start the game loop."""
    rooms, items, start = build_dungeon()
    engine = GameEngine(rooms=rooms, items=items, starting_room=start)

    try:
        engine.run()
    except (EOFError, KeyboardInterrupt):
        print("\n\n  ...The darkness closes in. Goodbye, adventurer.")


if __name__ == "__main__":
    main()
