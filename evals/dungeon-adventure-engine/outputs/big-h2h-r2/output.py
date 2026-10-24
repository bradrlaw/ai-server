"""
The Forgotten Dungeon — a text-based adventure game engine.

A single-file, standard-library-only implementation featuring:
  - A flexible command parser with synonyms and partial matches
  - A room/item/flag-based world model
  - A small 6-room dungeon with 5 interactive items
  - Graceful error handling and clean separation of concerns

Run with:  python forgotten_dungeon.py
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


# ─────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────


class Item:
    """A game item that can be picked up, dropped, and optionally used.

    Attributes:
        item_id:   Unique identifier used internally.
        name:      Human-readable display name.
        description: Text shown when the player examines the item.
        on_use:    Optional callback invoked when the player uses the item.
                   Receives the GameEngine and returns a message string.
    """

    def __init__(
        self,
        item_id: str,
        name: str,
        description: str,
        on_use: Optional[Callable[["GameEngine"], str]] = None,
    ) -> None:
        self.item_id: str = item_id
        self.name: str = name
        self.description: str = description
        self.on_use: Optional[Callable[["GameEngine"], str]] = on_use

    def __repr__(self) -> str:
        return f"Item({self.item_id!r}, name={self.name!r})"


class Room:
    """A room in the game world.

    Attributes:
        room_id:      Unique identifier.
        name:         Display name.
        description:  Base description text (may be augmented dynamically).
        exits:        Mapping of direction string → target room_id.
        items:        List of item_ids currently present in the room.
        locked_exits: Mapping of direction → lock spec dict with keys:
                      "required_item" (str|None), "unlock_flag" (str|None),
                      "message" (str shown when blocked).
    """

    def __init__(
        self,
        room_id: str,
        name: str,
        description: str,
        exits: Optional[Dict[str, str]] = None,
        items: Optional[List[str]] = None,
        locked_exits: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        self.room_id: str = room_id
        self.name: str = name
        self.description: str = description
        self.exits: Dict[str, str] = exits if exits is not None else {}
        self.items: List[str] = items if items is not None else []
        self.locked_exits: Dict[str, Dict[str, Any]] = (
            locked_exits if locked_exits is not None else {}
        )

    def __repr__(self) -> str:
        return f"Room({self.room_id!r}, name={self.name!r})"


class GameState:
    """Mutable game state: position, inventory, flags, and event history.

    Attributes:
        current_room:  The room_id the player is currently in.
        inventory:     Ordered list of item_ids the player carries.
        visited_rooms: Set of room_ids the player has entered.
        flags:         Boolean flags for persistent world events.
        history:       Chronological list of event/message strings.
    """

    def __init__(self, starting_room: str) -> None:
        self.current_room: str = starting_room
        self.inventory: List[str] = []
        self.visited_rooms: Set[str] = {starting_room}
        self.flags: Dict[str, bool] = {}
        self.history: List[str] = []

    # -- Flag helpers ---------------------------------------------------

    def set_flag(self, flag: str, value: bool = True) -> None:
        """Set a named boolean flag."""
        self.flags[flag] = value

    def get_flag(self, flag: str) -> bool:
        """Return the value of a named flag (default False)."""
        return self.flags.get(flag, False)

    # -- Inventory helpers ----------------------------------------------

    def has_item(self, item_id: str) -> bool:
        """Check whether the player carries a given item."""
        return item_id in self.inventory

    def add_item(self, item_id: str) -> None:
        """Add an item to the player's inventory."""
        if item_id not in self.inventory:
            self.inventory.append(item_id)

    def remove_item(self, item_id: str) -> None:
        """Remove an item from the player's inventory."""
        if item_id in self.inventory:
            self.inventory.remove(item_id)

    # -- History --------------------------------------------------------

    def add_to_history(self, event: str) -> None:
        """Append an event message to the game history log."""
        self.history.append(event)


# ─────────────────────────────────────────────
# Game Engine
# ─────────────────────────────────────────────


class GameEngine:
    """Core engine: parses input, dispatches commands, mutates state.

    The engine owns the world (rooms + items), the current GameState,
    and all command-handling logic.  It is deliberately free of any
    global mutable state so that multiple engines can coexist (e.g.
    for testing).
    """

    # -- Static lookup tables -------------------------------------------

    #: Maps every recognised command token (lowercase) to a canonical name.
    COMMAND_SYNONYMS: Dict[str, str] = {
        # look
        "look": "look",
        "l": "look",
        "look at": "look_at",
        "examine": "look_at",
        "inspect": "look_at",
        # movement
        "go": "go",
        "move": "move",
        "walk": "move",
        "head": "move",
        # item manipulation
        "take": "take",
        "grab": "take",
        "get": "take",
        "pick up": "take",
        "drop": "drop",
        "put down": "drop",
        # inventory
        "inventory": "inventory",
        "inv": "inventory",
        "i": "inventory",
        "list": "inventory",
        # use
        "use": "use",
        "read": "use",
        "activate": "use",
        "pull": "use",
        # meta
        "help": "help",
        "h": "help",
        "?": "help",
        "history": "history",
        "log": "history",
        "quit": "quit",
        "exit": "quit",
        "q": "quit",
    }

    #: Maps direction tokens to canonical compass names.
    DIRECTION_SYNONYMS: Dict[str, str] = {
        "n": "north",
        "north": "north",
        "s": "south",
        "south": "south",
        "e": "east",
        "east": "east",
        "w": "west",
        "west": "west",
    }

    #: Directions the game supports (for error messages).
    VALID_DIRECTIONS: Tuple[str, ...] = ("north", "south", "east", "west")

    # -- Construction ---------------------------------------------------

    def __init__(
        self,
        rooms: Dict[str, Room],
        items: Dict[str, Item],
        starting_room: str,
    ) -> None:
        """Initialise the engine with a world definition.

        Args:
            rooms:         Mapping of room_id → Room.
            items:         Mapping of item_id → Item.
            starting_room: room_id where the player begins.
        """
        self.rooms: Dict[str, Room] = rooms
        self.items: Dict[str, Item] = items
        self.state: GameState = GameState(starting_room)
        self.running: bool = True

    # ─────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────

    def process_input(self, raw: str) -> str:
        """Parse a line of player input and return the response text.

        Args:
            raw: The raw string typed by the player.

        Returns:
            The message to display to the player.
        """
        text = raw.strip().lower()
        if not text:
            return ""

        command, arg = self._parse_command(text)

        if command == "quit":
            self.running = False
            return "You leave the dungeon. Goodbye!"

        handler = self._dispatch(command, arg)
        response = handler() if handler else "Unknown command. Type 'help' for a list of commands."

        # Record the event in history
        self.state.add_to_history(f"> {raw.strip()}")
        return response

    def run_loop(self) -> None:
        """Run the interactive game loop until quit, EOF, or Ctrl-C."""
        while self.running:
            try:
                user_input = input("\n> ")
            except (EOFError, KeyboardInterrupt):
                print("\n\nYou leave the dungeon. Goodbye!")
                break

            response = self.process_input(user_input)
            if response:
                self._print_separator()
                print(response)

    # ─────────────────────────────────────────
    # Command Parsing
    # ─────────────────────────────────────────

    def _parse_command(self, text: str) -> Tuple[str, Optional[str]]:
        """Break a lowercased input string into (command, argument).

        Handles bare directions, exact command matches, and
        prefix-based matching for multi-word commands like
        "look at <thing>".

        Args:
            text: Lowercased, stripped player input.

        Returns:
            A (canonical_command, optional_argument) tuple.
            An empty string command means "unrecognised".
        """
        # 1. Bare direction → movement
        if text in self.DIRECTION_SYNONYMS:
            return ("move", self.DIRECTION_SYNONYMS[text])

        # 2. Exact command match (no argument)
        if text in self.COMMAND_SYNONYMS:
            return (self.COMMAND_SYNONYMS[text], None)

        # 3. Prefix match — longest command word first so that
        #    "look at sword" matches "look at" before "look".
        for cmd_token in sorted(self.COMMAND_SYNONYMS, key=len, reverse=True):
            if text.startswith(cmd_token + " "):
                arg = text[len(cmd_token) + 1 :].strip()
                canonical = self.COMMAND_SYNONYMS[cmd_token]
                return (canonical, arg if arg else None)

        # 4. Unrecognised
        return ("", None)

    def _dispatch(
        self, command: str, arg: Optional[str]
    ) -> Optional[Callable[[], str]]:
        """Return a zero-arg callable that produces the response for a command.

        Args:
            command: Canonical command name (e.g. "move", "take").
            arg:     Optional argument string.

        Returns:
            A callable, or None if the command is unknown.
        """
        dispatch_table: Dict[str, Callable[[], str]] = {
            "look": lambda: self._cmd_look(arg),
            "look_at": lambda: self._cmd_look_at(arg),
            "move": lambda: self._cmd_move(arg),
            "take": lambda: self._cmd_take(arg),
            "drop": lambda: self._cmd_drop(arg),
            "inventory": lambda: self._cmd_inventory(),
            "use": lambda: self._cmd_use(arg),
            "help": lambda: self._cmd_help(),
            "history": lambda: self._cmd_history(),
        }
        return dispatch_table.get(command)

    # ─────────────────────────────────────────
    # Command Handlers
    # ─────────────────────────────────────────

    def _cmd_look(self, arg: Optional[str]) -> str:
        """Handle 'look' — describe the current room and visible items.

        If an argument is supplied, redirect to look_at.
        """
        if arg:
            return self._cmd_look_at(arg)
        room = self.rooms[self.state.current_room]
        desc = self._get_room_description(room)
        lines = [f"You are in the {room.name}.", "", desc]

        # List visible items in the room
        if room.items:
            lines.append("")
            lines.append("You see: " + ", ".join(
                self.items[iid].name for iid in room.items
            ))

        # List exits
        if room.exits:
            exit_dirs = ", ".join(sorted(room.exits.keys()))
            lines.append(f"Exits: {exit_dirs}")

        return "\n".join(lines)

    def _cmd_look_at(self, arg: Optional[str]) -> str:
        """Handle 'look at <item>' / 'examine <item>'."""
        if not arg:
            return "Look at what? Specify an item name."

        # Check if it's an item in the room
        item_id = self._find_item_in_room(arg)
        if item_id:
            return self.items[item_id].description

        # Check if it's an item in inventory
        item_id = self._find_item_in_inventory(arg)
        if item_id:
            return self.items[item_id].description

        # Check if it's a room name (flavour)
        for rid, room in self.rooms.items():
            if arg in room.name.lower():
                return f"The {room.name}: {room.description}"

        return f"You don't see a '{arg}' here to look at."

    def _cmd_move(self, arg: Optional[str]) -> str:
        """Handle movement: 'go <dir>', 'move <dir>', or bare '<dir>'."""
        if not arg:
            return "Move where? Try north, south, east, or west."

        direction = self.DIRECTION_SYNONYMS.get(arg)
        if direction is None:
            return f"'{arg}' is not a valid direction. Try: {', '.join(self.VALID_DIRECTIONS)}."

        room = self.rooms[self.state.current_room]

        # Is this direction even an exit?
        if direction not in room.exits:
            return f"You can't go {direction} from here. There's no passage that way."

        # Is the exit locked?
        if direction in room.locked_exits:
            lock = room.locked_exits[direction]
            unlock_flag = lock.get("unlock_flag")
            required_item = lock.get("required_item")

            # Flag-based unlock (e.g. lever pulled)
            if unlock_flag and self.state.get_flag(unlock_flag):
                pass  # door is open, proceed
            # Item-based unlock (e.g. key)
            elif required_item and self.state.has_item(required_item):
                # Auto-consume the key
                self.state.remove_item(required_item)
                self.state.add_to_history(f"Used the {self.items[required_item].name} to unlock the door.")
                return self._execute_move(room, direction, extra=f"The {self.items[required_item].name} turns in the lock with a satisfying click. The door swings open.")
            else:
                return lock.get("message", f"The {direction} exit is locked.")

        return self._execute_move(room, direction)

    def _execute_move(self, room: Room, direction: str, extra: str = "") -> str:
        """Perform the actual room transition.

        Args:
            room:     The room we are leaving.
            direction: The compass direction.
            extra:    Optional prefix message (e.g. door unlocking).
        """
        target_id = room.exits[direction]
        target = self.rooms[target_id]
        self.state.current_room = target_id
        self.state.visited_rooms.add(target_id)
        self.state.add_to_history(f"Moved {direction} to {target.name}.")

        prefix = extra + "\n" if extra else ""
        return (
            prefix
            + f"You move {direction} into the {target.name}."
            + "\n"
            + self._get_room_description(target)
        )

    def _cmd_take(self, arg: Optional[str]) -> str:
        """Handle 'take <item>' / 'grab <item>'."""
        if not arg:
            return "Take what? Specify an item name."

        item_id = self._find_item_in_room(arg)
        if item_id is None:
            return f"There's no '{arg}' here to take."

        item = self.items[item_id]
        # Remove from room, add to inventory
        self.rooms[self.state.current_room].items.remove(item_id)
        self.state.add_item(item_id)
        self.state.add_to_history(f"Took {item.name}.")

        msg = f"You pick up the {item.name}."
        # Torch special: note the effect
        if item_id == "torch" and not self.state.get_flag("torch_taken"):
            self.state.set_flag("torch_taken")
            msg += "\n(A faint glow follows you as you walk.)"
        return msg

    def _cmd_drop(self, arg: Optional[str]) -> str:
        """Handle 'drop <item>'."""
        if not arg:
            return "Drop what? Specify an item name."

        item_id = self._find_item_in_inventory(arg)
        if item_id is None:
            return f"You aren't carrying a '{arg}'."

        item = self.items[item_id]
        self.state.remove_item(item_id)
        self.rooms[self.state.current_room].items.append(item_id)
        self.state.add_to_history(f"Dropped {item.name}.")
        return f"You set down the {item.name}."

    def _cmd_inventory(self) -> str:
        """Handle 'inventory' / 'i' / 'inv'."""
        if not self.state.inventory:
            return "Your inventory is empty."
        lines = ["Your inventory:"]
        for iid in self.state.inventory:
            lines.append(f"  • {self.items[iid].name}")
        return "\n".join(lines)

    def _cmd_use(self, arg: Optional[str]) -> str:
        """Handle 'use <item>' / 'read <item>' / 'pull <item>'."""
        if not arg:
            return "Use what? Specify an item name."

        item_id = self._find_item_in_inventory(arg)
        if item_id is None:
            return f"You aren't carrying a '{arg}' to use."

        item = self.items[item_id]
        if item.on_use is None:
            return f"You try to use the {item.name}, but nothing happens."

        response = item.on_use(self)
        self.state.add_to_history(f"Used {item.name}.")
        return response

    def _cmd_help(self) -> str:
        """Handle 'help' — print the command reference."""
        return (
            "╔══════════════════════════════════════════════╗\n"
            "║            AVAILABLE COMMANDS                ║\n"
            "╠══════════════════════════════════════════════╣\n"
            "║  look / l              Describe current room  ║\n"
            "║  look at <item>        Examine an item        ║\n"
            "║  go <dir> / <dir>      Move (n, s, e, w)     ║\n"
            "║  take <item>           Pick up an item        ║\n"
            "║  drop <item>           Put down an item       ║\n"
            "║  inventory / i         List carried items     ║\n"
            "║  use <item>            Activate an item       ║\n"
            "║  history               Last 10 events         ║\n"
            "║  help / h              Show this help         ║\n"
            "║  quit / q              Leave the game         ║\n"
            "╚══════════════════════════════════════════════╝\n"
            "Synonyms: examine=look at, grab=take, read=use,\n"
            "          move/walk/head=go, put down=drop"
        )

    def _cmd_history(self) -> str:
        """Handle 'history' — show the last 10 events."""
        if not self.state.history:
            return "No events recorded yet."
        recent = self.state.history[-10:]
        lines = ["── Recent Events ──"]
        for i, entry in enumerate(recent, 1):
            lines.append(f"  {i}. {entry}")
        return "\n".join(lines)

    # ─────────────────────────────────────────
    # Item Lookup Helpers
    # ─────────────────────────────────────────

    def _find_item_in_room(self, query: str) -> Optional[str]:
        """Find an item in the current room by partial name match.

        Args:
            query: Lowercased search string.

        Returns:
            The item_id, or None if not found.
        """
        room = self.rooms[self.state.current_room]
        for iid in room.items:
            if query in self.items[iid].item_id or query in self.items[iid].name.lower():
                return iid
        return None

    def _find_item_in_inventory(self, query: str) -> Optional[str]:
        """Find an item in the player's inventory by partial name match.

        Args:
            query: Lowercased search string.

        Returns:
            The item_id, or None if not found.
        """
        for iid in self.state.inventory:
            if query in self.items[iid].item_id or query in self.items[iid].name.lower():
                return iid
        return None

    # ─────────────────────────────────────────
    # Dynamic Room Descriptions
    # ─────────────────────────────────────────

    def _get_room_description(self, room: Room) -> str:
        """Return the effective description for a room, applying
        dynamic modifications based on game state (flags, inventory).

        Args:
            room: The Room to describe.

        Returns:
            The (possibly modified) description string.
        """
        desc = room.description

        # Library: bookshelf moves after lever is pulled
        if room.room_id == "library" and self.state.get_flag("lever_pulled"):
            desc = (
                "The dusty bookshelf has been pushed aside, revealing a "
                "narrow secret passage to the west. Cool air drifts through "
                "the gap. The remaining shelves still groan under the weight "
                "of forgotten tomes."
            )

        # Armory: torch glow
        if room.room_id == "armory" and self.state.has_item("torch"):
            desc = (
                "Racks of rusted swords and dented shields line the walls. "
                "Your torch casts a warm, flickering light across the steel, "
                "making the shadows dance. The air smells of iron and old "
                "grease."
            )

        return desc

    # ─────────────────────────────────────────
    # Output Helpers
    # ─────────────────────────────────────────

    @staticmethod
    def _print_separator() -> None:
        """Print a visual separator line before command output."""
        print("─" * 50)


# ─────────────────────────────────────────────
# World Construction
# ─────────────────────────────────────────────


def build_dungeon() -> Tuple[Dict[str, Room], Dict[str, Item], str]:
    """Construct and return the mini-dungeon world.

    Returns:
        A tuple of (rooms, items, starting_room_id).
    """

    # -- Item behaviour callbacks ---------------------------------------
    # (Defined as closures that capture the engine via the on_use signature.)

    def _use_rusted_key(engine: GameEngine) -> str:
        """Attempt to use the rusted key outside of a door context."""
        return "You fumble with the rusted key, but there's no lock here to open. Perhaps it fits a door somewhere else."

    def _use_lever(engine: GameEngine) -> str:
        """Pull the lever in the Library to open the secret passage."""
        if engine.state.current_room != "library":
            return "You pull the lever, but nothing happens. It seems anchored to this room."
        if engine.state.get_flag("lever_pulled"):
            return "The lever is already pulled. The passage to the west stands open."
        engine.state.set_flag("lever_pulled")
        return (
            "You grip the cold iron lever and heave. With a grinding scrape "
            "of stone on stone, the massive bookshelf slides aside, revealing "
            "a narrow, dark passage to the west. A faint draft carries the "
            "scent of ancient parchment."
        )

    def _use_torch(engine: GameEngine) -> str:
        """Light or examine the torch."""
        if not engine.state.get_flag("torch_lit"):
            engine.state.set_flag("torch_lit")
            return "You strike a match against the rough wall. The torch catches with a whoosh, bathing the room in warm, flickering light."
        return "The torch burns steadily, casting long shadows on the walls."

    def _use_scroll(engine: GameEngine) -> str:
        """Read the ancient scroll — the secret ending."""
        if engine.state.get_flag("won"):
            return "You've already read the scroll. Its words are etched in your memory forever."
        engine.state.set_flag("won")
        return (
            "You unroll the ancient scroll with trembling hands. The ink "
            "shimmers with an inner light as you read:\n"
            "\n"
            "  ┌─────────────────────────────────────────────────┐\n"
            "  │  In an age before the first stone was laid,    │\n"
            "  │  a guardian sealed this chamber to protect     │\n"
            "  │  a truth too vast for any single mind.         │\n"
            "  │                                                │\n"
            "  │  The dungeon was never a prison.               │\n"
            "  │  It was a GARDEN — a place where forgotten     │\n"
            "  │  knowledge could grow in safety.               │\n"
            "  │                                                │\n"
            "  │  You, who found the key, pulled the lever,     │\n"
            "  │  lit the dark, and sought the scroll:          │\n"
            "  │  you are the new guardian.                     │\n"
            "  │                                                │\n"
            "  │  The walls dissolve. The stone becomes soil.   │\n"
            "  │  And in the garden, a single golden coin       │\n"
            "  │  gleams among the roses.                       │\n"
            "  │                                                │\n"
            "  │  ── THE END ──                                 │\n"
            "  └─────────────────────────────────────────────────┘\n"
            "\n"
            "═══════════════════════════════════════════════════\n"
            "  ★  SECRET ENDING UNLOCKED: THE GARDEN  ★\n"
            "═══════════════════════════════════════════════════\n"
            "\n"
            "You have discovered the hidden truth of the Forgotten Dungeon.\n"
            "Thank you for playing!"
        )

    def _use_golden_coin(engine: GameEngine) -> str:
        """Examine the golden coin (cosmetic)."""
        return (
            "You hold the golden coin up to the light. It's warm, almost "
            "alive. On one side: a smiling face. On the other: the words "
            "'You found me. Nice.' You grin. Some treasures are just fun."
        )

    # -- Items ----------------------------------------------------------

    items: Dict[str, Item] = {
        "rusted_key": Item(
            item_id="rusted_key",
            name="rusted key",
            description=(
                "A small iron key, green with age. The teeth are worn but "
                "intact. It feels heavy in your hand, as if it carries "
                "important weight."
            ),
            on_use=_use_rusted_key,
        ),
        "lever": Item(
            item_id="lever",
            name="iron lever",
            description=(
                "A heavy iron lever protruding from the wall, set into the "
                "bookshelf. Dust coats every surface. A faded label reads: "
                "'For the worthy.'"
            ),
            on_use=_use_lever,
        ),
        "torch": Item(
            item_id="torch",
            name="torch",
            description=(
                "A sturdy wooden torch with a thick cloth wick, still "
                "soaked in tallow. It hasn't been lit yet, but it looks "
                "ready for a flame."
            ),
            on_use=_use_torch,
        ),
        "ancient_scroll": Item(
            item_id="ancient_scroll",
            name="ancient scroll",
            description=(
                "A scroll of impossibly fine parchment, sealed with wax the "
                "colour of deep twilight. The script is in a language you "
                "don't recognise — yet somehow, you feel you could read it."
            ),
            on_use=_use_scroll,
        ),
        "golden_coin": Item(
            item_id="golden_coin",
            name="golden coin",
            description=(
                "A perfectly round golden coin, warm to the touch. It "
                "shouldn't be this heavy for its size. You flip it — it "
                "lands on heads every single time. Coincidence? Probably "
                "not."
            ),
            on_use=_use_golden_coin,
        ),
    }

    # -- Rooms ----------------------------------------------------------

    rooms: Dict[str, Room] = {
        "entrance": Room(
            room_id="entrance",
            name="Entrance Hall",
            description=(
                "Cold stone arches frame the doorway you entered through. "
                "Dust motes drift in a shaft of pale light. The floor is "
                "cracked and uneven, and a faint draft whispers from "
                "somewhere deeper in the dungeon."
            ),
            exits={"north": "library", "east": "armory", "south": "garden"},
            items=["rusted_key"],
        ),
        "library": Room(
            room_id="library",
            name="Library",
            description=(
                "Floor-to-ceiling bookshelves crowd the room, their spines "
                "faded and cracked. A thick layer of dust muffles every "
                "footstep. To the west, a massive bookshelf seems to block "
                "the wall entirely — or does it? An iron lever is set into "
                "its side."
            ),
            exits={"south": "entrance", "west": "secret_chamber"},
            items=["lever"],
            locked_exits={
                "west": {
                    "required_item": None,
                    "unlock_flag": "lever_pulled",
                    "message": (
                        "The bookshelf is immovable. It's anchored deep into "
                        "the wall. Perhaps that iron lever on its side is "
                        "the mechanism."
                    ),
                }
            },
        ),
        "armory": Room(
            room_id="armory",
            name="Armory",
            description=(
                "Racks of rusted swords and dented shields line the walls. "
                "The air smells of iron and old grease. A heavy door to "
                "the north is banded in iron and bears a small, round "
                "keyhole."
            ),
            exits={"west": "entrance", "north": "treasure_room"},
            items=["torch"],
            locked_exits={
                "north": {
                    "required_item": "rusted_key",
                    "unlock_flag": None,
                    "message": (
                        "The iron-banded door is locked tight. A small round "
                        "keyhole glints in the dim light. You'd need a key."
                    ),
                }
            },
        ),
        "treasure_room": Room(
            room_id="treasure_room",
            name="Treasure Room",
            description=(
                "A small, circular chamber. The walls are carved with a "
                "riddle in a script that predates any known language: "
                "'What grows more the more you take? — A hole. What "
                "shrinks the more you fill? — A doubt.' In the centre, "
                "on a stone pedestal, a single golden coin catches the "
                "light."
            ),
            exits={"south": "armory"},
            items=["golden_coin"],
        ),
        "secret_chamber": Room(
            room_id="secret_chamber",
            name="Secret Chamber",
            description=(
                "A tiny, hidden room barely large enough for one person. "
                "The walls are smooth and warm, as if the stone remembers "
                "a fire long extinguished. On a small stone shelf rests a "
                "scroll, sealed and waiting."
            ),
            exits={"east": "library"},
            items=["ancient_scroll"],
        ),
        "garden": Room(
            room_id="garden",
            name="Garden",
            description=(
                "Against all expectation, a small clearing opens before "
                "you. Moss covers the stone floor, and a single pale "
                "flower pushes through a crack in the wall. The air is "
                "still and sweet. You've found peace.\n"
                "\n"
                "  *  *  *"
            ),
            exits={"north": "entrance"},
            items=[],
        ),
    }

    return rooms, items, "entrance"


# ─────────────────────────────────────────────
# Banner & Entry Point
# ─────────────────────────────────────────────


def print_banner() -> None:
    """Print the ASCII title banner at game start."""
    banner: str = (
        "\n"
        "  ╔═══════════════════════════════════════════════════════╗\n"
        "  ║                                                       ║\n"
        "  ║        ⚔   T H E   F O R G O T T E N   D U N G E O N  ⚔  ║\n"
        "  ║                                                       ║\n"
        "  ║           A  T e x t - B a s e d  A d v e n t u r e     ║\n"
        "  ║                                                       ║\n"
        "  ╚═══════════════════════════════════════════════════════╝\n"
        "\n"
    )
    print(banner)


def main() -> None:
    """Entry point: build the dungeon, print the banner, run the game loop."""
    rooms, items, start = build_dungeon()
    engine = GameEngine(rooms=rooms, items=items, starting_room=start)

    print_banner()
    print("  Welcome, adventurer. You stand in a cold, silent hall.")
    print("  Type 'help' to see available commands.")
    print("  Type 'quit' to leave at any time.")

    try:
        engine.run_loop()
    except (EOFError, KeyboardInterrupt):
        print("\n\nYou leave the dungeon. Goodbye!")
    finally:
        # Post-game summary
        print()
        print("═" * 50)
        print(f"  Rooms explored: {len(engine.state.visited_rooms)} / {len(rooms)}")
        print(f"  Items found:    {len(engine.state.inventory)}")
        if engine.state.get_flag("won"):
            print("  Ending:         ★ SECRET ENDING — THE GARDEN ★")
        else:
            print("  Ending:         The dungeon keeps its secrets...")
        print("═" * 50)
        print("  Thank you for playing!")


if __name__ == "__main__":
    main()
